"""Huawei Solar entity adapter."""

import logging
from dataclasses import dataclass
from typing import Final

from homeassistant.core import HomeAssistant

from .entity_resolver import EntityRegistryMatch, find_registry_entities

_LOGGER = logging.getLogger(__name__)

HUAWEI_DOMAIN = "huawei_solar"


@dataclass(frozen=True, slots=True)
class HuaweiEntityDefinition:
    """Definition of a Huawei entity required by SEM."""

    domain: str
    translation_key: str


# SEM's required Huawei entities. Add mappings only after their
# translation keys have been verified against the Huawei integration.
HUAWEI_ENTITY_MAP: Final[dict[str, HuaweiEntityDefinition]] = {
    "battery_working_mode": HuaweiEntityDefinition(
        domain="select",
        translation_key="storage_working_mode_settings",
    ),
}


def resolve_huawei_entities(
    hass: HomeAssistant,
) -> dict[str, list[EntityRegistryMatch]]:
    """Resolve all configured Huawei entities from the entity registry."""
    resolved: dict[str, list[EntityRegistryMatch]] = {}

    for function, definition in HUAWEI_ENTITY_MAP.items():
        resolved[function] = find_registry_entities(
            hass,
            platform=HUAWEI_DOMAIN,
            domain=definition.domain,
            translation_key=definition.translation_key,
        )

    return resolved


def log_huawei_entities(hass: HomeAssistant) -> None:
    """Log discovered Huawei entities without selecting ambiguous matches."""
    resolved = resolve_huawei_entities(hass)

    for function, matches in resolved.items():
        active = [entity for entity in matches if entity.disabled_by is None]
        disabled = [entity for entity in matches if entity.disabled_by is not None]

        if len(active) == 1:
            entity = active[0]
            state = hass.states.get(entity.entity_id)
            state_value = state.state if state is not None else "not_loaded"

            _LOGGER.info(
                "Huawei resolver: %s found. Entity ID: %s | Unique ID: %s | State: %s",
                function,
                entity.entity_id,
                entity.unique_id,
                state_value,
            )
        elif len(active) > 1:
            _LOGGER.error(
                "Huawei resolver: Multiple matches for %s: %s. "
                "A specific Huawei device must be selected.",
                function,
                [entity.entity_id for entity in active],
            )
        elif disabled:
            _LOGGER.warning(
                "Huawei resolver: Matching entity for %s is disabled: %s",
                function,
                [entity.entity_id for entity in disabled],
            )
        else:
            _LOGGER.warning(
                "Huawei resolver: No entity found for %s",
                function,
            )
