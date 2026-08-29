"""Sensoren: Stream-Restzeit, Scharfschalt-Countdown, Properties."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    HUB_IDENTIFIER,
    IGNORED_PROPERTIES,
    PROFILE_NAMES,
    SIGNAL_ARM_STATE,
)
from .controller_entity import EufyMaxControllerEntity
from .entity import EufyMaxPropertyEntity
from .websocket import EufyMaxClient

DEVICE_CLASSES = {
    "battery": (SensorDeviceClass.BATTERY, PERCENTAGE),
    "batteryTemperature": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    "wifiRssi": (SensorDeviceClass.SIGNAL_STRENGTH, "dBm"),
    "wifiSignalLevel": (None, None),
}

DIAGNOSTIC = {"wifiRssi", "wifiSignalLevel", "battery", "batteryTemperature",
              "chargingStatus", "softwareVersion", "hardwareVersion"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Alle Sensoren anlegen."""
    client: EufyMaxClient = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        EufyMaxStreamRemaining(client.stream),
        EufyMaxStreamEndsAt(client.stream),
        EufyMaxArmCountdown(client),
    ]

    for serial in client.devices:
        for prop, meta in client.get_metadata(serial).items():
            if prop in IGNORED_PROPERTIES:
                continue
            if meta.get("writeable"):
                continue
            if meta.get("type") == "boolean":
                continue
            entities.append(EufyMaxSensor(client, serial, prop, meta))

    async_add_entities(entities)


class EufyMaxStreamRemaining(EufyMaxControllerEntity, SensorEntity):
    """Zeigt sekundengenau, wie lange die Kameras noch laufen."""

    _attr_name = "Livestream Restzeit"
    _attr_icon = "mdi:timer-sand"
    _attr_unique_id = "eufy_max_stream_remaining"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class = SensorDeviceClass.DURATION

    async def async_added_to_hass(self) -> None:
        """Waehrend eines laufenden Streams jede Sekunde aktualisieren."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_tick, timedelta(seconds=1)
            )
        )

    @callback
    def _async_tick(self, _now) -> None:
        """Nur zeichnen, solange etwas laeuft."""
        if self.controller.active:
            self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        """Verbleibende Sekunden."""
        return self.controller.remaining


class EufyMaxStreamEndsAt(EufyMaxControllerEntity, SensorEntity):
    """Zeigt den Zeitpunkt der automatischen Abschaltung."""

    _attr_name = "Livestream endet um"
    _attr_icon = "mdi:clock-end"
    _attr_unique_id = "eufy_max_stream_ends_at"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        """Abschaltzeitpunkt."""
        return self.controller.ends_at


class EufyMaxArmCountdown(SensorEntity):
    """Countdown bis zum Scharfschalten.

    Die Standard-Alarmkarte zeigt waehrend der Vorlaufzeit nur
    "Wird scharf geschaltet", aber keine Sekunden. Dieser Sensor liefert
    sie - zum Danebenlegen aufs Dashboard oder fuer eine Ansage.
    Ausserhalb einer Vorlaufzeit steht er auf 0.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Scharfschaltung in"
    _attr_icon = "mdi:timer-alert-outline"
    _attr_unique_id = "eufy_max_arm_countdown"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class = SensorDeviceClass.DURATION

    def __init__(self, client: EufyMaxClient) -> None:
        """Sensor initialisieren."""
        self.client = client

    @property
    def profile(self):
        """Profilspeicher der Integration."""
        return getattr(self.client, "profile", None)

    @property
    def device_info(self) -> DeviceInfo:
        """Gehoert zum Steuerungsgeraet."""
        return DeviceInfo(
            identifiers={(DOMAIN, HUB_IDENTIFIER)},
            name="Eufy Max Steuerung",
            manufacturer="Max",
            model="Livestream Controller",
            entry_type="service",
        )

    async def async_added_to_hass(self) -> None:
        """Jede Sekunde zeichnen, solange eine Vorlaufzeit laeuft."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_ARM_STATE, self._handle_update
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_tick, timedelta(seconds=1)
            )
        )

    @callback
    def _handle_update(self) -> None:
        """Neu zeichnen."""
        self.async_write_ha_state()

    @callback
    def _async_tick(self, _now) -> None:
        """Nur zeichnen, solange etwas laeuft."""
        profile = self.profile
        if profile is not None and profile.laeuft:
            self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        """Verbleibende Sekunden bis zum Scharfschalten."""
        profile = self.profile
        return profile.restzeit if profile is not None else 0

    @property
    def extra_state_attributes(self) -> dict:
        """Worauf geschaltet wird."""
        profile = self.profile
        if profile is None:
            return {}

        lage = profile.pending_lage
        return {
            "laeuft": profile.laeuft,
            "ziel": PROFILE_NAMES.get(lage, lage),
            "vorlaufzeit": profile.verzoegerung,
        }


class EufyMaxSensor(EufyMaxPropertyEntity, SensorEntity):
    """Ein Messwert oder Statuswert des Geraets."""

    def __init__(self, client, serial, prop, meta) -> None:
        """Geraeteklasse und Einheit zuordnen."""
        super().__init__(client, serial, prop, meta)
        device_class, unit = DEVICE_CLASSES.get(prop, (None, meta.get("unit")))
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._states = {
            str(key): str(value) for key, value in (meta.get("states") or {}).items()
        }
        if prop in DIAGNOSTIC:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        """Wert, bei Zustandslisten als Klartext."""
        value = self.get_property(self.prop)
        if value is None:
            return None
        if self._states:
            return self._states.get(str(value), value)
        if isinstance(value, (dict, list)):
            return None
        return value
