"""Ereignis-Sensoren: Bewegung, Person, Tier, Klingeln."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import EufyMaxEntity
from .websocket import EufyMaxClient

EVENTS = {
    "motion_detected": ("Bewegung", BinarySensorDeviceClass.MOTION),
    "person_detected": ("Person", BinarySensorDeviceClass.MOTION),
    "pet_detected": ("Tier", BinarySensorDeviceClass.MOTION),
    "vehicle_detected": ("Fahrzeug", BinarySensorDeviceClass.MOTION),
    "sound_detected": ("Gerausch", BinarySensorDeviceClass.SOUND),
    "crying_detected": ("Weinen", BinarySensorDeviceClass.SOUND),
    "package_delivered": ("Paket", None),
    "rings": ("Klingel", None),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Ereignis-Sensoren fuer jedes Geraet anlegen."""
    client: EufyMaxClient = hass.data[DOMAIN][entry.entry_id]
    entities: list[EufyMaxBinarySensor] = []

    for serial in client.devices:
        metadata = client.get_metadata(serial)
        for key, (label, device_class) in EVENTS.items():
            prop = key.replace("_detected", "Detected").replace("_", "")
            # Sensor anlegen, wenn das Geraet die Faehigkeit meldet oder
            # der Ereignistyp generell zu Kameras gehoert.
            if prop in metadata or key in ("motion_detected", "person_detected"):
                entities.append(
                    EufyMaxBinarySensor(client, serial, key, label, device_class)
                )

    async_add_entities(entities)


class EufyMaxBinarySensor(EufyMaxEntity, BinarySensorEntity):
    """Ein Erkennungsereignis der Kamera."""

    def __init__(self, client, serial, key, label, device_class) -> None:
        """Sensor initialisieren."""
        super().__init__(client, serial)
        self.key = key
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_name = label
        self._attr_device_class = device_class

    @property
    def is_on(self) -> bool:
        """Ereignis aktiv?"""
        return bool(self.get_property(self.key, False))
