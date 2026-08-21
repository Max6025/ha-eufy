"""Auswahllisten: Eufy-Modi je Station plus Geraeteeinstellungen.

Die Modi sind dieselben wie in der Eufy-App. Sie haengen an derselben
Stationseigenschaft wie das Alarmpanel - dadurch sind App, Panel und
Auswahlliste automatisch synchron.
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    GUARD_AWAY,
    GUARD_CUSTOM1,
    GUARD_CUSTOM2,
    GUARD_CUSTOM3,
    GUARD_DISARMED,
    GUARD_GEO,
    GUARD_HOME,
    GUARD_MODE_PROPERTY,
    GUARD_SCHEDULE,
    IGNORED_PROPERTIES,
    SIGNAL_DEVICE_UPDATE,
)
from .entity import EufyMaxPropertyEntity
from .websocket import EufyMaxClient

_LOGGER = logging.getLogger(__name__)

# Genau die Modi, die es auch in der Eufy-App gibt.
GUARD_OPTIONS = {
    "Abwesend": GUARD_AWAY,
    "Zuhause": GUARD_HOME,
    "Zeitplan": GUARD_SCHEDULE,
    "Geofence": GUARD_GEO,
    "Eigener Modus 1": GUARD_CUSTOM1,
    "Eigener Modus 2": GUARD_CUSTOM2,
    "Eigener Modus 3": GUARD_CUSTOM3,
    "Unscharf": GUARD_DISARMED,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Alle Auswahllisten anlegen."""
    client: EufyMaxClient = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = []

    # Eufy-Modus je Station
    for serial in client.stations:
        entities.append(EufyMaxGuardModeSelect(client, serial))

    # Alles, was das Geraet selbst an Auswahlmoeglichkeiten meldet
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


class EufyMaxGuardModeSelect(Entity, SelectEntity):
    """Der Eufy-Modus einer Station - dieselben Modi wie in der App."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Eufy Modus"
    _attr_icon = "mdi:shield-account"
    _attr_options = list(GUARD_OPTIONS)

    def __init__(self, client: EufyMaxClient, serial: str) -> None:
        """Auswahlliste initialisieren."""
        self.client = client
        self.serial = serial
        self._attr_unique_id = f"{serial}_guard_mode"

    @property
    def device_info(self) -> DeviceInfo:
        """An das Geraet haengen, das zur Station gehoert."""
        device = self.client.get_device(self.serial)
        return DeviceInfo(
            identifiers={(DOMAIN, self.serial)},
            name=device.get("name", self.serial),
            manufacturer="Anker Eufy",
            model=device.get("model", "Station"),
            serial_number=self.serial,
        )

    @property
    def available(self) -> bool:
        """Verfuegbar, solange die Verbindung steht."""
        return self.client.connected and self.client.driver_connected

    async def async_added_to_hass(self) -> None:
        """Auf Aenderungen der Station hoeren - auch aus der App."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_DEVICE_UPDATE}_{self.serial}",
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        """Neu zeichnen."""
        self.async_write_ha_state()

    @property
    def current_option(self) -> str | None:
        """Aktueller Modus als Klartext."""
        mode = self.client.get_station_property(self.serial, GUARD_MODE_PROPERTY)
        if mode is None:
            return None
        for label, value in GUARD_OPTIONS.items():
            if value == int(mode):
                return label
        return None

    async def async_select_option(self, option: str) -> None:
        """Modus setzen - wirkt auch in der Eufy-App."""
        mode = GUARD_OPTIONS.get(option)
        if mode is None:
            return
        await self.client.async_set_guard_mode(self.serial, mode)
        self.async_write_ha_state()


class EufyMaxSelect(EufyMaxPropertyEntity, SelectEntity):
    """Ein Parameter mit fester Auswahl, z.B. Erkennungstyp."""

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
                await self.client.async_set_property(
                    self.serial, self.prop, int(key)
                )
                return
