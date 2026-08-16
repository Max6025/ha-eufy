"""Auswahllisten - aus allen Properties mit definierten Zustaenden."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, IGNORED_PROPERTIES
from .entity import EufyMaxPropertyEntity
from .websocket import EufyMaxClient


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Alle Auswahllisten anlegen."""
    client: EufyMaxClient = hass.data[DOMAIN][entry.entry_id]
    entities: list[EufyMaxSelect] = []

    for serial in client.devices:
        for prop, meta in client.get_metadata(serial).items():
            if prop in IGNORED_PROPERTIES:
                continue
            if not meta.get("writeable"):
                continue
            if not meta.get("states"):
                continue
            entities.append(EufyMaxSelect(client, serial, prop, meta))

    async_add_entities(entities)


class EufyMaxSelect(EufyMaxPropertyEntity, SelectEntity):
    """Ein Parameter mit fester Auswahl, z.B. Erkennungstyp oder Modus."""

    def __init__(self, client, serial, prop, meta) -> None:
        """Zustandsliste aus den Metadaten uebernehmen."""
        super().__init__(client, serial, prop, meta)
        self._states: dict[str, str] = {
            str(key): str(value) for key, value in meta.get("states", {}).items()
        }
        self._attr_options = list(self._states.values())

    @property
    def current_option(self) -> str | None:
        """Aktuelle Auswahl als Klartext."""
        value = self.get_property(self.prop)
        if value is None:
            return None
        return self._states.get(str(value))

    async def async_select_option(self, option: str) -> None:
        """Auswahl setzen."""
        for key, label in self._states.items():
            if label == option:
                await self.client.async_set_property(self.serial, self.prop, int(key))
                return
