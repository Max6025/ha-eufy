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
    DEFAULT_STREAM_DURATION,
    RTSP_PROPERTY,
    SIGNAL_STREAM_STATE,
)
from .websocket import EufyMaxClient

_LOGGER = logging.getLogger(__name__)


def camera_serials(client: EufyMaxClient) -> list[str]:
    """Alle Seriennummern liefern, die ueberhaupt streamen koennen."""
    serials: list[str] = []
    for serial in client.devices:
        metadata = client.get_metadata(serial)
        if RTSP_PROPERTY in metadata or client.has_command(
            serial, "device.start_livestream"
        ):
            serials.append(serial)
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

        for serial in serials:
            await self._async_start_camera(serial)

        self.active = True
        self.last_started = dt_util.utcnow()
        self.ends_at = self.last_started + timedelta(seconds=seconds)

        self._cancel_timer()
        self._unsub_timer = async_call_later(
            self.hass, seconds, self._async_timer_finished
        )

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
        """Eine einzelne Kamera in den Streammodus bringen."""
        metadata = self.client.get_metadata(serial)
        try:
            if RTSP_PROPERTY in metadata:
                await self.client.async_set_property(serial, RTSP_PROPERTY, True)
            else:
                await self.client.async_start_livestream(serial)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Start von %s fehlgeschlagen: %s", serial, err)

    async def _async_stop_camera(self, serial: str) -> None:
        """Eine einzelne Kamera wieder abschalten."""
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
