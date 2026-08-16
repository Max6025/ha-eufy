"""Alarm Panels.

Ohne HomeBase ist jede Kamera ihre eigene Station und hat einen eigenen
Guard Mode. Deshalb bekommt jede Kamera ein eigenes Panel, zusaetzlich
gibt es ein Sammelpanel fuer alle Kameras gleichzeitig.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CURRENT_MODE_PROPERTY,
    DOMAIN,
    GUARD_AWAY,
    GUARD_CUSTOM1,
    GUARD_CUSTOM2,
    GUARD_DISARMED,
    GUARD_GEO,
    GUARD_HOME,
    GUARD_MODE_NAMES,
    GUARD_MODE_PROPERTY,
    GUARD_OFF,
    GUARD_SCHEDULE,
    HUB_IDENTIFIER,
    MOTION_DETECTION_PROPERTY,
    SIGNAL_DEVICE_UPDATE,
)
from .entity import EufyMaxEntity
from .websocket import EufyMaxClient

_LOGGER = logging.getLogger(__name__)

# HA hat die Zustaende ab 2024.11 auf ein Enum umgestellt.
try:
    from homeassistant.components.alarm_control_panel import AlarmControlPanelState

    STATE_DISARMED = AlarmControlPanelState.DISARMED
    STATE_ARMED_AWAY = AlarmControlPanelState.ARMED_AWAY
    STATE_ARMED_HOME = AlarmControlPanelState.ARMED_HOME
    STATE_ARMED_NIGHT = AlarmControlPanelState.ARMED_NIGHT
    STATE_ARMED_VACATION = AlarmControlPanelState.ARMED_VACATION
    STATE_ARMED_CUSTOM = AlarmControlPanelState.ARMED_CUSTOM_BYPASS
    STATE_TRIGGERED = AlarmControlPanelState.TRIGGERED
    USE_ENUM = True
except ImportError:  # pragma: no cover - aeltere HA-Versionen
    from homeassistant.const import (  # type: ignore
        STATE_ALARM_ARMED_AWAY as STATE_ARMED_AWAY,
        STATE_ALARM_ARMED_CUSTOM_BYPASS as STATE_ARMED_CUSTOM,
        STATE_ALARM_ARMED_HOME as STATE_ARMED_HOME,
        STATE_ALARM_ARMED_NIGHT as STATE_ARMED_NIGHT,
        STATE_ALARM_ARMED_VACATION as STATE_ARMED_VACATION,
        STATE_ALARM_DISARMED as STATE_DISARMED,
        STATE_ALARM_TRIGGERED as STATE_TRIGGERED,
    )

    USE_ENUM = False

# Guard Mode -> HA-Zustand
MODE_TO_STATE = {
    GUARD_AWAY: STATE_ARMED_AWAY,
    GUARD_HOME: STATE_ARMED_HOME,
    GUARD_SCHEDULE: STATE_ARMED_CUSTOM,
    GUARD_CUSTOM1: STATE_ARMED_NIGHT,
    GUARD_CUSTOM2: STATE_ARMED_VACATION,
    GUARD_GEO: STATE_ARMED_VACATION,
    GUARD_OFF: STATE_DISARMED,
    GUARD_DISARMED: STATE_DISARMED,
}

SUPPORTED = (
    AlarmControlPanelEntityFeature.ARM_HOME
    | AlarmControlPanelEntityFeature.ARM_AWAY
    | AlarmControlPanelEntityFeature.ARM_NIGHT
    | AlarmControlPanelEntityFeature.ARM_VACATION
    | AlarmControlPanelEntityFeature.ARM_CUSTOM_BYPASS
    | AlarmControlPanelEntityFeature.TRIGGER
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Panels anlegen: eines je Kamera plus ein Sammelpanel."""
    client: EufyMaxClient = hass.data[DOMAIN][entry.entry_id]
    entities: list[AlarmControlPanelEntity] = []

    for serial in client.devices:
        entities.append(EufyMaxCameraAlarmPanel(client, serial))

    if entities:
        entities.append(EufyMaxMasterAlarmPanel(client))

    async_add_entities(entities)


