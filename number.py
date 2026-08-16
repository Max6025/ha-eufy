"""Zahlenwerte: Stream-Dauer plus alle schreibbaren numerischen Properties."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEFAULT_STREAM_DURATION,
    DOMAIN,
    IGNORED_PROPERTIES,
    MAX_STREAM_DURATION,
    MIN_STREAM_DURATION,
)
from .controller_entity import EufyMaxControllerEntity
from .entity import EufyMaxPropertyEntity
from .websocket import EufyMaxClient


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Zahlenregler anlegen."""
    client: EufyMaxClient = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = [EufyMaxStreamDuration(client.stream)]

    for serial in client.devices:
        for prop, meta in client.get_metadata(serial).items():
            if prop in IGNORED_PROPERTIES:
                continue
            if meta.get("type") != "number":
                continue
            if not meta.get("writeable") or meta.get("states"):
                continue
            entities.append(EufyMaxNumber(client, serial, prop, meta))

    async_add_entities(entities)


class EufyMaxStreamDuration(EufyMaxControllerEntity, RestoreNumber):
    """Wie lange die Kameras nach dem Buttondruck laufen."""

    _attr_name = "Livestream Dauer"
    _attr_icon = "mdi:timer-outline"
    _attr_unique_id = "eufy_max_stream_duration"
    _attr_native_min_value = MIN_STREAM_DURATION
    _attr_native_max_value = MAX_STREAM_DURATION
    _attr_native_step = 10
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    async def async_added_to_hass(self) -> None:
        """Gespeicherten Wert nach einem Neustart wiederherstellen."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self.controller.set_duration(int(last.native_value))

    @property
    def native_value(self) -> float:
        """Aktuell eingestellte Dauer."""
        return self.controller.duration

    async def async_set_native_value(self, value: float) -> None:
        """Neue Dauer setzen."""
        self.controller.set_duration(int(value))
        self.async_write_ha_state()


class EufyMaxNumber(EufyMaxPropertyEntity, NumberEntity):
    """Ein numerischer Parameter, z.B. Empfindlichkeit oder Helligkeit."""

    def __init__(self, client, serial, prop, meta) -> None:
        """Grenzen aus den Metadaten uebernehmen."""
        super().__init__(client, serial, prop, meta)
        self._attr_native_min_value = meta.get("min", 0)
        self._attr_native_max_value = meta.get("max", 100)
        self._attr_native_step = meta.get("steps", 1)
        self._attr_native_unit_of_measurement = meta.get("unit")

    async def async_set_native_value(self, value: float) -> None:
        """Wert setzen."""
        await self.client.async_set_property(self.serial, self.prop, int(value))
