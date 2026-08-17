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
    HEARTBEAT_INTERVAL,
    MAX_SCHEMA_VERSION,
    RECONNECT_MAX_DELAY,
    RECONNECT_MIN_DELAY,
)

_LOGGER = logging.getLogger(__name__)


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
        self.commands: dict[str, list[str]] = {}

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._message_id: int = 0
        self._futures: dict[str, asyncio.Future] = {}
        self._listeners: list[Callable[[str, str | None], None]] = []
        self._auth_listeners: list[Callable[[str, dict[str, Any]], None]] = []
        # Wird von __init__.py gesetzt: zentraler Livestream-Controller
        self.stream: Any = None

        self._runner: asyncio.Task | None = None
        self._reader: asyncio.Task | None = None
        self._disconnected = asyncio.Event()
        self._closing: bool = False

    # ------------------------------------------------------------------
    # Verbindungsaufbau und Watchdog
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Verbindung aufbauen und Watchdog starten."""
        self._closing = False
        self._session = async_get_clientsession(self.hass)
        # Erster Verbindungsversuch synchron, damit der Config Entry
        # sauber fehlschlaegt, wenn der Server gar nicht da ist.
        await self._async_connect_once()
        self._runner = self.hass.async_create_background_task(
            self._async_watchdog(), name="eufy_max_watchdog"
        )

    async def async_test(self) -> str | None:
        """Nur pruefen, ob der Server antwortet.

        Bewusst ohne start_listening und ohne Metadatenabfrage - das
        dauert auf schwacher Hardware zu lange fuer den Einrichtungs-
        dialog und laesst ihn in den Timeout laufen.
        """
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
        if self._runner:
            self._runner.cancel()
            self._runner = None
        if self._reader:
            self._reader.cancel()
            self._reader = None
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self.connected = False

    async def _async_watchdog(self) -> None:
        """Haelt die Verbindung dauerhaft am Leben.

        Wartet darauf, dass die Leseschleife das Ende der Verbindung
        meldet, und verbindet dann mit wachsendem Abstand endlos neu,
        statt die Entities dauerhaft auf 'unavailable' stehen zu lassen.
        """
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
                # Erneut ausloesen, damit die Schleife weiterlaeuft.
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

        # Der Server schickt als erstes seine Version.
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

        # WICHTIG: Die Leseschleife muss laufen, BEVOR der erste Befehl
        # rausgeht. Sonst liest niemand die Antworten und jeder Befehl
        # laeuft in den Timeout.
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
        """Geraete und Stationen aus start_listening uebernehmen.

        Ab Schema 13 liefert der Server in 'devices' und 'stations' nur
        noch Seriennummern als Strings. Die Eigenschaften muessen dann
        einzeln per get_properties nachgeladen werden. Aeltere Schemas
        liefern vollstaendige Objekte - beides wird hier abgedeckt.
        """
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
                "vermutlich Captcha oder 2FA offen"
            )
            await self._async_send_command(
                {"command": "driver.connect"}, wait_for_ws=False
            )

        # Eigenschaften, Metadaten und Befehle je Geraet nachladen.
        # Daraus werden spaeter die Entities generiert.
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

        _LOGGER.debug(
            "%s (%s): %s Eigenschaften, Befehle: %s",
            self.devices[serial].get("name", serial),
            self.devices[serial].get("model", "?"),
            len(self.metadata.get(serial, {})),
            self.commands.get(serial, []),
        )

    async def _async_load_station_details(self, serial: str) -> None:
        """Eigenschaften einer Station laden - liefert den Guard Mode."""
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

    # ------------------------------------------------------------------
    # Nachrichtenschleife
    # ------------------------------------------------------------------

    async def _async_reader(self) -> None:
        """Eingehende Nachrichten verarbeiten, bis die Verbindung endet.

        Laeuft als eigener Task ab dem Moment, in dem die Verbindung
        steht - also auch waehrend der Anmeldebefehle.
        """
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
            # Wartende Befehle nicht ins Timeout laufen lassen.
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
                self.driver_connected = True
                self._notify_all()
            elif name == "disconnected":
                self.driver_connected = False
                self._notify_all()
            elif name in ("captcha request", "verify code"):
                for callback in self._auth_listeners:
                    callback(name, event)
            return

        if source in ("device", "station") and serial:
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
        # Optimistisch schon lokal setzen, das Event bestaetigt gleich.
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

    async def async_set_guard_mode(self, serial: str, mode: int) -> None:
        """Guard Mode einer Station bzw. einer eigenstaendigen Kamera setzen."""
        await self.async_send_command(
            {
                "command": "station.set_guard_mode",
                "serialNumber": serial,
                "mode": int(mode),
            }
        )
        if serial in self.stations:
            self.stations[serial]["guardMode"] = int(mode)
            self._notify(serial, "guardMode")

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
        """Verbindung zur Eufy-Cloud neu aufbauen."""
        await self.async_send_command({"command": "driver.connect"})

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
        """Pruefen, ob ein Geraet einen Befehl unterstuetzt.

        Der Server liefert die Namen ohne Praefix und in snake_case,
        also 'start_livestream' statt 'device.start_livestream'. Hier
        werden beide Schreibweisen akzeptiert.
        """
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
