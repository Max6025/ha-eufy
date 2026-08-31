"""WebSocket-Client fuer eufy-security-ws.

Kernstueck der Integration. Haelt eine dauerhafte Verbindung zum
eufy-security-ws Server, faengt Verbindungsabbrueche selbst ab und stellt
den Geraetezustand als Dictionary bereit.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    COMMAND_TIMEOUT,
    DOMAIN,
    HEARTBEAT_INTERVAL,
    MAX_SCHEMA_VERSION,
    RECONNECT_MAX_DELAY,
    RECONNECT_MIN_DELAY,
)

_LOGGER = logging.getLogger(__name__)

# Akkukameras schlafen zwischen zwei Ereignissen.
WAKE_TIMEOUT = 12
WAKE_POLL = 0.5

# Nachkontrolle eines Moduswechsels
GUARD_MODE_TIMEOUT = 16
GUARD_MODE_POLL = 2

# Wie oft der Modus aller Stationen von sich aus nachgelesen wird.
STATION_POLL_INTERVAL = 60

# Nur jede n-te Runde wird die Kamera direkt befragt. Die direkte
# Abfrage kostet Zeit und belegt die Station - fuer die Anzeige reicht
# es, sie gelegentlich zu machen.
DEEP_POLL_EVERY = 5

# Wartezeit nach einer Geraeteabfrage, bis die Antwort verarbeitet ist.
CAMERA_INFO_DELAY = 3


class EufyMaxError(Exception):
    """Allgemeiner Fehler der Integration."""


class EufyMaxCommandError(EufyMaxError):
    """Der Server hat einen Befehl mit Fehler beantwortet."""


class EufyMaxClient:
    """Verwaltet die WebSocket-Verbindung und den Geraetezustand."""

    def __init__(self, hass: HomeAssistant, host: str, port: int) -> None:
        """Client initialisieren."""
        self.hass = hass
        self.host = host
        self.port = port
        self.url = f"ws://{host}:{port}"

        self.schema_version: int = MAX_SCHEMA_VERSION
        self.driver_connected: bool = False
        self.connected: bool = False

        # serialNumber -> Geraetezustand
        self.devices: dict[str, dict[str, Any]] = {}
        self.stations: dict[str, dict[str, Any]] = {}
        self.metadata: dict[str, dict[str, Any]] = {}
        self.station_metadata: dict[str, dict[str, Any]] = {}
        self.commands: dict[str, list[str]] = {}

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._message_id: int = 0
        self._futures: dict[str, asyncio.Future] = {}
        self._listeners: list[Callable[[str, str | None], None]] = []
        self._auth_listeners: list[Callable[[str, dict[str, Any]], None]] = []
        # serialNumber -> Empfaenger fuer rohe P2P-Videodaten
        self._video_handlers: dict[str, Callable[[bytes, str | None], None]] = {}
        # Damit nicht mehrere Befehle gleichzeitig dieselbe Kamera wecken
        self._wake_locks: dict[str, asyncio.Lock] = {}
        # Damit pro Station immer nur eine Nachkontrolle laeuft
        self._guard_locks: dict[str, asyncio.Lock] = {}
        self._guard_targets: dict[str, int] = {}
        # Wird von __init__.py gesetzt
        self.stream: Any = None
        self.profile: Any = None

        self._runner: asyncio.Task | None = None
        self._reader: asyncio.Task | None = None
        self._poller: asyncio.Task | None = None
        self._disconnected = asyncio.Event()
        self._closing: bool = False
        self._refreshing: bool = False

    # ------------------------------------------------------------------
    # Verbindungsaufbau und Watchdog
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Verbindung aufbauen, Watchdog und Modusabfrage starten."""
        self._closing = False
        self._session = async_get_clientsession(self.hass)
        await self._async_connect_once()
        self._runner = self.hass.async_create_background_task(
            self._async_watchdog(), name="eufy_max_watchdog"
        )
        self._poller = self.hass.async_create_background_task(
            self._async_station_poll(), name="eufy_max_station_poll"
        )

    async def async_test(self) -> str | None:
        """Nur pruefen, ob der Server antwortet."""
        session = async_get_clientsession(self.hass)
        ws = await session.ws_connect(self.url, timeout=10)
        try:
            msg = await ws.receive_json(timeout=10)
        finally:
            await ws.close()

        if msg.get("type") != "version":
            raise EufyMaxError(f"Unerwartete Antwort: {msg}")

        return msg.get("serverVersion")

    async def async_stop(self) -> None:
        """Verbindung sauber beenden."""
        self._closing = True
        for task in (self._runner, self._reader, self._poller):
            if task:
                task.cancel()
        self._runner = None
        self._reader = None
        self._poller = None
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self.connected = False

    async def _async_watchdog(self) -> None:
        """Haelt die Verbindung dauerhaft am Leben."""
        delay = RECONNECT_MIN_DELAY

        while not self._closing:
            await self._disconnected.wait()

            if self._closing:
                return

            self.connected = False
            self._notify_all()

            _LOGGER.warning(
                "Verbindung zu eufy-security-ws verloren. "
                "Neuer Versuch in %s Sekunden",
                delay,
            )
            await asyncio.sleep(delay)

            if self._closing:
                return

            try:
                await self._async_connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Reconnect fehlgeschlagen: %s", err)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)
                self._disconnected.set()
            else:
                delay = RECONNECT_MIN_DELAY
                _LOGGER.info("Verbindung zu eufy-security-ws wiederhergestellt")

    async def _async_connect_once(self) -> None:
        """Eine Verbindung aufbauen, Schema aushandeln, Zustand laden."""
        assert self._session is not None
        self._ws = await self._session.ws_connect(
            self.url, heartbeat=HEARTBEAT_INTERVAL, timeout=15
        )

        msg = await self._ws.receive_json(timeout=15)
        if msg.get("type") != "version":
            raise EufyMaxError(f"Unerwartete Begruessung: {msg}")

        server_max = msg.get("maxSchemaVersion", 13)
        self.schema_version = min(server_max, MAX_SCHEMA_VERSION)
        _LOGGER.debug(
            "eufy-security-ws %s, Schema %s ausgehandelt",
            msg.get("serverVersion"),
            self.schema_version,
        )

        self._disconnected.clear()
        self._reader = self.hass.async_create_background_task(
            self._async_reader(), name="eufy_max_reader"
        )

        await self._async_send_command(
            {"command": "set_api_schema", "schemaVersion": self.schema_version},
            wait_for_ws=False,
        )

        result = await self._async_send_command(
            {"command": "start_listening"}, wait_for_ws=False
        )
        await self._async_load_state(result.get("state", {}))

        self.connected = True
        self._notify_all()

    async def _async_load_state(self, state: dict[str, Any]) -> None:
        """Geraete und Stationen aus start_listening uebernehmen."""
        driver = state.get("driver", {})
        self.driver_connected = bool(driver.get("connected", False))

        self.devices = {}
        for entry in state.get("devices", []):
            if isinstance(entry, str):
                self.devices[entry] = {"serialNumber": entry}
            elif isinstance(entry, dict) and entry.get("serialNumber"):
                self.devices[entry["serialNumber"]] = entry

        self.stations = {}
        for entry in state.get("stations", []):
            if isinstance(entry, str):
                self.stations[entry] = {"serialNumber": entry}
            elif isinstance(entry, dict) and entry.get("serialNumber"):
                self.stations[entry["serialNumber"]] = entry

        _LOGGER.debug(
            "start_listening: %s Geraet(e), %s Station(en)",
            len(self.devices),
            len(self.stations),
        )

        if not self.driver_connected:
            _LOGGER.warning(
                "Treiber ist nicht mit der Eufy-Cloud verbunden - "
                "vermutlich Captcha, 2FA oder abgelaufene Sitzung"
            )
            await self._async_send_command(
                {"command": "driver.connect"}, wait_for_ws=False
            )

        for serial in list(self.devices):
            await self._async_load_device_details(serial)

        for serial in list(self.stations):
            await self._async_load_station_details(serial)

    async def _async_load_device_details(self, serial: str) -> None:
        """Eigenschaften, Metadaten und Befehlsliste eines Geraets laden."""
        try:
            props = await self._async_send_command(
                {"command": "device.get_properties", "serialNumber": serial},
                wait_for_ws=False,
            )
            self.devices[serial].update(props.get("properties", {}))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Eigenschaften fuer %s nicht ladbar: %s", serial, err)

        try:
            meta = await self._async_send_command(
                {
                    "command": "device.get_properties_metadata",
                    "serialNumber": serial,
                },
                wait_for_ws=False,
            )
            self.metadata[serial] = meta.get("properties", {})
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Metadaten fuer %s nicht ladbar: %s", serial, err)
            self.metadata[serial] = {}

        try:
            cmds = await self._async_send_command(
                {"command": "device.get_commands", "serialNumber": serial},
                wait_for_ws=False,
            )
            self.commands[serial] = cmds.get("commands", [])
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Befehle fuer %s nicht ladbar: %s", serial, err)
            self.commands[serial] = []

    async def _async_load_station_details(self, serial: str) -> None:
        """Eigenschaften und Metadaten einer Station laden."""
        try:
            props = await self._async_send_command(
                {"command": "station.get_properties", "serialNumber": serial},
                wait_for_ws=False,
            )
            self.stations[serial].update(props.get("properties", {}))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Stationseigenschaften fuer %s nicht ladbar: %s", serial, err
            )

        try:
            meta = await self._async_send_command(
                {
                    "command": "station.get_properties_metadata",
                    "serialNumber": serial,
                },
                wait_for_ws=False,
            )
            self.station_metadata[serial] = meta.get("properties", {})
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Stationsmetadaten fuer %s nicht ladbar: %s", serial, err
            )
            self.station_metadata[serial] = {}

    # ------------------------------------------------------------------
    # Stationen aktiv nachlesen
    # ------------------------------------------------------------------

    async def async_ask_camera(self, serial: str) -> None:
        """Die Kamera selbst nach ihren Parametern fragen.

        Der uebliche Weg fuehrt ueber die Cloud. Fuer neuere Modelle
        steht der Modus dort aber nicht mehr drin - der Wert bleibt dann
        auf dem Stand vom Verbindungsaufbau, egal was in der App
        passiert. CMD_CAMERA_INFO fragt direkt die Kamera.
        """
        try:
            await self.async_send_command(
                {"command": "station.get_camera_info", "serialNumber": serial}
            )
            await asyncio.sleep(CAMERA_INFO_DELAY)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Geraeteabfrage fuer %s: %s", serial, err)

    async def async_refresh_station(
        self, serial: str, tief: bool = False
    ) -> dict[str, Any]:
        """Eigenschaften einer Station frisch holen."""
        if tief:
            await self.async_ask_camera(serial)

        try:
            result = await self.async_send_command(
                {"command": "station.get_properties", "serialNumber": serial}
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Station %s nicht abfragbar: %s", serial, err)
            return {}

        props = result.get("properties", {})
        if not props:
            return {}

        station = self.stations.setdefault(serial, {"serialNumber": serial})
        vorher = station.get("guardMode")
        station.update(props)

        if props.get("guardMode") != vorher:
            _LOGGER.info(
                "%s meldet jetzt Modus %s (vorher %s)",
                station.get("name", serial),
                props.get("guardMode"),
                vorher,
            )

        self._notify(serial, "guardMode")
        return props

    async def async_refresh_all_stations(self, tief: bool = False) -> None:
        """Alle Stationen nacheinander abfragen."""
        for serial in list(self.stations):
            await self.async_refresh_station(serial, tief=tief)

    async def _async_station_poll(self) -> None:
        """Regelmaessig den Modus aller Stationen nachlesen.

        Die direkte Geraeteabfrage laeuft nur jede fuenfte Runde. Sie
        belegt die Station mehrere Sekunden - liefe sie jede Minute,
        stuende sie einem Moduswechsel staendig im Weg.
        """
        runde = 0

        while not self._closing:
            await asyncio.sleep(STATION_POLL_INTERVAL)

            if self._closing or not self.connected:
                continue

            runde += 1
            tief_erlaubt = runde % DEEP_POLL_EVERY == 0

            for serial in list(self.stations):
                # Waehrend eines laufenden Wechsels nicht dazwischenfunken
                lock = self._guard_locks.get(serial)
                if lock is not None and lock.locked():
                    continue

                try:
                    tief = False
                    if tief_erlaubt:
                        tief = await self.async_station_connected(serial)
                    await self.async_refresh_station(serial, tief=tief)
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Stationsabfrage %s: %s", serial, err)

    # ------------------------------------------------------------------
    # Nachtraegliche Anmeldung
    # ------------------------------------------------------------------

    async def _async_refresh_after_login(self) -> None:
        """Geraeteliste nachladen, wenn sich der Treiber spaeter anmeldet."""
        if self._refreshing:
            return

        self._refreshing = True
        try:
            vorher = set(self.devices)

            try:
                result = await self._async_send_command(
                    {"command": "start_listening"}
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Nachladen nach Anmeldung fehlgeschlagen: %s", err)
                return

            await self._async_load_state(result.get("state", {}))
            self._notify_all()

            neu = set(self.devices) - vorher
            if not neu:
                return

            _LOGGER.info(
                "Nach der Anmeldung sind %s Geraet(e) dazugekommen - "
                "Integration wird neu geladen",
                len(neu),
            )

            entries = self.hass.config_entries.async_entries(DOMAIN)
            if entries:
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(entries[0].entry_id)
                )
        finally:
            self._refreshing = False

    # ------------------------------------------------------------------
    # Akkukameras aufwecken
    # ------------------------------------------------------------------

    def station_for(self, serial: str) -> str:
        """Zur Kamera die zustaendige Station finden."""
        station = self.get_device(serial).get("stationSerialNumber")
        if station and station in self.stations:
            return station
        if serial in self.stations:
            return serial
        return station or serial

    async def async_station_connected(self, station: str) -> bool:
        """Pruefen, ob die P2P-Sitzung zur Station steht."""
        try:
            result = await self.async_send_command(
                {"command": "station.is_connected", "serialNumber": station}
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("is_connected fuer %s: %s", station, err)
            return False

        return bool(result.get("connected", False))

    async def async_wake(self, serial: str) -> bool:
        """Kamera aufwecken, bevor ein Befehl geschickt wird."""
        station = self.station_for(serial)
        lock = self._wake_locks.setdefault(station, asyncio.Lock())

        async with lock:
            if await self.async_station_connected(station):
                return True

            _LOGGER.debug("%s schlaeft - baue P2P-Sitzung auf", station)

            try:
                await self.async_send_command(
                    {"command": "station.connect", "serialNumber": station}
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("connect fuer %s: %s", station, err)

            wartezeit = 0.0
            while wartezeit < WAKE_TIMEOUT:
                await asyncio.sleep(WAKE_POLL)
                wartezeit += WAKE_POLL
                if await self.async_station_connected(station):
                    _LOGGER.debug("%s ist nach %.1f s wach", station, wartezeit)
                    return True

            _LOGGER.info("%s liess sich nicht aufwecken", station)
            return False

    # ------------------------------------------------------------------
    # Nachrichtenschleife
    # ------------------------------------------------------------------

    async def _async_reader(self) -> None:
        """Eingehende Nachrichten verarbeiten, bis die Verbindung endet."""
        try:
            if self._ws is None:
                return

            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        self._handle_message(msg.json())
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("Fehler beim Verarbeiten einer Nachricht")
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Leseschleife beendet: %s", err)
        finally:
            self.connected = False
            for future in self._futures.values():
                if not future.done():
                    future.set_exception(EufyMaxError("Verbindung verloren"))
            self._futures.clear()
            self._disconnected.set()

    def _handle_message(self, data: dict[str, Any]) -> None:
        """Eine einzelne Nachricht einsortieren."""
        msg_type = data.get("type")

        if msg_type == "result":
            message_id = data.get("messageId")
            future = self._futures.pop(message_id, None)
            if future and not future.done():
                if data.get("success"):
                    future.set_result(data.get("result", {}))
                else:
                    future.set_exception(
                        EufyMaxCommandError(
                            data.get("errorCode", "unbekannter Fehler")
                        )
                    )
            return

        if msg_type == "event":
            self._handle_event(data.get("event", {}))

    def _handle_event(self, event: dict[str, Any]) -> None:
        """Ein Event vom Server auswerten."""
        source = event.get("source")
        name = event.get("event")
        serial = event.get("serialNumber")

        if source == "driver":
            if name == "connected":
                war_verbunden = self.driver_connected
                self.driver_connected = True
                self._notify_all()
                if not war_verbunden or not self.devices:
                    self.hass.async_create_task(
                        self._async_refresh_after_login()
                    )
            elif name == "disconnected":
                self.driver_connected = False
                self._notify_all()
            elif name in ("captcha request", "verify code"):
                for callback in self._auth_listeners:
                    callback(name, event)
            return

        if source in ("device", "station") and serial:
            if name == "livestream video data":
                handler = self._video_handlers.get(serial)
                if handler is not None:
                    buffer = event.get("buffer")
                    data = buffer.get("data") if isinstance(buffer, dict) else None
                    if data:
                        metadata = event.get("metadata") or {}
                        codec = metadata.get("videoCodec")
                        try:
                            handler(bytes(data), codec)
                        except Exception:  # noqa: BLE001
                            _LOGGER.debug("Videodaten nicht uebergebbar")
                return

            if name == "livestream audio data":
                return

            store = self.devices if source == "device" else self.stations
            target = store.setdefault(serial, {"serialNumber": serial})

            if name == "property changed":
                target[event.get("name")] = event.get("value")
                self._notify(serial, event.get("name"))
            elif name in ("motion detected", "person detected", "pet detected",
                          "sound detected", "crying detected", "rings",
                          "package delivered", "vehicle detected"):
                key = name.replace(" ", "_")
                target[key] = event.get("state", True)
                self._notify(serial, key)
            elif name == "livestream started":
                target["livestreaming"] = True
                self._notify(serial, "livestreaming")
            elif name == "livestream stopped":
                target["livestreaming"] = False
                self._notify(serial, "livestreaming")
            else:
                self._notify(serial, None)

    # ------------------------------------------------------------------
    # Befehle
    # ------------------------------------------------------------------

    async def _async_send_command(
        self, command: dict[str, Any], wait_for_ws: bool = True
    ) -> dict[str, Any]:
        """Befehl senden und auf das Ergebnis warten."""
        if self._ws is None or self._ws.closed:
            raise EufyMaxError("Keine Verbindung zu eufy-security-ws")

        self._message_id += 1
        message_id = str(self._message_id)
        payload = {**command, "messageId": message_id}

        future: asyncio.Future = self.hass.loop.create_future()
        self._futures[message_id] = future

        await self._ws.send_json(payload)

        try:
            async with asyncio.timeout(COMMAND_TIMEOUT):
                return await future
        except TimeoutError as err:
            self._futures.pop(message_id, None)
            raise EufyMaxError(f"Zeitueberschreitung bei {command}") from err

    async def async_send_command(self, command: dict[str, Any]) -> dict[str, Any]:
        """Oeffentlicher Wrapper fuer Befehle."""
        return await self._async_send_command(command)

    async def async_set_property(
        self, serial: str, name: str, value: Any
    ) -> None:
        """Eine Geraeteeigenschaft setzen."""
        await self.async_send_command(
            {
                "command": "device.set_property",
                "serialNumber": serial,
                "name": name,
                "value": value,
            }
        )
        if serial in self.devices:
            self.devices[serial][name] = value
            self._notify(serial, name)

    async def async_set_station_property(
        self, serial: str, name: str, value: Any
    ) -> None:
        """Eine Stationseigenschaft setzen."""
        await self.async_send_command(
            {
                "command": "station.set_property",
                "serialNumber": serial,
                "name": name,
                "value": value,
            }
        )
        if serial in self.stations:
            self.stations[serial][name] = value
            self._notify(serial, name)

    def _guard_payload(self, serial: str, mode: int) -> dict[str, Any]:
        """Passenden Befehl fuer den Moduswechsel bauen."""
        if self.schema_version >= 13:
            return {
                "command": "station.set_property",
                "serialNumber": serial,
                "name": "guardMode",
                "value": mode,
            }
        return {
            "command": "station.set_guard_mode",
            "serialNumber": serial,
            "mode": mode,
        }

    async def async_set_guard_mode(
        self, serial: str, mode: int, versuche: int = 2
    ) -> None:
        """Guard Mode setzen - sofort senden, danach absichern.

        Die Reihenfolge ist hier entscheidend. Frueher wurde erst die
        Kamera geweckt und dann gesendet. Das kostete bis zu zwoelf
        Sekunden, und lief parallel die regelmaessige Modusabfrage, wurde
        es noch deutlich mehr - der Befehl lag also eine halbe Minute
        herum, bevor Eufy ihn ueberhaupt zu sehen bekam.

        Jetzt geht der Befehl als Erstes raus. Wecken und Nachpruefen
        laufen danach im Hintergrund und halten niemanden mehr auf.
        """
        mode = int(mode)

        # Wunsch vormerken - wer zuletzt drueckt, bestimmt das Ziel.
        self._guard_targets[serial] = mode

        # Sofort senden. Ist die Kamera wach, schaltet sie in
        # Sekundenbruchteilen.
        await self.async_send_command(self._guard_payload(serial, mode))

        # Anzeige sofort mitziehen, damit die Oberflaeche nicht haengt.
        if serial in self.stations:
            self.stations[serial]["guardMode"] = mode
            self._notify(serial, "guardMode")

        # Alles Weitere im Hintergrund.
        self.hass.async_create_task(
            self._async_guard_followup(serial, mode, versuche)
        )

    async def _async_guard_followup(
        self, serial: str, mode: int, versuche: int
    ) -> None:
        """Nach dem Senden absichern, dass der Wechsel wirklich ankam."""
        name = self.get_station(serial).get("name", serial)
        lock = self._guard_locks.setdefault(serial, asyncio.Lock())

        if lock.locked():
            # Es laeuft bereits eine Nachkontrolle fuer diese Station.
            # Die merkt am Zielwert selbst, dass sich etwas geaendert hat.
            return

        async with lock:
            for versuch in range(1, versuche + 1):
                wartezeit = 0.0

                while wartezeit < GUARD_MODE_TIMEOUT:
                    await asyncio.sleep(GUARD_MODE_POLL)
                    wartezeit += GUARD_MODE_POLL

                    # Neuer Wunsch? Dann ist dieser hier hinfaellig.
                    if self._guard_targets.get(serial) != mode:
                        return

                    tief = wartezeit >= GUARD_MODE_TIMEOUT / 2
                    await self.async_refresh_station(serial, tief=tief)

                    gemeldet = self.get_station_property(serial, "guardMode")
                    if gemeldet is not None and int(gemeldet) == mode:
                        _LOGGER.debug(
                            "%s hat Modus %s nach %.0f s bestaetigt",
                            name,
                            mode,
                            wartezeit,
                        )
                        return

                if versuch >= versuche:
                    break

                _LOGGER.info(
                    "%s hat den Moduswechsel auf %s nicht bestaetigt - "
                    "wecke und sende erneut",
                    name,
                    mode,
                )

                await self.async_wake(serial)

                if self._guard_targets.get(serial) != mode:
                    return

                try:
                    await self.async_send_command(
                        self._guard_payload(serial, mode)
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Wiederholung fuer %s: %s", name, err)

            _LOGGER.warning(
                "%s meldet weiterhin Modus %s statt %s",
                name,
                self.get_station_property(serial, "guardMode"),
                mode,
            )

    async def async_trigger_station_alarm(
        self, serial: str, seconds: int = 30
    ) -> None:
        """Sirene der Station ausloesen."""
        await self.async_send_command(
            {
                "command": "station.trigger_alarm",
                "serialNumber": serial,
                "seconds": seconds,
            }
        )

    async def async_reset_station_alarm(self, serial: str) -> None:
        """Sirene der Station stoppen."""
        await self.async_send_command(
            {"command": "station.reset_alarm", "serialNumber": serial}
        )

    def get_station(self, serial: str) -> dict[str, Any]:
        """Zustand einer Station holen."""
        return self.stations.get(serial, {})

    def get_station_property(
        self, serial: str, name: str, default: Any = None
    ) -> Any:
        """Einzelne Stationseigenschaft holen."""
        return self.stations.get(serial, {}).get(name, default)

    def get_station_metadata(self, serial: str) -> dict[str, Any]:
        """Property-Metadaten einer Station holen."""
        return self.station_metadata.get(serial, {})

    async def async_pan_and_tilt(self, serial: str, direction: int) -> None:
        """Kamera schwenken oder neigen."""
        await self.async_send_command(
            {
                "command": "device.pan_and_tilt",
                "serialNumber": serial,
                "direction": direction,
            }
        )

    async def async_start_livestream(self, serial: str) -> None:
        """P2P-Livestream starten."""
        await self.async_send_command(
            {"command": "device.start_livestream", "serialNumber": serial}
        )

    async def async_stop_livestream(self, serial: str) -> None:
        """P2P-Livestream stoppen."""
        await self.async_send_command(
            {"command": "device.stop_livestream", "serialNumber": serial}
        )

    async def async_set_captcha(self, captcha_id: str, captcha: str) -> None:
        """Captcha-Antwort an den Treiber schicken."""
        await self.async_send_command(
            {
                "command": "driver.set_captcha",
                "captchaId": captcha_id,
                "captcha": captcha,
            }
        )

    async def async_set_verify_code(self, code: str) -> None:
        """2FA-Code an den Treiber schicken."""
        await self.async_send_command(
            {"command": "driver.set_verify_code", "verifyCode": code}
        )

    async def async_reconnect(self) -> None:
        """Verbindung zur Eufy-Cloud neu aufbauen und Liste nachladen."""
        await self.async_send_command({"command": "driver.connect"})
        await self._async_refresh_after_login()

    # ------------------------------------------------------------------
    # Hilfsfunktionen
    # ------------------------------------------------------------------

    def get_device(self, serial: str) -> dict[str, Any]:
        """Zustand eines Geraets holen."""
        return self.devices.get(serial, {})

    def get_property(self, serial: str, name: str, default: Any = None) -> Any:
        """Einzelne Eigenschaft holen."""
        return self.devices.get(serial, {}).get(name, default)

    def get_metadata(self, serial: str) -> dict[str, Any]:
        """Property-Metadaten eines Geraets holen."""
        return self.metadata.get(serial, {})

    def has_command(self, serial: str, command: str) -> bool:
        """Pruefen, ob ein Geraet einen Befehl unterstuetzt."""
        available = self.commands.get(serial, [])
        wanted = command.split(".")[-1]
        return any(entry.split(".")[-1] == wanted for entry in available)

    def add_listener(self, callback: Callable[[str, str | None], None]) -> Callable:
        """Listener fuer Zustandsaenderungen registrieren."""
        self._listeners.append(callback)

        def remove() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return remove

    def add_video_handler(
        self, serial: str, callback: Callable[[bytes, str | None], None]
    ) -> None:
        """Empfaenger fuer die rohen P2P-Videodaten einer Kamera setzen."""
        self._video_handlers[serial] = callback

    def remove_video_handler(self, serial: str) -> None:
        """Empfaenger wieder abmelden."""
        self._video_handlers.pop(serial, None)

    def add_auth_listener(
        self, callback: Callable[[str, dict[str, Any]], None]
    ) -> None:
        """Listener fuer Captcha- und 2FA-Anfragen registrieren."""
        self._auth_listeners.append(callback)

    def _notify(self, serial: str, prop: str | None) -> None:
        """Listener ueber eine Aenderung informieren."""
        for callback in list(self._listeners):
            callback(serial, prop)

    def _notify_all(self) -> None:
        """Alle Entities zum Neuzeichnen anstossen."""
        for serial in list(self.devices):
            self._notify(serial, None)
