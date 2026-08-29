"""Modus-Profile und verzoegertes Scharfschalten.

Das Sammelpanel kennt drei Lagen: Zuhause, Abwesend und Schlafen. Welchen
Eufy-Modus jede einzelne Kamera in einer Lage bekommt, entscheidet nicht
das Panel, sondern Max: einstellen, dann speichern.

Gespeichert wird NIE automatisch. Wer nachtraeglich eine Kamera umstellt,
aendert das Profil nicht - bis er wieder ausdruecklich speichert.

Zusaetzlich gibt es eine Verzoegerung: Nach dem Druck auf eine Lage laeuft
erst eine einstellbare Zeit ab, bevor die Modi gesetzt werden. So kommt
man noch aus dem Haus, ohne selbst die Kamera auszuloesen. Unscharf wirkt
immer sofort.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_ARM_DELAY,
    GUARD_AWAY,
    GUARD_HOME,
    GUARD_MODE_NAMES,
    GUARD_MODE_PROPERTY,
    PROFILE_AWAY,
    PROFILE_HOME,
    PROFILE_LAGEN,
    PROFILE_NAMES,
    PROFILE_SLEEP,
    SIGNAL_ARM_STATE,
    SIGNAL_PROFILE_UPDATE,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Womit eine Kamera bedient wird, fuer die es (noch) keinen Eintrag im
# Profil gibt - etwa weil sie nach dem Speichern dazugekommen ist.
# Zuhause ist dabei die vorsichtige Wahl: eher zu wenig scharf als eine
# Kamera, die unerwartet Alarm schlaegt.
STANDARD_MODUS = {
    PROFILE_HOME: GUARD_HOME,
    PROFILE_AWAY: GUARD_AWAY,
    PROFILE_SLEEP: GUARD_HOME,
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
            lage: {} for lage in PROFILE_LAGEN
        }
        # Zuletzt angewandte Lage. Bestimmt, was das Panel anzeigt und
        # wohin der Speichern-Knopf schreibt.
        self.aktiv: str | None = None
        # Vorlaufzeit in Sekunden, bevor scharf geschaltet wird
        self.verzoegerung: int = DEFAULT_ARM_DELAY

        # Laufende Verzoegerung
        self.pending_lage: str | None = None
        self.pending_bis: datetime | None = None
        self._unsub_timer = None

    # ------------------------------------------------------------------
    # Laden und Sichern
    # ------------------------------------------------------------------

    async def async_load(self) -> None:
        """Gespeicherte Profile einlesen."""
        daten = await self._store.async_load()
        if not daten:
            _LOGGER.debug("Noch keine Modus-Profile gespeichert")
            return

        for lage in PROFILE_LAGEN:
            eintrag = daten.get(lage) or {}
            self.profile[lage] = {
                str(serial): int(modus) for serial, modus in eintrag.items()
            }

        aktiv = daten.get("aktiv")
        if aktiv in PROFILE_LAGEN:
            self.aktiv = aktiv

        verzoegerung = daten.get("verzoegerung")
        if isinstance(verzoegerung, int):
            self.verzoegerung = verzoegerung

        _LOGGER.debug(
            "Modus-Profile geladen: %s, aktiv %s, Verzoegerung %s s",
            {lage: len(self.profile[lage]) for lage in PROFILE_LAGEN},
            self.aktiv,
            self.verzoegerung,
        )

    async def _async_write(self) -> None:
        """Profile auf die Platte schreiben."""
        daten = {lage: self.profile[lage] for lage in PROFILE_LAGEN}
        daten["aktiv"] = self.aktiv
        daten["verzoegerung"] = self.verzoegerung
        await self._store.async_save(daten)
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

    @property
    def laeuft(self) -> bool:
        """Laeuft gerade eine Verzoegerung?"""
        return self.pending_lage is not None

    @property
    def restzeit(self) -> int:
        """Verbleibende Sekunden bis zum Scharfschalten."""
        if self.pending_bis is None:
            return 0
        return max(0, int((self.pending_bis - dt_util.utcnow()).total_seconds()))

    def set_verzoegerung(self, sekunden: int) -> None:
        """Vorlaufzeit setzen."""
        self.verzoegerung = max(0, int(sekunden))
        self.hass.async_create_task(self._async_write())

    # ------------------------------------------------------------------
    # Speichern
    # ------------------------------------------------------------------

    async def async_save(self, lage: str | None = None) -> dict[str, int]:
        """Aktuelle Modi aller Kameras als Profil ablegen.

        Ohne Angabe wird in die zuletzt angewandte Lage gespeichert. Gab
        es die noch nie, wird Zuhause genommen.
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

    # ------------------------------------------------------------------
    # Verzoegertes Scharfschalten
    # ------------------------------------------------------------------

    async def async_request(
        self, lage: str, verzoegerung: int | None = None
    ) -> list[str]:
        """Lage herstellen - sofort oder nach Ablauf der Vorlaufzeit.

        Bei einer Vorlaufzeit groesser null wird nur vorgemerkt; das Panel
        zeigt so lange "Wird scharf geschaltet" und der Countdown-Sensor
        laeuft. Rueckgabe ist die Fehlerliste des sofortigen Schaltens -
        bei vorgemerktem Wechsel also immer leer.
        """
        sekunden = self.verzoegerung if verzoegerung is None else int(verzoegerung)

        # Eine bereits laufende Vormerkung wird ersetzt.
        self.cancel_pending(benachrichtigen=False)

        if sekunden <= 0:
            return await self.async_apply(lage)

        self.pending_lage = lage
        self.pending_bis = dt_util.utcnow() + timedelta(seconds=sekunden)

        @callback
        def _abgelaufen(_now) -> None:
            self._unsub_timer = None
            self.hass.async_create_task(self._async_finish())

        self._unsub_timer = async_call_later(self.hass, sekunden, _abgelaufen)

        _LOGGER.info(
            "'%s' wird in %s Sekunden scharf geschaltet",
            PROFILE_NAMES.get(lage, lage),
            sekunden,
        )
        async_dispatcher_send(self.hass, SIGNAL_ARM_STATE)
        return []

    async def _async_finish(self) -> None:
        """Vorgemerkte Lage nach Ablauf der Zeit anwenden."""
        lage = self.pending_lage
        self.pending_lage = None
        self.pending_bis = None

        if lage is None:
            return

        fehler = await self.async_apply(lage)
        if fehler:
            _LOGGER.warning(
                "Verzoegertes Scharfschalten teilweise fehlgeschlagen: %s",
                "; ".join(fehler),
            )

    @callback
    def cancel_pending(self, benachrichtigen: bool = True) -> None:
        """Laufende Vorlaufzeit abbrechen."""
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

        war_gesetzt = self.pending_lage is not None
        self.pending_lage = None
        self.pending_bis = None

        if war_gesetzt and benachrichtigen:
            async_dispatcher_send(self.hass, SIGNAL_ARM_STATE)

    # ------------------------------------------------------------------
    # Anwenden
    # ------------------------------------------------------------------

    async def async_apply(self, lage: str) -> list[str]:
        """Gespeichertes Profil einer Lage auf alle Kameras anwenden.

        Rueckgabe ist die Liste der Fehler - leer heisst, alles hat
        geklappt. Kameras ohne Eintrag im Profil bekommen den
        Standardmodus der Lage.
        """
        gespeichert = self.profile.get(lage, {})
        standard = STANDARD_MODUS.get(lage, GUARD_HOME)
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
        async_dispatcher_send(self.hass, SIGNAL_ARM_STATE)

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
