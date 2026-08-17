"""Kamera-Entity.

Zwei Betriebsarten, je nachdem was die Kamera kann:

* Meldet die Kamera eine RTSP-URL, wird diese als Streamquelle genutzt.
  Home Assistant macht daraus ein flüssiges Livebild.
* Sonst laeuft der P2P-Datenstrom ueber eine ffmpeg-Bruecke, aus der
  Einzelbilder kommen. Die Kamera meldet dann bewusst KEINE
  Stream-Faehigkeit, damit Home Assistant sein Einzelbild-Verfahren
  nutzt statt einen Stream zu erwarten, den es nicht gibt.

In beiden Faellen gibt es ein Bild nur, wenn der Livestream-Controller
aktiv ist.
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

    def __init__(self, client: EufyMaxClient, serial: str) -> None:
        """Kamera initialisieren."""
        EufyMaxEntity.__init__(self, client, serial)
        Camera.__init__(self)
        self._attr_unique_id = f"{serial}_camera"

        # Nur Kameras mit RTSP koennen echtes Streaming. Bei den anderen
        # darf die Faehigkeit nicht gemeldet werden, sonst kommt
        # "does not support play stream service".
        self._has_rtsp = RTSP_PROPERTY in client.get_metadata(serial)

        if self._has_rtsp:
            self._attr_supported_features = (
                CameraEntityFeature.STREAM | CameraEntityFeature.ON_OFF
            )
        else:
            self._attr_supported_features = CameraEntityFeature.ON_OFF

    @property
    def controller(self):
        """Zentraler Livestream-Controller."""
        return self.client.stream

    @property
    def bridge(self):
        """P2P-Bruecke dieser Kamera, falls vorhanden."""
        controller = self.controller
        if controller is None:
            return None
        return controller.bridges.get(self.serial)

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
        """Laeuft gerade ein Livebild?"""
        return bool(self.controller and self.controller.active)

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
        bridge = self.bridge
        return {
            "serial_number": self.serial,
            "betriebsart": "rtsp" if self._has_rtsp else "p2p",
            "stream_aktiv": bool(self.controller and self.controller.active),
            "restzeit_sekunden": self.controller.remaining if self.controller else 0,
            "rtsp_url": self.get_property(RTSP_URL_PROPERTY),
            "bilder_empfangen": bridge.frames_received if bridge else 0,
            "battery": self.get_property("battery"),
            "wifi_rssi": self.get_property("wifiRssi"),
        }

    async def stream_source(self) -> str | None:
        """RTSP-Quelle - nur fuer Kameras, die eine URL melden."""
        if not self._has_rtsp:
            return None

        if not (self.controller and self.controller.active):
            _LOGGER.debug(
                "Streamanfrage fuer %s abgelehnt - Livestream ist aus", self.serial
            )
            return None

        return self.get_property(RTSP_URL_PROPERTY)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Einzelbild liefern.

        Bei P2P kommt es aus der Bruecke, bei RTSP aus dem Stream. Ist
        nichts aktiv, gibt es das letzte Ereignisbild.
        """
        bridge = self.bridge
        if bridge is not None and bridge.latest_image:
            return bridge.latest_image

        if self._has_rtsp and self.controller and self.controller.active:
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
