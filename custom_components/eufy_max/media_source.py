"""Medienquelle fuer Eufy Max.

Legt in der Medienübersicht einen Eintrag "Eufy Max" an, darunter einen
Ordner "Cams" mit allen Kameras. Das Live-Bild gibt es weiterhin nur,
wenn der Livestream-Controller aktiv ist - abspielen startet also nichts
von selbst.
"""

from __future__ import annotations

import logging

from homeassistant.components import media_source
from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.components.media_source.models import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
)
from homeassistant.components.stream import FORMAT_CONTENT_TYPE, HLS_PROVIDER
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

FOLDER = "cams"


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Medienquelle bereitstellen - wird von HA automatisch aufgerufen."""
    return EufyMaxMediaSource(hass)


class EufyMaxMediaSource(MediaSource):
    """Zeigt die Eufy-Kameras in der Medienübersicht."""

    name = "Eufy Max"

    def __init__(self, hass: HomeAssistant) -> None:
        """Medienquelle initialisieren."""
        super().__init__(DOMAIN)
        self.hass = hass

    # ------------------------------------------------------------------

    def _cameras(self) -> list[tuple[str, str, bool]]:
        """Kamera-Entities dieser Integration mit Name und Stream-Faehigkeit."""
        registry = er.async_get(self.hass)
        result: list[tuple[str, str, bool]] = []

        for entry in registry.entities.values():
            if entry.platform != DOMAIN or entry.domain != "camera":
                continue
            state = self.hass.states.get(entry.entity_id)
            name = (
                state.attributes.get("friendly_name")
                if state
                else None
            ) or entry.original_name or entry.entity_id
            # Bit 2 von supported_features ist CameraEntityFeature.STREAM
            features = state.attributes.get("supported_features", 0) if state else 0
            result.append((entry.entity_id, name, bool(features & 2)))

        return sorted(result, key=lambda item: item[1])

    # ------------------------------------------------------------------

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Ordnerstruktur aufbauen."""
        identifier = item.identifier or ""

        # Ebene 2: Inhalt des Ordners "Cams"
        if identifier == FOLDER:
            return BrowseMediaSource(
                domain=DOMAIN,
                identifier=FOLDER,
                media_class=MediaClass.DIRECTORY,
                media_content_type=MediaType.VIDEO,
                title="Cams",
                can_play=False,
                can_expand=True,
                children_media_class=MediaClass.VIDEO,
                children=[
                    self._camera_item(entity_id, name, can_stream)
                    for entity_id, name, can_stream in self._cameras()
                ],
            )

        # Ebene 1: Wurzel mit dem Ordner "Cams"
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title="Eufy Max",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=[
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=FOLDER,
                    media_class=MediaClass.DIRECTORY,
                    media_content_type=MediaType.VIDEO,
                    title="Cams",
                    can_play=False,
                    can_expand=True,
                    children_media_class=MediaClass.VIDEO,
                )
            ],
        )

    def _camera_item(
        self, entity_id: str, name: str, can_stream: bool
    ) -> BrowseMediaSource:
        """Eine einzelne Kamera als abspielbaren Eintrag.

        Der Medientyp muss zur Betriebsart passen: HLS bei RTSP-Kameras,
        Einzelbild bei den P2P-Kameras. Sonst greift der Browser zum
        falschen Player und meldet ein nicht unterstuetztes Format.
        """
        content_type = (
            FORMAT_CONTENT_TYPE[HLS_PROVIDER] if can_stream else "image/jpeg"
        )
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{FOLDER}/{entity_id}",
            media_class=MediaClass.VIDEO,
            media_content_type=content_type,
            title=name,
            can_play=True,
            can_expand=False,
            thumbnail=f"/api/camera_proxy/{entity_id}",
        )

    # ------------------------------------------------------------------

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Kamera abspielen - nur wenn der Livestream laeuft."""
        identifier = item.identifier or ""
        entity_id = identifier.split("/", 1)[-1]

        if not entity_id.startswith("camera."):
            raise Unresolvable(f"Unbekannter Eintrag: {identifier}")

        if not self._stream_active():
            raise Unresolvable(
                "Der Livestream ist aus. Erst den Knopf "
                "'Livestream starten' druecken, dann erneut oeffnen."
            )

        # Die eigentliche HLS-Aufbereitung macht Home Assistant selbst.
        return await media_source.async_resolve_media(
            self.hass, f"media-source://camera/{entity_id}", None
        )

    def _stream_active(self) -> bool:
        """Prueft, ob der Livestream-Controller gerade laeuft."""
        for client in self.hass.data.get(DOMAIN, {}).values():
            controller = getattr(client, "stream", None)
            if controller is not None and controller.active:
                return True
        return False
