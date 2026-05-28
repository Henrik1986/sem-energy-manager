from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

DOMAIN = "huawei_solar"


async def check_huawei_setup(hass):

    result = {
        "installed": False,
        "configured": False,
        "devices_found": False,
        "entities_found": False,
        "battery_found": False,
    }

    # Är integrationen installerad/konfigurerad?
    entries = hass.config_entries.async_entries(DOMAIN)

    if not entries:
        return result

    result["installed"] = True
    result["configured"] = True

    # Devices
    device_registry = dr.async_get(hass)

    devices = dr.async_entries_for_config_entry(
        device_registry,
        entries[0].entry_id
    )

    if devices:
        result["devices_found"] = True

    # Entities
    entity_registry = er.async_get(hass)

    entities = er.async_entries_for_config_entry(
        entity_registry,
        entries[0].entry_id
    )

    if entities:
        result["entities_found"] = True

    # Försök hitta batteri
    for entity in entities:

        entity_id = entity.entity_id

        state = hass.states.get(entity_id)

        if not state:
            continue

        if state.attributes.get("device_class") == "battery":
            result["battery_found"] = True

    return result
