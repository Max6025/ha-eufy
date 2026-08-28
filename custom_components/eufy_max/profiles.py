"""Modus-Profile fuer das Sammelpanel.

Gedanke dahinter: Das Sammelpanel kennt nur zwei Lagen - Zuhause und
Abwesend. Welchen Eufy-Modus jede einzelne Kamera in dieser Lage haben
soll, entscheidet aber nicht das Panel, sondern Max. Eine Kamera darf im
Zustand "Zuhause" auf Zeitplan stehen, die naechste auf Unscharf, die
dritte auf Abwesend.

Ablauf:
  1. Jede Kamera einzeln so einstellen, wie sie in dieser Lage sein soll.
  2. Auf "Modi speichern" druecken. Erst dann wird das Profil abgelegt.
  3. Beim naechsten Umschalten auf diese Lage wird genau das wieder
     hergestellt.

Wichtig: Es wird NIE automatisch gespeichert. Wer nachtraeglich eine
Kamera umstellt, aendert damit das Profil nicht - bis er wieder
ausdruecklich speichert.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import (
    GUARD_AWAY,
    GUARD_HOME,
    GUARD_MODE_NAMES,
    GUARD_MODE_PROPERTY,
    PROFILE_AWAY,
    PROFILE_HOME,
    PROFILE_NAMES,
    SIGNAL_PROFILE_UPDATE,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Womit eine Kamera bedient wird, fuer die es (noch) keinen Eintrag im
# Profil gibt - etwa weil sie nach dem Speichern dazugekommen ist.
STANDARD_MODUS = {
    PROFILE_HOME: GUARD_HOME,
    PROFILE_AWAY: GUARD_AWAY,
}


class ModusProfile:
    """Haelt je Lage einen Modus pro Kamera und wendet ihn an."""

    def __init__(self, hass: HomeAssistant, client) -> None:
        """Profilspeicher anlegen."""
        self.hass = hass
        self.client = client
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

        # Lage -> {Seriennummer der Station: Guard Mode}
        self.profile: dict[str, dict[str, int]] = {
            PROFILE_HOME: {},
            PROFILE_AWAY: {},
        }
        # Zuletzt angewandte Lage. Bestimmt, was das Panel anzeigt und
        # wohin der Speichern-Knopf schreibt.
        self.aktiv: str | None = None

    # ------------------------------------------------------------------
    # Laden und Sichern
    # ------------------------------------------------------------------

    async def async_load(self) -> None:
        """Gespeicherte Profile einlesen."""
        daten = await self._store.async_load()
        if not daten:
            _LOGGER.debug("Noch keine Modus-Profile gespeichert")
            return

        for lage in (PROFILE_HOME, PROFILE_AWAY):
            eintrag = daten.get(lage) or {}
            self.profile[lage] = {
                str(serial): int(modus) for serial, modus in eintrag.items()
            }

        aktiv = daten.get("aktiv")
        if aktiv in (PROFILE_HOME, PROFILE_AWAY):
            self.aktiv = aktiv

        _LOGGER.debug(
            "Modus-Profile geladen: zuhause %s Kamera(s), abwesend %s, aktiv %s",
            len(self.profile[PROFILE_HOME]),
            len(self.profile[PROFILE_AWAY]),
            self.aktiv,
        )

    async def _async_write(self) -> None:
        """Profile auf die Platte schreiben."""
        await self._store.async_save(
            {
                PROFILE_HOME: self.profile[PROFILE_HOME],
                PROFILE_AWAY: self.profile[PROFILE_AWAY],
                "aktiv": self.aktiv,
            }
        )
        async_dispatcher_send(self.hass, SIGNAL_PROFILE_UPDATE)

    # ------------------------------------------------------------------
    # Abfragen
    # ------------------------------------------------------------------

    def modi(self, lage: str) -> dict[str, int]:
        """Gespeicherte Modi einer Lage."""
        return dict(self.profile.get(lage, {}))

    def ist_gespeichert(self, lage: str) -> bool:
        """Wurde fuer diese Lage schon einmal gespeichert?"""
        return bool(self.profile.get(lage))

    def uebersicht(self, lage: str) -> dict[str, str]:
        """Lesbare Fassung eines Profils fuer die Attributanzeige."""
        namen = {}
        for serial, modus in self.profile.get(lage, {}).items():
            station = self.client.get_station(serial)
            bezeichnung = station.get("name") or serial
            namen[bezeichnung] = GUARD_MODE_NAMES.get(int(modus), str(modus))
        return namen

    # ------------------------------------------------------------------
    # Speichern und Anwenden
    # ------------------------------------------------------------------

    async def async_save(self, lage: str | None = None) -> dict[str, int]:
        """Aktuelle Modi aller Kameras als Profil ablegen.

        Ohne Angabe wird in die zuletzt angewandte Lage gespeichert. Gab
        es die noch nie, wird Zuhause genommen - das ist der Zustand, in
        dem die meisten anfangen einzurichten.
        """
        ziel = lage or self.aktiv or PROFILE_HOME

        modi: dict[str, int] = {}
        for serial in self.client.stations:
            modus = self.client.get_station_property(serial, GUARD_MODE_PROPERTY)
            if modus is None:
                _LOGGER.debug("%s meldet keinen Modus - wird uebersprungen", serial)
                continue
            modi[serial] = int(modus)

        self.profile[ziel] = modi
        self.aktiv = ziel
        await self._async_write()

        _LOGGER.info(
            "Modi fuer '%s' gespeichert: %s",
            PROFILE_NAMES.get(ziel, ziel),
            self.uebersicht(ziel),
        )
        return modi

    async def async_apply(self, lage: str) -> list[str]:
        """Gespeichertes Profil einer Lage auf alle Kameras anwenden.

        Rueckgabe ist die Liste der Fehler - leer heisst, alles hat
        geklappt. Kameras ohne Eintrag im Profil bekommen den
        Standardmodus der Lage.
        """
        gespeichert = self.profile.get(lage, {})
        standard = STANDARD_MODUS[lage]
        fehler: list[str] = []

        for serial in self.client.stations:
            modus = gespeichert.get(serial, standard)
            try:
                await self.client.async_set_guard_mode(serial, modus)
            except Exception as err:  # noqa: BLE001
                name = self.client.get_station(serial).get("name", serial)
                fehler.append(f"{name}: {err}")

        self.aktiv = lage
        await self._async_write()

        if gespeichert:
            _LOGGER.info(
                "Profil '%s' angewandt: %s",
                PROFILE_NAMES.get(lage, lage),
                self.uebersicht(lage),
            )
        else:
            _LOGGER.info(
                "Fuer '%s' ist noch nichts gespeichert - alle Kameras auf %s",
                PROFILE_NAMES.get(lage, lage),
                GUARD_MODE_NAMES.get(standard, standard),
            )

        return fehler
