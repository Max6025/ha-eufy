"""Eufy Max - eigene Home Assistant Integration fuer Eufy Security."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    ATTR_CAPTCHA,
    ATTR_CAPTCHA_ID,
    ATTR_DIRECTION,
    ATTR_PROFILE,
    ATTR_PROPERTY,
    ATTR_VALUE,
    ATTR_VERIFY_CODE,
    CONF_HOST,
    CONF_PORT,
    CONF_RTSP_FIRST,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DOMAIN,
    PLATFORMS,
    PROFILE_AWAY,
    PROFILE_HOME,
    PTZ_DIRECTIONS,
    SERVICE_PTZ,
    SERVICE_RECONNECT,
    SERVICE_SAVE_PROFILE,
    SERVICE_SET_CAPTCHA,
    SERVICE_SET_PROPERTY,
    SERVICE_SET_VERIFY_CODE,
    SERVICE_SET_GUARD_MODE,
    ATTR_GUARD_MODE,
    SERVICE_START_STREAM,
    SERVICE_STOP_STREAM,
    SERVICE_START_CAMERA_STREAM,
    SERVICE_STOP_CAMERA_STREAM,
    ATTR_DURATION,
    SIGNAL_DEVICE_UPDATE,
)
from .profiles import ModusProfile
from .stream import StreamController
from .websocket import EufyMaxClient, EufyMaxError

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Integration aus einem Config Entry aufsetzen."""
    host = entry.data.get(CONF_HOST, DEFAULT_HOST)
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)

    client = EufyMaxClient(hass, host, port)

    try:
        await client.async_start()
    except Exception as err:
        raise ConfigEntryNotReady(
            f"Verbindung zu eufy-security-ws auf {host}:{port} fehlgeschlagen: {err}"
        ) from err

    # Zentraler Livestream-Controller haengt am Client, damit alle
    # Plattformen ihn ueber client.stream erreichen.
    client.stream = StreamController(hass, client)

    # Modus-Profile fuer das Sammelpanel. Muss vor dem Anlegen der
    # Entities geladen sein, sonst zeigt das Panel beim Start eine
    # falsche Lage.
    client.profile = ModusProfile(hass, client)
    await client.profile.async_load()

    # Standard ist der P2P-Weg: die von den Kameras gemeldeten
    # RTSP-Adressen stimmen nicht immer, der P2P-Strom dagegen schon.
    client.rtsp_first = entry.options.get(CONF_RTSP_FIRST, False)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = client

    @callback_safe
    def _on_update(serial: str, prop: str | None) -> None:
        """Aenderung an die Entities weiterreichen."""
        async_dispatcher_send(hass, f"{SIGNAL_DEVICE_UPDATE}_{serial}")

    client.add_listener(_on_update)

    def _on_auth(event_name: str, event: dict[str, Any]) -> None:
        """Captcha oder 2FA-Anfrage sichtbar machen."""
        if event_name == "captcha request":
            captcha_id = event.get("captchaId", "")
            persistent_notification.async_create(
                hass,
                "Eufy verlangt ein Captcha.\n\n"
                f"Captcha-ID: `{captcha_id}`\n\n"
                "Bild oeffnen, Code ablesen und den Service "
                "`eufy_max.set_captcha` mit ID und Code aufrufen.\n\n"
                f"Bild: {event.get('captcha', '')[:120]}",
                title="Eufy Max: Captcha noetig",
                notification_id=f"{DOMAIN}_captcha",
            )
        elif event_name == "verify code":
            persistent_notification.async_create(
                hass,
                "Eufy hat einen 2FA-Code per Mail geschickt.\n\n"
                "Service `eufy_max.set_verify_code` mit dem Code aufrufen.",
                title="Eufy Max: 2FA-Code noetig",
                notification_id=f"{DOMAIN}_verify",
            )

    client.add_auth_listener(_on_auth)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def callback_safe(func):
    """Kleiner Wrapper, damit Listener-Fehler nichts abreissen."""

    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Fehler in Update-Listener")

    return wrapper


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Bei geaenderten Optionen neu laden."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Config Entry entladen."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        client: EufyMaxClient = hass.data[DOMAIN].pop(entry.entry_id)
        if getattr(client, "stream", None):
            await client.stream.async_shutdown()
        await client.async_stop()
    return unloaded


def _get_client(hass: HomeAssistant) -> EufyMaxClient:
    """Ersten verfuegbaren Client holen."""
    clients = list(hass.data.get(DOMAIN, {}).values())
    if not clients:
        raise EufyMaxError("Keine aktive Eufy Max Instanz")
    return clients[0]


