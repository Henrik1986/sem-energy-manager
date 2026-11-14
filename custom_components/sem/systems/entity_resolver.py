"""Generic Home Assistant entity registry resolver."""

from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


@dataclass(frozen=True, slots=True)
class EntityRegistryMatch:
    """Information about a resolved entity."""

    entity_id: str
    unique_id: str
    device_id: str | None
    config_entry_id: str | None
    disabled_by: str | None


def find_registry_entities(
    hass: HomeAssistant,
    *,
    platform: str,
    domain: str,
    translation_key: str,
) -> list[EntityRegistryMatch]:
    """Find entities using integration platform and translation key."""
    registry = er.async_get(hass)
    matches: list[EntityRegistryMatch] = []

    for entry in registry.entities.values():
        if entry.platform != platform:
            continue

        if entry.domain != domain:
            continue

        if entry.translation_key != translation_key:
            continue

        disabled_by = entry.disabled_by
        if disabled_by is not None:
            disabled_by = getattr(disabled_by, "value", str(disabled_by))

        matches.append(
            EntityRegistryMatch(
                entity_id=entry.entity_id,
                unique_id=entry.unique_id,
                device_id=entry.device_id,
                config_entry_id=entry.config_entry_id,
                disabled_by=disabled_by,
            )
        )

    return sorted(matches, key=lambda entity: entity.entity_id)