class EufyMaxAlarmBase(AlarmControlPanelEntity):
    """Gemeinsame Logik fuer alle Panels."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_code_arm_required = False
    _attr_supported_features = SUPPORTED

    if not USE_ENUM:
        _attr_state: str | None = None

    def _set_state(self, state) -> None:
        """Zustand je nach HA-Version setzen."""
        if USE_ENUM:
            self._attr_alarm_state = state
        else:
            self._attr_state = state


class EufyMaxCameraAlarmPanel(EufyMaxEntity, EufyMaxAlarmBase):
    """Panel fuer eine einzelne Kamera.

    Hat die Kamera eine eigene Station (bei dir der Fall, da kein
    HomeBase), wird der echte Guard Mode geschaltet. Sonst wird als
    Ersatz die Bewegungserkennung geschaltet.
    """

    _attr_name = "Alarm"

    def __init__(self, client: EufyMaxClient, serial: str) -> None:
        """Panel initialisieren."""
        super().__init__(client, serial)
        self._attr_unique_id = f"{serial}_alarm_panel"
        # Eigenstaendige Kamera = eigene Station mit gleicher Seriennummer
        self._own_station = serial in client.stations

    @property
    def _station_serial(self) -> str | None:
        """Zugehoerige Station."""
        if self._own_station:
            return self.serial
        return self.device.get("stationSerialNumber")

    async def async_added_to_hass(self) -> None:
        """Zusaetzlich auf Updates der Station hoeren."""
        await super().async_added_to_hass()
        station = self._station_serial
        if station and station != self.serial:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    f"{SIGNAL_DEVICE_UPDATE}_{station}",
                    self._handle_update,
                )
            )

    @property
    def alarm_state(self):
        """Aktueller Zustand (HA 2024.11+)."""
        return self._current_state()

    @property
    def state(self):
        """Aktueller Zustand (aeltere HA-Versionen)."""
        return self._current_state()

    def _current_state(self):
        """Zustand aus Guard Mode oder Bewegungserkennung ableiten."""
        if self.get_property("alarm", False):
            return STATE_TRIGGERED

        station = self._station_serial
        if station:
            mode = self.client.get_station_property(station, GUARD_MODE_PROPERTY)
            if mode is None:
                mode = self.client.get_station_property(
                    station, CURRENT_MODE_PROPERTY
                )
            if mode is not None:
                return MODE_TO_STATE.get(int(mode), STATE_DISARMED)

        # Ersatzlogik ohne eigene Station
        if self.get_property(MOTION_DETECTION_PROPERTY):
            return STATE_ARMED_AWAY
        return STATE_DISARMED

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Rohwerte fuer Automatisierungen."""
        station = self._station_serial
        mode = (
            self.client.get_station_property(station, GUARD_MODE_PROPERTY)
            if station
            else None
        )
        return {
            "serial_number": self.serial,
            "station": station,
            "eigene_station": self._own_station,
            "guard_mode": mode,
            "guard_mode_name": GUARD_MODE_NAMES.get(mode) if mode is not None else None,
        }

    async def _async_set_mode(self, mode: int) -> None:
        """Modus setzen - echter Guard Mode oder Ersatzlogik."""
        station = self._station_serial
        if station:
            try:
                await self.client.async_set_guard_mode(station, mode)
                return
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Guard Mode fuer %s nicht setzbar (%s), nutze Bewegungserkennung",
                    self.serial,
                    err,
                )

        scharf = mode not in (GUARD_DISARMED, GUARD_OFF)
        await self.client.async_set_property(
            self.serial, MOTION_DETECTION_PROPERTY, scharf
        )

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Unscharf schalten."""
        await self._async_set_mode(GUARD_DISARMED)

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Abwesend."""
        await self._async_set_mode(GUARD_AWAY)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Zuhause."""
        await self._async_set_mode(GUARD_HOME)

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        """Erster eigener Modus aus der Eufy-App."""
        await self._async_set_mode(GUARD_CUSTOM1)

    async def async_alarm_arm_vacation(self, code: str | None = None) -> None:
        """Zweiter eigener Modus aus der Eufy-App."""
        await self._async_set_mode(GUARD_CUSTOM2)

    async def async_alarm_arm_custom_bypass(self, code: str | None = None) -> None:
        """Zeitplan aus der Eufy-App."""
        await self._async_set_mode(GUARD_SCHEDULE)

    async def async_alarm_trigger(self, code: str | None = None) -> None:
        """Sirene ausloesen."""
        station = self._station_serial
        if station:
            await self.client.async_trigger_station_alarm(station, 30)


class EufyMaxMasterAlarmPanel(EufyMaxAlarmBase):
    """Sammelpanel: schaltet alle Kameras gleichzeitig."""

    _attr_name = "Alarm alle Kameras"
    _attr_unique_id = "eufy_max_master_alarm"
    _attr_icon = "mdi:shield-home"

    def __init__(self, client: EufyMaxClient) -> None:
        """Panel initialisieren."""
        self.client = client

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

    @property
    def available(self) -> bool:
        """Verfuegbar, solange die Verbindung steht."""
        return self.client.connected and self.client.driver_connected

    async def async_added_to_hass(self) -> None:
        """Auf alle Stationen hoeren."""
        for serial in self.client.stations:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    f"{SIGNAL_DEVICE_UPDATE}_{serial}",
                    self._handle_update,
                )
            )

    @callback
    def _handle_update(self) -> None:
        """Neu zeichnen."""
        self.async_write_ha_state()

    def _current_state(self):
        """Gemeinsamer Zustand - nur einheitlich, wenn alle gleich sind."""
        modes = {
            self.client.get_station_property(serial, GUARD_MODE_PROPERTY)
            for serial in self.client.stations
        }
        modes.discard(None)

        if not modes:
            return STATE_DISARMED
        if len(modes) == 1:
            return MODE_TO_STATE.get(int(next(iter(modes))), STATE_DISARMED)
        # Uneinheitlich: sobald irgendwas scharf ist, gilt scharf.
        if any(int(m) not in (GUARD_DISARMED, GUARD_OFF) for m in modes):
            return STATE_ARMED_CUSTOM
        return STATE_DISARMED

    @property
    def alarm_state(self):
        """Aktueller Zustand (HA 2024.11+)."""
        return self._current_state()

    @property
    def state(self):
        """Aktueller Zustand (aeltere HA-Versionen)."""
        return self._current_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Einzelmodi zur Kontrolle."""
        return {
            "stationen": {
                serial: GUARD_MODE_NAMES.get(
                    self.client.get_station_property(serial, GUARD_MODE_PROPERTY)
                )
                for serial in self.client.stations
            }
        }

    async def _async_set_all(self, mode: int) -> None:
        """Modus auf allen Stationen setzen."""
        for serial in self.client.stations:
            try:
                await self.client.async_set_guard_mode(serial, mode)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Guard Mode fuer Station %s: %s", serial, err)

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Alles unscharf."""
        await self._async_set_all(GUARD_DISARMED)

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Alles auf abwesend."""
        await self._async_set_all(GUARD_AWAY)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Alles auf zuhause."""
        await self._async_set_all(GUARD_HOME)

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        """Alles auf den ersten eigenen Modus."""
        await self._async_set_all(GUARD_CUSTOM1)

    async def async_alarm_arm_vacation(self, code: str | None = None) -> None:
        """Alles auf den zweiten eigenen Modus."""
        await self._async_set_all(GUARD_CUSTOM2)

    async def async_alarm_arm_custom_bypass(self, code: str | None = None) -> None:
        """Alles auf Zeitplan."""
        await self._async_set_all(GUARD_SCHEDULE)

    async def async_alarm_trigger(self, code: str | None = None) -> None:
        """Alle Sirenen ausloesen."""
        for serial in self.client.stations:
            await self.client.async_trigger_station_alarm(serial, 30)
