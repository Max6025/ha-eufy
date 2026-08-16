"""Licht - fuer Floodlight-Kameras."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import EufyMaxEntity
from .websocket import EufyMaxClient

BRIGHTNESS_PROPERTIES = (
    "lightSettingsBrightnessManual",
    "lightSettingsBrightnessMotion",
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Licht anlegen, wenn das Geraet eines hat."""
    client: EufyMaxClient = hass.data[DOMAIN][entry.entry_id]
    entities: list[EufyMaxLight] = []

    for serial in client.devices:
        metadata = client.get_metadata(serial)
        if "light" in metadata:
            brightness_prop = next(
                (p for p in BRIGHTNESS_PROPERTIES if p in metadata), None
            )
            entities.append(EufyMaxLight(client, serial, brightness_prop))

    async_add_entities(entities)


class EufyMaxLight(EufyMaxEntity, LightEntity):
    """Der Flutlichtstrahler der Kamera."""

    _attr_name = "Licht"

    def __init__(self, client, serial, brightness_prop) -> None:
        """Licht initialisieren."""
        super().__init__(client, serial)
        self._brightness_prop = brightness_prop
        self._attr_unique_id = f"{serial}_light"
        if brightness_prop:
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        else:
            self._attr_color_mode = ColorMode.ONOFF
            self._attr_supported_color_modes = {ColorMode.ONOFF}

    @property
    def is_on(self) -> bool:
        """Licht an?"""
        return bool(self.get_property("light", False))

    @property
    def brightness(self) -> int | None:
        """Helligkeit 0-255 aus Eufys 0-100 umgerechnet."""
        if not self._brightness_prop:
            return None
        value = self.get_property(self._brightness_prop)
        if value is None:
            return None
        return int(value * 255 / 100)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Licht einschalten, optional mit Helligkeit."""
        if ATTR_BRIGHTNESS in kwargs and self._brightness_prop:
            percent = int(kwargs[ATTR_BRIGHTNESS] * 100 / 255)
            await self.client.async_set_property(
                self.serial, self._brightness_prop, max(1, percent)
            )
        await self.client.async_set_property(self.serial, "light", True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Licht ausschalten."""
        await self.client.async_set_property(self.serial, "light", False)
