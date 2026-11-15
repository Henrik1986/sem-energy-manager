from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .systems.huawei import async_wait_for_huawei
from .systems.registry import SemEntityRegistry

PLATFORMS = [Platform.SENSOR, Platform.SELECT, Platform.NUMBER]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data

    entity_registries = hass.data[DOMAIN].setdefault(
        "entity_registries", {}
    )
    entity_registry = SemEntityRegistry()
    entity_registries[entry.entry_id] = entity_registry

    hass.async_create_task(
        async_wait_for_huawei(hass, entity_registry)
    )

    await hass.config_entries.async_forward_entry_setups(
        entry, PLATFORMS
    )

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.data[DOMAIN].get("entity_registries", {}).pop(
            entry.entry_id, None
        )

    return unload_ok
