"""Buttons: Livestream, Schwenken, Alarm, Modi speichern."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    HUB_IDENTIFIER,
    PROFILE_HOME,
    PROFILE_LAGEN,
    PROFILE_NAMES,
    PTZ_DIRECTIONS,
    SIGNAL_PROFILE_UPDATE,
)
from .controller_entity import EufyMaxControllerEntity
from .entity import EufyMaxEntity
from .websocket import EufyMaxClient

_LOGGER = logging.getLogger(__name__)

PTZ_LABELS = {
    "left": "Schwenken links",
    "right": "Schwenken rechts",
    "up": "Neigen hoch",
    "down": "Neigen runter",
    "rotate360": "360 Grad Rundumblick",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Buttons anlegen."""
    client: EufyMaxClient = hass.data[DOMAIN][entry.entry_id]
    controller = client.stream
    entities: list[ButtonEntity] = [
        EufyMaxStartStreamButton(controller),
        EufyMaxStopStreamButton(controller),
        EufyMaxSaveProfileButton(client),
    ]

    # Je Lage ein eigener Knopf, damit man ein Profil einrichten kann,
    # ohne vorher umschalten zu muessen.
    entities.extend(
        EufyMaxSaveProfileButton(client, lage) for lage in PROFILE_LAGEN
    )

    for serial in client.devices:
        if client.has_command(serial, "device.pan_and_tilt"):
            for key, label in PTZ_LABELS.items():
                entities.append(EufyMaxPtzButton(client, serial, key, label))

        if client.has_command(serial, "device.trigger_alarm"):
            entities.append(EufyMaxAlarmButton(client, serial, True))
            entities.append(EufyMaxAlarmButton(client, serial, False))

    async_add_entities(entities)


class EufyMaxStartStreamButton(EufyMaxControllerEntity, ButtonEntity):
    """Startet den Livestream aller Kameras fuer die eingestellte Dauer."""

    _attr_name = "Livestream starten"
    _attr_icon = "mdi:cctv"
    _attr_unique_id = "eufy_max_start_stream"

    async def async_press(self) -> None:
        """Alle Kameras starten."""
        await self.controller.async_start()


class EufyMaxStopStreamButton(EufyMaxControllerEntity, ButtonEntity):
    """Stoppt den Livestream aller Kameras sofort."""

    _attr_name = "Livestream stoppen"
    _attr_icon = "mdi:cctv-off"
    _attr_unique_id = "eufy_max_stop_stream"

    async def async_press(self) -> None:
        """Alle Kameras stoppen."""
        await self.controller.async_stop()


class EufyMaxSaveProfileButton(ButtonEntity):
    """Speichert die aktuellen Modi aller Kameras als Profil.

    Ohne Lage schreibt der Knopf in die Lage, auf der das Sammelpanel
    gerade steht - der normale Weg im Alltag. Die Knoepfe mit Lage im
    Namen schreiben ausdruecklich dorthin.

    Gespeichert wird ausschliesslich hier. Wer eine Kamera nachtraeglich
    umstellt, aendert das Profil nicht.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:content-save-cog"

    def __init__(self, client: EufyMaxClient, lage: str | None = None) -> None:
        """Knopf initialisieren."""
        self.client = client
        self.lage = lage

        if lage is None:
            self._attr_name = "Modi speichern"
            self._attr_unique_id = "eufy_max_save_profile"
        else:
            self._attr_name = f"Modi speichern als {PROFILE_NAMES[lage]}"
            self._attr_unique_id = f"eufy_max_save_profile_{lage}"

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

    @property
    def available(self) -> bool:
        """Verfuegbar, solange die Verbindung steht."""
        return self.client.connected and self.client.driver_connected

    async def async_added_to_hass(self) -> None:
        """Auf Profilaenderungen hoeren, damit die Attribute stimmen."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_PROFILE_UPDATE, self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        """Neu zeichnen."""
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        """Zeigt, wohin gespeichert wird und was dort steht."""
        profile = self.profile
        if profile is None:
            return {}

        ziel = self.lage or profile.aktiv or PROFILE_HOME
        return {
            "ziel": PROFILE_NAMES.get(ziel, ziel),
            "gespeichert": profile.uebersicht(ziel),
        }

    async def async_press(self) -> None:
        """Aktuelle Modi ablegen."""
        profile = self.profile
        if profile is None:
            raise HomeAssistantError(
                "Profilspeicher nicht bereit - Integration neu laden"
            )

        modi = await profile.async_save(self.lage)

        if not modi:
            raise HomeAssistantError(
                "Keine Kamera hat einen Modus gemeldet - nichts gespeichert"
            )

        ziel = self.lage or profile.aktiv
        _LOGGER.info(
            "%s Kamera(s) fuer '%s' gespeichert",
            len(modi),
            PROFILE_NAMES.get(ziel, ziel),
        )
        self.async_write_ha_state()


class EufyMaxPtzButton(EufyMaxEntity, ButtonEntity):
    """Schwenk- und Neigebefehl."""

    def __init__(self, client, serial, key, label) -> None:
        """Button initialisieren."""
        super().__init__(client, serial)
        self.key = key
        self._attr_unique_id = f"{serial}_ptz_{key}"
        self._attr_name = label

    async def async_press(self) -> None:
        """Bewegung ausloesen."""
        await self.client.async_pan_and_tilt(self.serial, PTZ_DIRECTIONS[self.key])


class EufyMaxAlarmButton(EufyMaxEntity, ButtonEntity):
    """Sirene ausloesen oder stoppen."""

    def __init__(self, client, serial, trigger: bool) -> None:
        """Button initialisieren."""
        super().__init__(client, serial)
        self.trigger = trigger
        suffix = "trigger" if trigger else "reset"
        self._attr_unique_id = f"{serial}_alarm_{suffix}"
        self._attr_name = "Sirene ausloesen" if trigger else "Sirene aus"

    async def async_press(self) -> None:
        """Alarm schalten."""
        command = "device.trigger_alarm" if self.trigger else "device.reset_alarm"
        payload = {"command": command, "serialNumber": self.serial}
        if self.trigger:
            payload["seconds"] = 30
        await self.client.async_send_command(payload)
