"""Basisklasse fuer alle Eufy Max Entities."""

from __future__ import annotations

from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SIGNAL_DEVICE_UPDATE
from .websocket import EufyMaxClient


class EufyMaxEntity(Entity):
    """Gemeinsame Basis: Geraetezuordnung, Updates, Verfuegbarkeit."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, client: EufyMaxClient, serial: str) -> None:
        """Entity initialisieren."""
        self.client = client
        self.serial = serial

    @property
    def device(self) -> dict[str, Any]:
        """Aktueller Geraetezustand."""
        return self.client.get_device(self.serial)

    @property
    def device_info(self) -> DeviceInfo:
        """Geraeteeintrag fuer die HA-Geraeteliste."""
        device = self.device
        return DeviceInfo(
            identifiers={(DOMAIN, self.serial)},
            name=device.get("name", self.serial),
            manufacturer="Anker Eufy",
            model=device.get("model", "unbekannt"),
            sw_version=device.get("softwareVersion"),
            hw_version=device.get("hardwareVersion"),
            serial_number=self.serial,
        )

    @property
    def available(self) -> bool:
        """Nur verfuegbar, wenn Socket und Treiber stehen."""
        return self.client.connected and self.client.driver_connected

    def get_property(self, name: str, default: Any = None) -> Any:
        """Eigenschaft des Geraets lesen."""
        return self.client.get_property(self.serial, name, default)

    async def async_added_to_hass(self) -> None:
        """Auf Updates fuer dieses Geraet hoeren."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_DEVICE_UPDATE}_{self.serial}",
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        """Zustand neu zeichnen."""
        self.async_write_ha_state()


class EufyMaxPropertyEntity(EufyMaxEntity):
    """Entity, die genau einer Geraeteeigenschaft entspricht."""

    def __init__(
        self,
        client: EufyMaxClient,
        serial: str,
        prop: str,
        meta: dict[str, Any],
    ) -> None:
        """Entity aus Property-Metadaten aufbauen."""
        super().__init__(client, serial)
        self.prop = prop
        self.meta = meta
        self._attr_unique_id = f"{serial}_{prop}"
        self._attr_name = meta.get("label") or _humanize(prop)

    @property
    def native_value(self) -> Any:
        """Rohwert der Eigenschaft."""
        return self.get_property(self.prop)


def _humanize(prop: str) -> str:
    """camelCase-Property in lesbaren Namen umwandeln."""
    out = ""
    for char in prop:
        if char.isupper() and out:
            out += " "
        out += char
    return out[:1].upper() + out[1:]
