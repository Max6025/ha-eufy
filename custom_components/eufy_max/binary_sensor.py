"""Ereignis-Sensoren: Bewegung, Person, Tier, Klingeln.

Der Server liefert Erkennungen auf zwei Wegen: als benanntes Ereignis
("person detected") und als Eigenschaftsaenderung ("personDetected").
Welcher Weg genutzt wird, haengt vom Modell und der Firmware ab -
deshalb werden immer beide Schluessel geprueft.

Bei ganz neuen Modellen kommt ueber den Push-Kanal teils gar nichts an.
Fuer die gibt es zusaetzlich einen Sensor, der anschlaegt, sobald Eufy
ein neues Ereignisbild meldet. Das ist langsamer als Push, aber es
zeigt zuverlaessig, dass die Kamera ausgeloest hat.

Am Steuerungsgeraet haengt ausserdem die Kontrollleuchte "Modus weicht
ab": Sie geht an, sobald eine Kamera nicht auf dem Modus steht, den die
aktive Lage des Sammelpanels vorsieht. Niemand kann sie von Hand
schalten - sie zeigt nur, was ist.
"""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import (
    DOMAIN,
    HUB_IDENTIFIER,
    PROFILE_NAMES,
    SIGNAL_ARM_STATE,
    SIGNAL_DEVICE_UPDATE,
    SIGNAL_PROFILE_UPDATE,
)
from .entity import EufyMaxEntity
from .stream import camera_serials
from .websocket import EufyMaxClient

_LOGGER = logging.getLogger(__name__)

# Wie lange der Ereignis-Sensor nach einem neuen Bild aktiv bleibt
EVENT_HOLD_SECONDS = 60

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
    entities: list[BinarySensorEntity] = []

    for serial in client.devices:
        metadata = client.get_metadata(serial)
        for key, (label, device_class, prop) in EVENTS.items():
            if prop in metadata or key in ("motion_detected", "person_detected"):
                entities.append(
                    EufyMaxBinarySensor(
                        client, serial, key, prop, label, device_class
                    )
                )

    # Zusaetzlicher Ausloese-Sensor fuer jede Kamera
    for serial in camera_serials(client):
        entities.append(EufyMaxTriggerSensor(client, serial))

    if client.stations:
        entities.append(EufyMaxModeMismatchSensor(client))

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


class EufyMaxTriggerSensor(EufyMaxEntity, BinarySensorEntity):
    """Schlaegt an, sobald Eufy ein neues Ereignisbild meldet.

    Gedacht fuer Kameras, deren Push-Meldungen nicht ankommen. Die
    Aussage ist bewusst schlicht: Die Kamera hat ausgeloest. Ob Person,
    Tier oder Fahrzeug steht hier nicht - dafuer sind die anderen
    Sensoren zustaendig, sofern das Modell sie meldet.
    """

    _attr_name = "Ausgeloest"
    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(self, client: EufyMaxClient, serial: str) -> None:
        """Sensor initialisieren."""
        super().__init__(client, serial)
        self._attr_unique_id = f"{serial}_ausgeloest"
        self._letztes_bild: str | None = None
        self._aktiv: bool = False
        self._unsub = None

    async def async_added_to_hass(self) -> None:
        """Ausgangswert merken, damit der Start nicht ausloest."""
        await super().async_added_to_hass()
        self._letztes_bild = self.get_property("pictureUrl")

    @property
    def is_on(self) -> bool:
        """Hat die Kamera kuerzlich ausgeloest?"""
        return self._aktiv

    @callback
    def _handle_update(self) -> None:
        """Auf ein neues Ereignisbild reagieren."""
        bild = self.get_property("pictureUrl")

        if bild and bild != self._letztes_bild:
            self._letztes_bild = bild
            self._aktiv = True
            _LOGGER.debug("%s hat ausgeloest (neues Ereignisbild)", self.serial)

            if self._unsub is not None:
                self._unsub()

            @callback
            def _zuruecksetzen(_now) -> None:
                self._unsub = None
                self._aktiv = False
                self.async_write_ha_state()

            self._unsub = async_call_later(
                self.hass, EVENT_HOLD_SECONDS, _zuruecksetzen
            )

        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Timer aufraeumen."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None


class EufyMaxModeMismatchSensor(BinarySensorEntity):
    """Kontrollleuchte: Steht jede Kamera auf dem Modus ihrer Lage?

    Verglichen wird der gemeldete Guard Mode jeder Station mit dem, was
    das Profil der aktiven Lage fuer sie vorsieht. Sobald eine abweicht,
    geht die Leuchte an - rot in der Karte, weil Geraeteklasse "Problem".
    Welche Kamera es ist und was sie stattdessen meldet, steht in den
    Attributen.

    Ruhig bleibt sie, solange
    - keine Lage aktiv ist (nach Unscharf oder vor dem ersten Wechsel),
    - die Nachkontrolle eines Wechsels noch laeuft,
    - das Profil fuer die Kamera Zeitplan oder Geofence vorsieht.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Modus weicht ab"
    _attr_unique_id = "eufy_max_mode_mismatch"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:shield-alert"

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

    @property
    def available(self) -> bool:
        """Verfuegbar, solange die Verbindung steht."""
        return self.client.connected and self.client.driver_connected

    async def async_added_to_hass(self) -> None:
        """Auf jede Station, das Profil und den Wechselvorgang hoeren."""
        for serial in self.client.stations:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    f"{SIGNAL_DEVICE_UPDATE}_{serial}",
                    self._handle_update,
                )
            )
        for signal in (SIGNAL_PROFILE_UPDATE, SIGNAL_ARM_STATE):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, signal, self._handle_update)
            )

    @callback
    def _handle_update(self) -> None:
        """Neu zeichnen."""
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Weicht mindestens eine Kamera ab?"""
        profile = self.profile
        if profile is None:
            return False
        return bool(profile.abweichungen())

    @property
    def extra_state_attributes(self) -> dict:
        """Welche Lage gilt, wer abweicht und was gemeldet wird."""
        profile = self.profile
        if profile is None:
            return {}
        return {
            "lage": PROFILE_NAMES.get(profile.aktiv) if profile.aktiv else None,
            "abweichend": profile.abweichungen(),
        }