def _async_register_services(hass: HomeAssistant) -> None:
    """Services registrieren (nur einmal)."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_CAPTCHA):
        return

    async def handle_set_captcha(call: ServiceCall) -> None:
        client = _get_client(hass)
        await client.async_set_captcha(
            call.data[ATTR_CAPTCHA_ID], call.data[ATTR_CAPTCHA]
        )
        persistent_notification.async_dismiss(hass, f"{DOMAIN}_captcha")

    async def handle_set_verify_code(call: ServiceCall) -> None:
        client = _get_client(hass)
        await client.async_set_verify_code(call.data[ATTR_VERIFY_CODE])
        persistent_notification.async_dismiss(hass, f"{DOMAIN}_verify")

    async def handle_set_property(call: ServiceCall) -> None:
        client = _get_client(hass)
        for entity_id in call.data.get("entity_id", []):
            serial = _serial_from_entity(hass, entity_id)
            if serial:
                await client.async_set_property(
                    serial, call.data[ATTR_PROPERTY], call.data[ATTR_VALUE]
                )

    async def handle_ptz(call: ServiceCall) -> None:
        client = _get_client(hass)
        direction = PTZ_DIRECTIONS[call.data[ATTR_DIRECTION]]
        for entity_id in call.data.get("entity_id", []):
            serial = _serial_from_entity(hass, entity_id)
            if serial:
                await client.async_pan_and_tilt(serial, direction)

    async def handle_reconnect(call: ServiceCall) -> None:
        client = _get_client(hass)
        await client.async_reconnect()

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CAPTCHA,
        handle_set_captcha,
        schema=vol.Schema(
            {
                vol.Required(ATTR_CAPTCHA_ID): cv.string,
                vol.Required(ATTR_CAPTCHA): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_VERIFY_CODE,
        handle_set_verify_code,
        schema=vol.Schema({vol.Required(ATTR_VERIFY_CODE): cv.string}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_PROPERTY,
        handle_set_property,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_ids,
                vol.Required(ATTR_PROPERTY): cv.string,
                vol.Required(ATTR_VALUE): vol.Any(str, int, float, bool),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PTZ,
        handle_ptz,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_ids,
                vol.Required(ATTR_DIRECTION): vol.In(list(PTZ_DIRECTIONS)),
            }
        ),
    )
    async def handle_start_stream(call: ServiceCall) -> None:
        client = _get_client(hass)
        await client.stream.async_start(call.data.get(ATTR_DURATION))

    async def handle_stop_stream(call: ServiceCall) -> None:
        client = _get_client(hass)
        await client.stream.async_stop()

    hass.services.async_register(DOMAIN, SERVICE_RECONNECT, handle_reconnect)
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_STREAM,
        handle_start_stream,
        schema=vol.Schema(
            {vol.Optional(ATTR_DURATION): vol.All(int, vol.Range(min=10, max=1800))}
        ),
    )
    async def handle_start_camera_stream(call: ServiceCall) -> None:
        """Nur die angegebenen Kameras aufschalten."""
        client = _get_client(hass)
        duration = call.data.get(ATTR_DURATION)
        for entity_id in call.data.get("entity_id", []):
            serial = _serial_from_entity(hass, entity_id)
            if serial:
                await client.stream.async_start_one(serial, duration)
            else:
                _LOGGER.warning("Keine Kamera zu %s gefunden", entity_id)

    async def handle_stop_camera_stream(call: ServiceCall) -> None:
        """Nur die angegebenen Kameras abschalten."""
        client = _get_client(hass)
        for entity_id in call.data.get("entity_id", []):
            serial = _serial_from_entity(hass, entity_id)
            if serial:
                await client.stream.async_stop_one(serial)

    hass.services.async_register(
        DOMAIN,
        SERVICE_START_CAMERA_STREAM,
        handle_start_camera_stream,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_ids,
                vol.Optional(ATTR_DURATION): vol.All(
                    int, vol.Range(min=10, max=1800)
                ),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_CAMERA_STREAM,
        handle_stop_camera_stream,
        schema=vol.Schema({vol.Required("entity_id"): cv.entity_ids}),
    )

    async def handle_set_guard_mode(call: ServiceCall) -> None:
        client = _get_client(hass)
        mode = int(call.data[ATTR_GUARD_MODE])
        targets = call.data.get("entity_id")
        if targets:
            for entity_id in targets:
                serial = _serial_from_entity(hass, entity_id)
                if serial:
                    station = serial
                    if serial not in client.stations:
                        station = client.get_property(serial, "stationSerialNumber")
                    if station:
                        await client.async_set_guard_mode(station, mode)
        else:
            for station in client.stations:
                await client.async_set_guard_mode(station, mode)

    hass.services.async_register(DOMAIN, SERVICE_STOP_STREAM, handle_stop_stream)
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_GUARD_MODE,
        handle_set_guard_mode,
        schema=vol.Schema(
            {
                vol.Optional("entity_id"): cv.entity_ids,
                vol.Required(ATTR_GUARD_MODE): vol.All(int, vol.In([0, 1, 2, 3, 4, 5, 6, 47, 63])),
            }
        ),
    )

    async def handle_save_profile(call: ServiceCall) -> None:
        """Aktuelle Modi als Profil speichern.

        Ohne Angabe wird in die Lage gespeichert, auf der das
        Sammelpanel gerade steht.
        """
        client = _get_client(hass)
        profile = getattr(client, "profile", None)
        if profile is None:
            raise EufyMaxError("Profilspeicher nicht bereit")
        await profile.async_save(call.data.get(ATTR_PROFILE))

    hass.services.async_register(
        DOMAIN,
        SERVICE_SAVE_PROFILE,
        handle_save_profile,
        schema=vol.Schema(
            {vol.Optional(ATTR_PROFILE): vol.In([PROFILE_HOME, PROFILE_AWAY])}
        ),
    )


def _serial_from_entity(hass: HomeAssistant, entity_id: str) -> str | None:
    """Seriennummer aus einer Entity-ID ermitteln."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry and entry.unique_id:
        return entry.unique_id.split("_")[0]
    return None
