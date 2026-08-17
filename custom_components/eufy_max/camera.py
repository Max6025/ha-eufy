"""Kamera-Entity - streamt nur, wenn der Controller den Stream freigegeben hat.

Im Ruhezustand liefert die Kamera nur noch das letzte Ereignisbild. Erst
wenn der Livestream-Button gedrueckt wurde, gibt stream_source() eine
Quelle zurueck.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CAMERA_ENABLED_PROPERTY,
    DOMAIN,
    RTSP_PROPERTY,
    RTSP_URL_PROPERTY,
    SIGNAL_STREAM_STATE,
)
from .entity import EufyMaxEntity
from .stream import camera_serials
from .websocket import EufyMaxClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Fuer jedes Kamerageraet eine Kamera-Entity anlegen."""
    client: EufyMaxClient = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EufyMaxCamera(client, serial) for serial in camera_serials(client)
    )


class EufyMaxCamera(EufyMaxEntity, Camera):
    """Eine Eufy-Kamera in Home Assistant."""

    _attr_name = None  # nutzt den Geraetenamen
    _attr_supported_features = CameraEntityFeature.STREAM | CameraEntityFeature.ON_OFF

    def __init__(self, client: EufyMaxClient, serial: str) -> None:
        """Kamera initialisieren."""
        EufyMaxEntity.__init__(self, client, serial)
        Camera.__init__(self)
        self._attr_unique_id = f"{serial}_camera"

    @property
    def controller(self):
        """Zentraler Livestream-Controller."""
        return self.client.stream

    async def async_added_to_hass(self) -> None:
        """Auf Geraete- und Controllerzustand hoeren."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_STREAM_STATE, self._handle_update
            )
        )

    @property
    def is_on(self) -> bool:
        """Ist die Kamera eingeschaltet?"""
        return bool(self.get_property(CAMERA_ENABLED_PROPERTY, True))

    @property
    def is_streaming(self) -> bool:
        """Nur streamend, wenn der Controller aktiv ist."""
        return self.controller.active

    @property
    def brand(self) -> str:
        """Hersteller."""
        return "Anker Eufy"

    @property
    def model(self) -> str | None:
        """Modellbezeichnung."""
        return self.device.get("model")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Zusatzinfos fuer Automatisierungen und Fehlersuche."""
        return {
            "serial_number": self.serial,
            "stream_aktiv": self.controller.active,
            "restzeit_sekunden": self.controller.remaining,
            "rtsp_url": self.get_property(RTSP_URL_PROPERTY),
            "battery": self.get_property("battery"),
            "wifi_rssi": self.get_property("wifiRssi"),
        }

    async def stream_source(self) -> str | None:
        """Streamquelle nur liefern, wenn der Controller den Stream freigibt.

        Es gibt nur eine brauchbare Quelle: die RTSP-URL, die die Kamera
        selbst meldet. Der P2P-Stream von eufy-security-ws kommt als
        Datenstrom ueber die WebSocket-Verbindung und laesst sich nicht
        als URL weiterreichen - dafuer braucht es eine Bruecke.
        """
        if not self.controller.active:
            _LOGGER.debug(
                "Streamanfrage fuer %s abgelehnt - Livestream ist aus", self.serial
            )
            return None

        rtsp_url = self.get_property(RTSP_URL_PROPERTY)
        if rtsp_url:
            return rtsp_url

        _LOGGER.warning(
            "%s meldet keine RTSP-URL. Diese Kamera kann nur P2P und "
            "liefert ohne Bruecke kein Livebild",
            self.device.get("name", self.serial),
        )
        return None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Standbild: bei laufendem Stream live, sonst letztes Ereignisbild."""
        if self.controller.active:
            source = await self.stream_source()
            if source:
                try:
                    ffmpeg = get_ffmpeg_manager(self.hass)
                    from haffmpeg.tools import IMAGE_JPEG, ImageFrame

                    image_frame = ImageFrame(ffmpeg.binary)
                    image = await image_frame.get_image(
                        source, output_format=IMAGE_JPEG
                    )
                    if image:
                        return image
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Snapshot ueber ffmpeg fehlgeschlagen: %s", err)

        picture = self.get_property("picture")
        if isinstance(picture, dict) and picture.get("data"):
            import base64

            data = picture["data"]
            if isinstance(data, dict) and "data" in data:
                return bytes(data["data"])
            if isinstance(data, str):
                return base64.b64decode(data)
        return None

    async def async_turn_on(self) -> None:
        """Kamera einschalten."""
        await self.client.async_set_property(
            self.serial, CAMERA_ENABLED_PROPERTY, True
        )

    async def async_turn_off(self) -> None:
        """Kamera ausschalten."""
        await self.client.async_set_property(
            self.serial, CAMERA_ENABLED_PROPERTY, False
        )
