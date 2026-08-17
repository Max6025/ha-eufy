"""Zentraler Livestream-Controller.

Statt dauerhaft zu streamen werden alle Kameras per Knopfdruck gemeinsam
gestartet und nach einer einstellbaren Zeit automatisch wieder gestoppt.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import (
    CAMERA_DEVICE_TYPES,
    DEFAULT_STREAM_DURATION,
    RTSP_PROPERTY,
    SIGNAL_STREAM_STATE,
)
from .p2p import P2PVideoBridge
from .websocket import EufyMaxClient

_LOGGER = logging.getLogger(__name__)

# Eigenschaften, die es praktisch nur bei Kameras gibt. Damit werden
# auch Modelle erkannt, die die Eufy-Bibliothek noch nicht kennt.
CAMERA_HINTS = (
    "picture",
    "watermark",
    "videoStreamingQuality",
    "motionDetection",
)


def camera_serials(client: EufyMaxClient) -> list[str]:
    """Alle Seriennummern liefern, die ueberhaupt ein Bild liefern koennen.

    Mehrere Wege, weil kein einzelner zuverlaessig ist: manche Modelle
    melden eine RTSP-Eigenschaft, andere den Livestream-Befehl. Ganz neue
    Modelle kennt die Eufy-Bibliothek noch nicht - dann ist die
    Befehlsliste leer und der Typ unbekannt, und es entscheiden die
    typischen Kameraeigenschaften.
    """
    serials: list[str] = []

    for serial, device in client.devices.items():
        metadata = client.get_metadata(serial)
        device_type = device.get("type")

        is_camera = (
            RTSP_PROPERTY in metadata
            or client.has_command(serial, "start_livestream")
            or (isinstance(device_type, int) and device_type in CAMERA_DEVICE_TYPES)
            or any(prop in metadata for prop in CAMERA_HINTS)
        )

        if is_camera:
            serials.append(serial)
        else:
            _LOGGER.debug(
                "%s (Typ %s) wird nicht als Kamera behandelt",
                device.get("name", serial),
                device_type,
            )

    return serials


class StreamController:
    """Startet und stoppt den Livestream aller Kameras gemeinsam."""

    def __init__(self, hass: HomeAssistant, client: EufyMaxClient) -> None:
        """Controller initialisieren."""
        self.hass = hass
        self.client = client
        self.active: bool = False
        self.duration: int = DEFAULT_STREAM_DURATION
        self.ends_at: datetime | None = None
        self.last_started: datetime | None = None
        # serialNumber -> laufende P2P-Bruecke
        self.bridges: dict[str, P2PVideoBridge] = {}
        self._unsub_timer = None

    # ------------------------------------------------------------------

    @property
    def remaining(self) -> int:
        """Restlaufzeit in Sekunden."""
        if not self.active or self.ends_at is None:
            return 0
        delta = (self.ends_at - dt_util.utcnow()).total_seconds()
        return max(0, int(delta))

    @property
    def active_cameras(self) -> list[str]:
        """Seriennummern der Kameras, die gerade laufen."""
        return camera_serials(self.client) if self.active else []

    # ------------------------------------------------------------------

    async def async_start(self, duration: int | None = None) -> None:
        """Alle Kameras starten und Abschaltzeit setzen."""
        seconds = int(duration or self.duration)
        serials = camera_serials(self.client)

        if not serials:
            _LOGGER.warning("Keine streamfaehige Kamera gefunden")
            return

        # Erst den Zustand setzen, dann starten: die Kamera-Entities
        # fragen waehrend des Starts schon nach Bildern.
        self.active = True
        self.last_started = dt_util.utcnow()
        self.ends_at = self.last_started + timedelta(seconds=seconds)

        self._cancel_timer()
        self._unsub_timer = async_call_later(
            self.hass, seconds, self._async_timer_finished
        )
        self._notify()

        for serial in serials:
            await self._async_start_camera(serial)

        _LOGGER.info(
            "Livestream fuer %s Kamera(s) gestartet, Abschaltung in %s Sekunden",
            len(serials),
            seconds,
        )
        self._notify()

    async def async_stop(self) -> None:
        """Alle Kameras wieder abschalten."""
        self._cancel_timer()

        for serial in camera_serials(self.client):
            await self._async_stop_camera(serial)

        self.active = False
        self.ends_at = None
        _LOGGER.info("Livestream aller Kameras gestoppt")
        self._notify()

    async def async_extend(self, seconds: int) -> None:
        """Laufenden Stream verlaengern."""
        if not self.active:
            await self.async_start(seconds)
            return

        self.ends_at = dt_util.utcnow() + timedelta(seconds=seconds)
        self._cancel_timer()
        self._unsub_timer = async_call_later(
            self.hass, seconds, self._async_timer_finished
        )
        self._notify()

    def set_duration(self, seconds: int) -> None:
        """Neue Standarddauer setzen."""
        self.duration = int(seconds)
        self._notify()

    # ------------------------------------------------------------------

    async def _async_start_camera(self, serial: str) -> None:
        """Eine einzelne Kamera in den Streammodus bringen.

        Standardweg ist die P2P-Bruecke, weil sie bei allen Modellen
        funktioniert. RTSP wird nur genutzt, wenn es in den Optionen
        eingeschaltet ist - die von den Kameras gemeldeten RTSP-Adressen
        stimmen naemlich nicht immer.
        """
        metadata = self.client.get_metadata(serial)
        use_rtsp = getattr(self.client, "rtsp_first", False)

        if use_rtsp and RTSP_PROPERTY in metadata:
            try:
                await self.client.async_set_property(serial, RTSP_PROPERTY, True)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("RTSP fuer %s nicht aktivierbar: %s", serial, err)
            return

        if serial in self.bridges:
            return

        bridge = P2PVideoBridge(self.hass, self.client, serial)
        self.bridges[serial] = bridge

        # Bewusst ohne Abbruch bei Zeitueberschreitung: manche Kameras
        # senden erst nach einigen Sekunden ein Vollbild, und vorher kann
        # ffmpeg nichts liefern. Die Bruecke laeuft weiter und holt das
        # Bild nach.
        await bridge.async_start()

    async def _async_stop_camera(self, serial: str) -> None:
        """Eine einzelne Kamera wieder abschalten."""
        bridge = self.bridges.pop(serial, None)
        if bridge is not None:
            await bridge.async_stop()
            return

        metadata = self.client.get_metadata(serial)
        try:
            if RTSP_PROPERTY in metadata:
                await self.client.async_set_property(serial, RTSP_PROPERTY, False)
            elif self.client.get_property(serial, "livestreaming"):
                await self.client.async_stop_livestream(serial)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Stopp von %s fehlgeschlagen: %s", serial, err)

    @callback
    def _async_timer_finished(self, _now) -> None:
        """Timer abgelaufen - abschalten."""
        self._unsub_timer = None
        self.hass.async_create_task(self.async_stop())

    def _cancel_timer(self) -> None:
        """Laufenden Timer abbrechen."""
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    def _notify(self) -> None:
        """Alle betroffenen Entities aktualisieren."""
        async_dispatcher_send(self.hass, SIGNAL_STREAM_STATE)

    async def async_shutdown(self) -> None:
        """Beim Entladen aufraeumen."""
        self._cancel_timer()
        if self.active:
            await self.async_stop()
