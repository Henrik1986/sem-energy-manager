"""SEM integration setup and generic service registration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
import voluptuous as vol

from .const import DOMAIN
from .systems.adapters import create_system_adapters
from .systems.manager import SemSystemManager
from .systems.registry import SemEntityRegistry

PLATFORMS = [Platform.SENSOR, Platform.SELECT, Platform.NUMBER]

SEM_COMMAND_SCHEMAS = {
    "set_battery_time_of_use_periods": vol.Schema(
        {vol.Required("periods"): str}
    ),
    "set_grid_export_power_limit": vol.Schema(
        {vol.Required("power"): vol.Any(str, int, float)}
    ),
    "reset_grid_export_power_limit": vol.Schema({}),
}


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the SEM integration and its system-independent services."""
    hass.data.setdefault(DOMAIN, {})

    for command, schema in SEM_COMMAND_SCHEMAS.items():
        if not hass.services.has_service(DOMAIN, command):
            hass.services.async_register(
                DOMAIN,
                command,
                _create_command_handler(hass, command),
                schema=schema,
            )

    return True


def _create_command_handler(hass: HomeAssistant, command: str):
    """Create a handler for a generic SEM command."""

    async def handle_service(call: ServiceCall) -> None:
        managers = list(
            hass.data.get(DOMAIN, {}).get("system_managers", {}).values()
        )
        active_managers = [
            manager
            for manager in managers
            if manager.active_adapter is not None
        ]

        if len(active_managers) != 1:
            raise HomeAssistantError(
                "SEM needs exactly one active energy-system adapter "
                "to execute this command"
            )

        await active_managers[0].async_execute(
            command,
            dict(call.data),
        )

    return handle_service


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Set up SEM and its registered energy-system adapters."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data

    entity_registries = hass.data[DOMAIN].setdefault(
        "entity_registries", {}
    )
    entity_registry = SemEntityRegistry()
    entity_registries[entry.entry_id] = entity_registry

    system_manager = SemSystemManager(hass, entity_registry)
    for adapter in create_system_adapters():
        system_manager.register_adapter(adapter)

    system_managers = hass.data[DOMAIN].setdefault(
        "system_managers", {}
    )
    system_managers[entry.entry_id] = system_manager
    system_manager.async_start()

    await hass.config_entries.async_forward_entry_setups(
        entry, PLATFORMS
    )

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Unload SEM platforms and adapter tasks."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )

    if unload_ok:
        system_manager = hass.data[DOMAIN].get(
            "system_managers", {}
        ).pop(entry.entry_id, None)

        if system_manager is not None:
            await system_manager.async_unload()

        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.data[DOMAIN].get("entity_registries", {}).pop(
            entry.entry_id, None
        )

    return unload_ok
