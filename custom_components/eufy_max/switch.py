"""Schalter: Livestream-Hauptschalter plus alle schreibbaren Boolean-Properties."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, IGNORED_PROPERTIES, LIGHT_PROPERTIES, RTSP_PROPERTY
from .controller_entity import EufyMaxControllerEntity
from .entity import EufyMaxPropertyEntity
from .websocket import EufyMaxClient


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Alle Schalter anlegen, die das Geraet tatsaechlich kann."""
    client: EufyMaxClient = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = [EufyMaxStreamSwitch(client.stream)]

    for serial in client.devices:
        for prop, meta in client.get_metadata(serial).items():
            if prop in IGNORED_PROPERTIES or prop in LIGHT_PROPERTIES:
                continue
            # RTSP wird vom Controller verwaltet, nicht von Hand geschaltet.
            if prop == RTSP_PROPERTY:
                continue
            if meta.get("type") != "boolean":
                continue
            if not meta.get("writeable"):
                continue
            entities.append(EufyMaxSwitch(client, serial, prop, meta))

    async_add_entities(entities)


class EufyMaxStreamSwitch(EufyMaxControllerEntity, SwitchEntity):
    """Hauptschalter: an startet den Timer, aus stoppt sofort."""

    _attr_name = "Livestream"
    _attr_icon = "mdi:video"
    _attr_unique_id = "eufy_max_stream_switch"

    @property
    def is_on(self) -> bool:
        """Laufen die Kameras gerade?"""
        return self.controller.active

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Restzeit und beteiligte Kameras."""
        return {
            "restzeit_sekunden": self.controller.remaining,
            "eingestellte_dauer": self.controller.duration,
            "endet_um": self.controller.ends_at,
            "kameras": len(self.controller.active_cameras),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Alle Kameras fuer die eingestellte Dauer starten."""
        await self.controller.async_start()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Alle Kameras sofort stoppen."""
        await self.controller.async_stop()


class EufyMaxSwitch(EufyMaxPropertyEntity, SwitchEntity):
    """Ein schaltbarer Geraeteparameter."""

    @property
    def is_on(self) -> bool | None:
        """Aktueller Schaltzustand."""
        value = self.get_property(self.prop)
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Einschalten."""
        await self.client.async_set_property(self.serial, self.prop, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Ausschalten."""
        await self.client.async_set_property(self.serial, self.prop, False)
