"""Basisklasse fuer die Steuer-Entities des Livestream-Controllers.

Diese Entities gehoeren nicht zu einer einzelnen Kamera, sondern zu einem
eigenen Geraet "Eufy Max Steuerung".
"""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, HUB_IDENTIFIER, SIGNAL_STREAM_STATE
from .stream import StreamController


class EufyMaxControllerEntity(Entity):
    """Gemeinsame Basis fuer alle Controller-Entities."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller: StreamController) -> None:
        """Entity initialisieren."""
        self.controller = controller

    @property
    def device_info(self) -> DeviceInfo:
        """Eigenes Geraet fuer die zentrale Steuerung."""
        return DeviceInfo(
            identifiers={(DOMAIN, HUB_IDENTIFIER)},
            name="Eufy Max Steuerung",
            manufacturer="Max",
            model="Livestream Controller",
            entry_type="service",
        )

    @property
    def available(self) -> bool:
        """Verfuegbar, solange die Verbindung steht."""
        return self.controller.client.connected

    async def async_added_to_hass(self) -> None:
        """Auf Zustandsaenderungen des Controllers hoeren."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_STREAM_STATE, self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        """Neu zeichnen."""
        self.async_write_ha_state()
