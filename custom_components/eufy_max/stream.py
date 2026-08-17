"""Zentraler Livestream-Controller.

Kameras streamen nicht dauerhaft, sondern werden gezielt aufgeschaltet
und nach einer einstellbaren Zeit automatisch wieder abgeschaltet - je
Kamera einzeln oder alle zusammen.
"""

from __future__ import annotations

import asyncio
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
    """Alle Seriennummern liefern, die ueberhaupt ein Bild liefern koennen."""
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
    """Schaltet Kameras einzeln oder gemeinsam auf.

    Jede Kamera hat ihre eigene Abschaltzeit. Das ist wichtig, weil Eufy
    nicht beliebig viele Livestreams gleichzeitig zulaesst - wer nur eine
    Kamera ansehen will, soll nicht alle belegen.
    """

    def __init__(self, hass: HomeAssistant, client: EufyMaxClient) -> None:
        """Controller initialisieren."""
        self.hass = hass
        self.client = client
        self.duration: int = DEFAULT_STREAM_DURATION

        # serialNumber -> Abschaltzeitpunkt
        self.ends_at_map: dict[str, datetime] = {}
        # serialNumber -> laufende P2P-Bruecke
        self.bridges: dict[str, P2PVideoBridge] = {}
        # Kameras, deren Livestream die Bibliothek ablehnt. Fuer die gibt
        # es nur das letzte Ereignisbild - erneute Versuche erzeugen nur
        # Fehler im Protokoll.
        self.unsupported: set[str] = set()
        self._timers: dict[str, object] = {}

    # ------------------------------------------------------------------
    # Zustand
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        """Laeuft irgendeine Kamera?"""
        return bool(self.ends_at_map)

    @property
    def ends_at(self) -> datetime | None:
        """Spaetester Abschaltzeitpunkt ueber alle Kameras."""
        if not self.ends_at_map:
            return None
        return max(self.ends_at_map.values())

    @property
    def remaining(self) -> int:
        """Laengste Restlaufzeit in Sekunden."""
        end = self.ends_at
        if end is None:
            return 0
        return max(0, int((end - dt_util.utcnow()).total_seconds()))

    @property
    def active_cameras(self) -> list[str]:
        """Seriennummern der Kameras, die gerade laufen."""
        return list(self.ends_at_map)

    def is_active(self, serial: str) -> bool:
        """Laeuft diese eine Kamera?"""
        return serial in self.ends_at_map

    def remaining_for(self, serial: str) -> int:
        """Restlaufzeit einer einzelnen Kamera."""
        end = self.ends_at_map.get(serial)
        if end is None:
            return 0
        return max(0, int((end - dt_util.utcnow()).total_seconds()))

    def supports_livestream(self, serial: str) -> bool:
        """Kann diese Kamera ueberhaupt einen Livestream liefern?"""
        return serial not in self.unsupported

    def set_duration(self, seconds: int) -> None:
        """Neue Standarddauer setzen."""
        self.duration = int(seconds)
        self._notify()

    # ------------------------------------------------------------------
    # Einzelne Kamera
    # ------------------------------------------------------------------

    async def async_start_one(
        self, serial: str, duration: int | None = None
    ) -> None:
        """Eine einzelne Kamera aufschalten."""
        seconds = int(duration or self.duration)

        if serial not in self.client.devices:
            _LOGGER.warning("Unbekannte Kamera: %s", serial)
            return

        already = serial in self.ends_at_map

        self.ends_at_map[serial] = dt_util.utcnow() + timedelta(seconds=seconds)
        self._schedule_stop(serial, seconds)
        self._notify()

        if not already:
            await self._async_start_camera(serial)
            _LOGGER.info(
                "Livestream fuer %s gestartet, Abschaltung in %s Sekunden",
                self.client.get_property(serial, "name", serial),
                seconds,
            )
            self._notify()

    async def async_stop_one(self, serial: str) -> None:
        """Eine einzelne Kamera abschalten."""
        self._cancel_timer(serial)
        self.ends_at_map.pop(serial, None)
        await self._async_stop_camera(serial)
        self._notify()

    # ------------------------------------------------------------------
    # Alle Kameras
    # ------------------------------------------------------------------

    async def async_start(self, duration: int | None = None) -> None:
        """Alle Kameras aufschalten."""
        seconds = int(duration or self.duration)
        serials = camera_serials(self.client)

        if not serials:
            _LOGGER.warning("Keine streamfaehige Kamera gefunden")
            return

        _LOGGER.info(
            "Starte Livestream fuer %s Kameras. Hinweis: Eufy laesst nicht "
            "beliebig viele Streams gleichzeitig zu - fuer eine einzelne "
            "Kamera besser eufy_max.start_camera_stream nutzen",
            len(serials),
        )

        await asyncio.gather(
            *(self.async_start_one(serial, seconds) for serial in serials),
            return_exceptions=True,
        )

    async def async_stop(self) -> None:
        """Alle Kameras abschalten."""
        await asyncio.gather(
            *(self.async_stop_one(serial) for serial in list(self.ends_at_map)),
            return_exceptions=True,
        )
        # Sicherheitshalber auch verwaiste Bruecken einsammeln.
        for serial in list(self.bridges):
            await self._async_stop_camera(serial)

        _LOGGER.info("Livestream aller Kameras gestoppt")
        self._notify()

    async def async_extend(self, seconds: int) -> None:
        """Alle laufenden Kameras verlaengern."""
        if not self.ends_at_map:
            await self.async_start(seconds)
            return

        for serial in list(self.ends_at_map):
            self.ends_at_map[serial] = dt_util.utcnow() + timedelta(seconds=seconds)
            self._schedule_stop(serial, seconds)
        self._notify()

    # ------------------------------------------------------------------
    # Technik je Kamera
    # ------------------------------------------------------------------

    async def _async_start_camera(self, serial: str) -> None:
        """Kamera in den Streammodus bringen."""
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

        if serial in self.unsupported:
            _LOGGER.debug(
                "%s kann keinen Livestream - es bleibt beim Ereignisbild",
                serial,
            )
            return

        bridge = P2PVideoBridge(self.hass, self.client, serial)
        self.bridges[serial] = bridge
        await bridge.async_start()

        if not bridge.supported:
            self.unsupported.add(serial)
            self.bridges.pop(serial, None)

    async def _async_stop_camera(self, serial: str) -> None:
        """Kamera wieder abschalten."""
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
            _LOGGER.debug("Stopp von %s: %s", serial, err)

    # ------------------------------------------------------------------
    # Timer
    # ------------------------------------------------------------------

    def _schedule_stop(self, serial: str, seconds: int) -> None:
        """Abschalttimer fuer eine Kamera setzen."""
        self._cancel_timer(serial)

        @callback
        def _finished(_now) -> None:
            self._timers.pop(serial, None)
            self.hass.async_create_task(self.async_stop_one(serial))

        self._timers[serial] = async_call_later(self.hass, seconds, _finished)

    def _cancel_timer(self, serial: str) -> None:
        """Timer einer Kamera abbrechen."""
        unsub = self._timers.pop(serial, None)
        if unsub is not None:
            unsub()

    def _notify(self) -> None:
        """Alle betroffenen Entities aktualisieren."""
        async_dispatcher_send(self.hass, SIGNAL_STREAM_STATE)

    async def async_shutdown(self) -> None:
        """Beim Entladen aufraeumen."""
        for serial in list(self._timers):
            self._cancel_timer(serial)
        if self.ends_at_map or self.bridges:
            await self.async_stop()
