"""Ereignis-Sensoren: Bewegung, Person, Tier, Klingeln.

Der Server liefert dasselbe Ereignis auf zwei Wegen: als benanntes
Ereignis ("person detected") und als Eigenschaftsaenderung
("personDetected"). Welcher Weg genutzt wird, haengt vom Modell und der
Firmware ab - deshalb werden hier immer beide Schluessel geprueft.
"""

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

# Ereignisschluessel -> (Anzeigename, Geraeteklasse, Eigenschaftsname)
EVENTS = {
    "motion_detected": (
        "Bewegung",
        BinarySensorDeviceClass.MOTION,
        "motionDetected",
    ),
    "person_detected": (
        "Person",
        BinarySensorDeviceClass.MOTION,
        "personDetected",
    ),
    "pet_detected": ("Tier", BinarySensorDeviceClass.MOTION, "petDetected"),
    "vehicle_detected": (
        "Fahrzeug",
        BinarySensorDeviceClass.MOTION,
        "vehicleDetected",
    ),
    "sound_detected": (
        "Geraeusch",
        BinarySensorDeviceClass.SOUND,
        "soundDetected",
    ),
    "crying_detected": (
        "Weinen",
        BinarySensorDeviceClass.SOUND,
        "cryingDetected",
    ),
    "package_delivered": ("Paket", None, "packageDelivered"),
    "rings": ("Klingel", None, "ringing"),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Ereignis-Sensoren fuer jedes Geraet anlegen."""
    client: EufyMaxClient = hass.data[DOMAIN][entry.entry_id]
    entities: list[EufyMaxBinarySensor] = []

    for serial in client.devices:
        metadata = client.get_metadata(serial)
        for key, (label, device_class, prop) in EVENTS.items():
            # Bewegung und Person gibt es bei jeder Kamera, der Rest nur,
            # wenn das Geraet die Eigenschaft meldet.
            if prop in metadata or key in ("motion_detected", "person_detected"):
                entities.append(
                    EufyMaxBinarySensor(
                        client, serial, key, prop, label, device_class
                    )
                )

    async_add_entities(entities)


class EufyMaxBinarySensor(EufyMaxEntity, BinarySensorEntity):
    """Ein Erkennungsereignis der Kamera."""

    def __init__(self, client, serial, key, prop, label, device_class) -> None:
        """Sensor initialisieren."""
        super().__init__(client, serial)
        self.key = key
        self.prop = prop
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_name = label
        self._attr_device_class = device_class

    @property
    def is_on(self) -> bool:
        """Ereignis aktiv? Beide Schreibweisen pruefen."""
        if self.get_property(self.prop):
            return True
        return bool(self.get_property(self.key, False))
