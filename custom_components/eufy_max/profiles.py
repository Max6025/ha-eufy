"""Modus-Profile und verzoegertes Scharfschalten.

Das Sammelpanel kennt drei Lagen: Zuhause, Abwesend und Schlafen. Welchen
Eufy-Modus jede einzelne Kamera in einer Lage bekommt, entscheidet nicht
das Panel, sondern Max: einstellen, dann speichern.

Gespeichert wird NIE automatisch. Wer nachtraeglich eine Kamera umstellt,
aendert das Profil nicht - bis er wieder ausdruecklich speichert.

Zusaetzlich gibt es eine Vorlaufzeit fuer den Weg nach draussen: Wer auf
Abwesend schaltet, hat noch die eingestellten Sekunden, um aus dem Haus
zu kommen, ohne selbst die Kamera auszuloesen.

Nur dieser eine Weg bekommt den Countdown. Home Assistant zaehlt zwar
auch Zuhause und Schlafen als Scharfschaltung, aber dabei bleibt man ja
im Haus - da soll sofort geschaltet werden. Unscharf wirkt ebenfalls
immer sofort und bricht einen laufenden Countdown ab.
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
    GUARD_GEO,
    GUARD_HOME,
    GUARD_MODE_NAMES,
    GUARD_MODE_PROPERTY,
    GUARD_SCHEDULE,
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

# Modi, bei denen Eufy selbst entscheidet, was gerade gilt. Steht so
# einer im Profil, ist jeder gemeldete Modus in Ordnung - Zeitplan und
# Geofence wechseln von allein, dagegen soll niemand anlaufen.
SELBSTSTAENDIGE_MODI = (GUARD_SCHEDULE, GUARD_GEO)

# Lagen, bei denen vor dem Scharfschalten eine Vorlaufzeit laeuft.
#
# Das ist ausschliesslich der Weg nach draussen. Home Assistant sieht
# Zuhause und Schlafen zwar ebenfalls als Scharfschaltung an, aber dabei
# bleibt man im Haus: Ein Countdown wuerde dort nur die Anlage unnoetig
# spaet scharf machen, ohne dass ihn jemand braucht.
LAGEN_MIT_VORLAUF = (PROFILE_AWAY,)


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

    def erwarteter_modus(self, serial: str) -> int | None:
        """Welchen Modus diese Station in der aktiven Lage haben sollte.

        None, wenn gerade keine Lage aktiv ist - dann gibt es auch
        nichts, woran man die Kamera messen koennte.
        """
        if self.aktiv not in PROFILE_LAGEN:
            return None
        gespeichert = self.profile.get(self.aktiv, {})
        return gespeichert.get(serial, STANDARD_MODUS.get(self.aktiv, GUARD_HOME))

    def abweichungen(self) -> list[dict[str, str | None]]:
        """Kameras, die nicht auf dem Modus der aktiven Lage stehen.

        Ausgenommen sind Kameras, deren Profil einen selbststaendigen
        Modus (Zeitplan, Geofence) vorsieht, und solche, fuer die die
        Nachkontrolle noch laeuft. Waehrend einer Vorlaufzeit gilt noch
        die alte Lage - die Kameras stehen ja auch noch so.
        """
        befunde: list[dict[str, str | None]] = []

        for serial in self.client.stations:
            soll = self.erwarteter_modus(serial)
            if soll is None or soll in SELBSTSTAENDIGE_MODI:
                continue
            if self.client.guard_change_running(serial):
                continue

            ist = self.client.get_station_property(serial, GUARD_MODE_PROPERTY)
            if ist is not None and int(ist) == soll:
                continue

            befunde.append(
                {
                    "kamera": self.client.get_station(serial).get("name", serial),
                    "station": serial,
                    "soll": GUARD_MODE_NAMES.get(soll, str(soll)),
                    "ist": (
                        GUARD_MODE_NAMES.get(int(ist), str(ist))
                        if ist is not None
                        else None
                    ),
                }
            )

        return befunde

    def braucht_vorlauf(self, lage: str) -> bool:
        """Laeuft vor dieser Lage eine Vorlaufzeit?

        Nur beim Wechsel nach Abwesend - und auch dann nur, wenn die
        Anlage nicht ohnehin schon abwesend ist. Ein erneuter Druck auf
        dieselbe Lage stellt lediglich die gespeicherten Modi wieder her;
        dafuer muss niemand aus dem Haus.
        """
        if lage not in LAGEN_MIT_VORLAUF:
            return False
        return self.aktiv != lage

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

        # Nur das Speichern ohne Angabe betrifft die laufende Lage - da
        # ist das Ziel ohnehin die aktive. Ein Knopf mit Lage im Namen
        # legt dagegen bloss etwas ab; er schaltet nichts, also darf er
        # auch nicht die Anzeige des Sammelpanels umspringen lassen.
        if lage is None:
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

        Die Vorlaufzeit gilt nur fuer die Lagen in LAGEN_MIT_VORLAUF,
        also fuer den Weg nach draussen. Zuhause und Schlafen schalten
        sofort, auch wenn am Regler eine Zeit steht. Wer ausdruecklich
        eine Verzoegerung uebergibt, bekommt sie in jedem Fall.

        Bei einer Vorlaufzeit groesser null wird nur vorgemerkt; das Panel
        zeigt so lange "Wird scharf geschaltet" und der Countdown-Sensor
        laeuft. Rueckgabe ist die Fehlerliste des sofortigen Schaltens -
        bei vorgemerktem Wechsel also immer leer.
        """
        if verzoegerung is None:
            sekunden = self.verzoegerung if self.braucht_vorlauf(lage) else 0
        else:
            sekunden = int(verzoegerung)

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
            # Kein Profil hinterlegt: Es passiert zwar etwas, aber eben
            # nur der Notnagel. Stand die Anlage ohnehin schon so, sieht
            # es von aussen aus, als haette der Knopf nichts bewirkt -
            # deshalb hier eine Warnung statt einer stillen Notiz.
            _LOGGER.warning(
                "Fuer die Lage '%s' ist noch kein Profil gespeichert. "
                "Alle Kameras laufen deshalb auf '%s'. Zum Einrichten: "
                "Modi je Kamera einstellen, dann den Knopf 'Modi "
                "speichern als %s' druecken.",
                PROFILE_NAMES.get(lage, lage),
                GUARD_MODE_NAMES.get(standard, standard),
                PROFILE_NAMES.get(lage, lage),
            )

        return fehler
