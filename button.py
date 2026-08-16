"""Buttons: Livestream-Steuerung, Schwenken, Neigen, Alarm."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, PTZ_DIRECTIONS
from .controller_entity import EufyMaxControllerEntity
from .entity import EufyMaxEntity
from .websocket import EufyMaxClient

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
    ]

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
