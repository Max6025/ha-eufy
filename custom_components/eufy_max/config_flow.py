"""Einrichtungsdialog fuer Eufy Max."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .const import (
    CONF_HOST,
    CONF_PORT,
    CONF_RTSP_FIRST,
    CONF_SNAPSHOT_FROM_STREAM,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_RTSP_FIRST,
    DEFAULT_SNAPSHOT_FROM_STREAM,
    DOMAIN,
)
from .websocket import EufyMaxClient

SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
    }
)


class EufyMaxConfigFlow(ConfigFlow, domain=DOMAIN):
    """Fuehrt durch die Einrichtung."""

    VERSION = 1

    def __init__(self) -> None:
        """Flow initialisieren."""
        self._discovered: dict[str, Any] | None = None

    async def async_step_hassio(
        self, discovery_info: HassioServiceInfo
    ) -> FlowResult:
        """Das eigene Add-on hat sich gemeldet - nichts mehr eintippen."""
        host = discovery_info.config.get(CONF_HOST)
        port = discovery_info.config.get(CONF_PORT, DEFAULT_PORT)

        await self.async_set_unique_id(f"{host}:{port}")
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: host, CONF_PORT: port}
        )

        self._discovered = {CONF_HOST: host, CONF_PORT: port}
        self.context["title_placeholders"] = {"name": "Eufy Max WS Add-on"}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Kurz bestaetigen lassen, dann fertig."""
        assert self._discovered is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            client = EufyMaxClient(
                self.hass,
                self._discovered[CONF_HOST],
                self._discovered[CONF_PORT],
            )
            try:
                await client.async_start()
                device_count = len(client.devices)
                await client.async_stop()
            except Exception:  # noqa: BLE001
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=f"Eufy Max ({device_count} Geraete)",
                    data=self._discovered,
                )

        return self.async_show_form(
            step_id="hassio_confirm",
            description_placeholders={
                "addon": f"{self._discovered[CONF_HOST]}:{self._discovered[CONF_PORT]}"
            },
            errors=errors,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Adresse des eufy-security-ws Servers abfragen und pruefen."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]

            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            client = EufyMaxClient(self.hass, host, port)
            try:
                await client.async_start()
                device_count = len(client.devices)
                await client.async_stop()
            except Exception:  # noqa: BLE001
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=f"Eufy Max ({device_count} Geraete)",
                    data={CONF_HOST: host, CONF_PORT: port},
                )

        return self.async_show_form(
            step_id="user", data_schema=SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        """Optionen-Dialog."""
        return EufyMaxOptionsFlow(entry)


class EufyMaxOptionsFlow(OptionsFlow):
    """Optionen nach der Einrichtung."""

    def __init__(self, entry: ConfigEntry) -> None:
        """Optionen initialisieren."""
        self.entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Optionen anzeigen und speichern."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_RTSP_FIRST,
                        default=options.get(CONF_RTSP_FIRST, DEFAULT_RTSP_FIRST),
                    ): bool,
                    vol.Required(
                        CONF_SNAPSHOT_FROM_STREAM,
                        default=options.get(
                            CONF_SNAPSHOT_FROM_STREAM, DEFAULT_SNAPSHOT_FROM_STREAM
                        ),
                    ): bool,
                }
            ),
        )
