"""Generic Home Assistant entity registry resolver."""

from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er


@dataclass(frozen=True, slots=True)
class EntityRegistryMatch:
    """Information about a resolved entity and its device relationship."""

    entity_id: str
    unique_id: str
    device_id: str | None
    parent_device_id: str | None
    root_device_id: str | None
    device_type: str | None
    config_entry_id: str | None
    disabled_by: str | None


def _get_root_device_id(
    device_registry: dr.DeviceRegistry,
    device_id: str | None,
) -> str | None:
    """Return the highest parent device in a device hierarchy."""
    current_device_id = device_id
    visited: set[str] = set()

    while current_device_id is not None:
        if current_device_id in visited:
            break
        visited.add(current_device_id)

        device = device_registry.async_get(current_device_id)
        if device is None or device.via_device_id is None:
            return current_device_id

        current_device_id = device.via_device_id

    return current_device_id or device_id


def _get_device_type(device: dr.AnyDeviceEntry | None) -> str | None:
    """Identify Huawei child devices from their integration identifiers."""
    if device is None:
        return None

    for domain, identifier in device.identifiers:
        if domain != "huawei_solar":
            continue
        if identifier.endswith("/connected_energy_storage"):
            return "battery"
        if identifier.endswith("/power_meter"):
            return "power_meter"
        if identifier.endswith("/battery_1") or identifier.endswith("/battery_2"):
            return "battery_module"
        if "/" not in identifier:
            return "inverter"

    return None


def find_registry_entities(
    hass: HomeAssistant,
    *,
    platform: str,
    domain: str,
    translation_key: str,
) -> list[EntityRegistryMatch]:
    """Find entities using integration platform and translation key."""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    matches: list[EntityRegistryMatch] = []

    for entry in entity_registry.entities.values():
        if entry.platform != platform:
            continue

        if entry.domain != domain:
            continue

        if entry.translation_key != translation_key:
            continue

        disabled_by = entry.disabled_by
        if disabled_by is not None:
            disabled_by = getattr(disabled_by, "value", str(disabled_by))

        device = (
            device_registry.async_get(entry.device_id)
            if entry.device_id is not None
            else None
        )

        matches.append(
            EntityRegistryMatch(
                entity_id=entry.entity_id,
                unique_id=entry.unique_id,
                device_id=entry.device_id,
                parent_device_id=(
                    device.via_device_id if device is not None else None
                ),
                root_device_id=_get_root_device_id(
                    device_registry, entry.device_id
                ),
                device_type=_get_device_type(device),
                config_entry_id=entry.config_entry_id,
                disabled_by=disabled_by,
            )
        )

    return sorted(matches, key=lambda entity: entity.entity_id)
