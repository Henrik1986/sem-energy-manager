"""Huawei Solar entity adapter."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Final

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .adapter import SemSystemAdapter
from .entity_resolver import EntityRegistryMatch, find_registry_entities
from .registry import EntitySource, SemEntityRegistry

_LOGGER = logging.getLogger(__name__)

HUAWEI_DOMAIN = "huawei_solar"
HUAWEI_SYSTEM_ID = "huawei"


@dataclass(frozen=True, slots=True)
class HuaweiEntityDefinition:
    """Definition of a Huawei entity required by SEM."""

    domain: str
    translation_key: str


# Huawei entities required by SEM's current YAML logic and dashboard.
HUAWEI_ENTITY_MAP: Final[dict[str, HuaweiEntityDefinition]] = {
    # Battery
    "battery_working_mode": HuaweiEntityDefinition(
        domain="select",
        translation_key="storage_working_mode_settings",
    ),
    "battery_state_of_capacity": HuaweiEntityDefinition(
        domain="sensor",
        translation_key="storage_state_of_capacity",
    ),
    "battery_rated_capacity": HuaweiEntityDefinition(
        domain="sensor",
        translation_key="storage_rated_capacity",
    ),
    "battery_charge_discharge_power": HuaweiEntityDefinition(
        domain="sensor",
        translation_key="storage_charge_discharge_power",
    ),
    "battery_day_charge": HuaweiEntityDefinition(
        domain="sensor",
        translation_key="storage_current_day_charge_capacity",
    ),
    "battery_day_discharge": HuaweiEntityDefinition(
        domain="sensor",
        translation_key="storage_current_day_discharge_capacity",
    ),

    # Inverter
    "inverter_input_power": HuaweiEntityDefinition(
        domain="sensor",
        translation_key="input_power",
    ),

    # Power meter
    "power_meter_active_power": HuaweiEntityDefinition(
        domain="sensor",
        translation_key="power_meter_active_power",
    ),

    # Battery configuration
    "grid_charge_maximum_power": HuaweiEntityDefinition(
        domain="number",
        translation_key="storage_power_of_charge_from_grid",
    ),
    "maximum_discharging_power": HuaweiEntityDefinition(
        domain="number",
        translation_key="storage_maximum_discharging_power",
    ),
    "end_of_discharge_soc": HuaweiEntityDefinition(
        domain="number",
        translation_key="storage_discharging_cutoff_capacity",
    ),
}


# These entities are attached to the battery or meter child device in HA.
# Their parent device is the inverter that owns the physical system.
HUAWEI_CHILD_DEVICE_ENTITY_KEYS: Final[frozenset[str]] = frozenset({
    "battery_working_mode",
    "battery_state_of_capacity",
    "battery_rated_capacity",
    "battery_charge_discharge_power",
    "battery_day_charge",
    "battery_day_discharge",
    "power_meter_active_power",
})


def _to_entity_sources(
    entity_key: str,
    matches: list[EntityRegistryMatch],
) -> list[EntitySource]:
    """Convert registry matches into physical sources with inverter ownership."""
    sources = []

    for entity in matches:
        owner_device_id = entity.device_id
        if entity_key in HUAWEI_CHILD_DEVICE_ENTITY_KEYS:
            owner_device_id = entity.parent_device_id or entity.device_id

        sources.append(
            EntitySource(
                entity_id=entity.entity_id,
                unique_id=entity.unique_id,
                platform=HUAWEI_DOMAIN,
                device_id=entity.device_id,
                parent_device_id=entity.parent_device_id,
                root_device_id=entity.root_device_id,
                owner_device_id=owner_device_id,
                config_entry_id=entity.config_entry_id,
            )
        )

    return sources


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


@dataclass
class HuaweiInverterGroup:
    """All Huawei entities that belong to one physical inverter."""

    inverter_device_id: str
    root_device_id: str | None
    config_entry_ids: set[str]
    entities: dict[str, list[EntitySource]]


def get_huawei_inverter_groups(
    sem_registry: SemEntityRegistry,
) -> dict[str, HuaweiInverterGroup]:
    """Group all discovered Huawei entity sources by their owning inverter."""
    groups: dict[str, HuaweiInverterGroup] = {}

    for entity_key in HUAWEI_ENTITY_MAP:
        for source in sem_registry.get_entity_sources(
            HUAWEI_SYSTEM_ID,
            entity_key,
        ):
            inverter_device_id = (
                source.owner_device_id or source.device_id
            )
            if inverter_device_id is None:
                continue

            group = groups.get(inverter_device_id)
            if group is None:
                group = HuaweiInverterGroup(
                    inverter_device_id=inverter_device_id,
                    root_device_id=source.root_device_id,
                    config_entry_ids=set(),
                    entities={},
                )
                groups[inverter_device_id] = group

            if source.config_entry_id is not None:
                group.config_entry_ids.add(source.config_entry_id)

            group.entities.setdefault(entity_key, []).append(source)

    return dict(sorted(groups.items()))


def register_huawei_entities(
    hass: HomeAssistant,
    sem_registry: SemEntityRegistry,
) -> None:
    """Resolve Huawei entities and store the results in the SEM registry."""
    sem_registry.register_system(
        HUAWEI_SYSTEM_ID,
        configured=True,
        required_entities=list(HUAWEI_ENTITY_MAP),
    )

    resolved = resolve_huawei_entities(hass)

    for function, matches in resolved.items():
        active = [entity for entity in matches if entity.disabled_by is None]
        disabled = [entity for entity in matches if entity.disabled_by is not None]

        if function == "battery_rated_capacity":
            battery_entities = resolved.get("battery_state_of_capacity", [])
            battery_device_ids = {
                entity.device_id
                for entity in battery_entities
                if entity.device_id is not None
                and entity.disabled_by is None
            }

            active_by_device = {
                entity.device_id: entity
                for entity in active
                if entity.device_id is not None
            }
            disabled_by_device = {
                entity.device_id: entity
                for entity in disabled
                if entity.device_id is not None
            }

            disabled_batteries = sorted(
                device_id
                for device_id in battery_device_ids
                if device_id in disabled_by_device
            )
            missing_batteries = sorted(
                device_id
                for device_id in battery_device_ids
                if device_id not in active_by_device
            )

            if disabled_batteries:
                disabled_ids = [
                    disabled_by_device[device_id].entity_id
                    for device_id in disabled_batteries
                ]
                sem_registry.set_entity_result(
                    HUAWEI_SYSTEM_ID,
                    function,
                    status="disabled",
                    platform=HUAWEI_DOMAIN,
                    matches=disabled_ids,
                    sources=_to_entity_sources(
                        function,
                        [
                            disabled_by_device[device_id]
                            for device_id in disabled_batteries
                        ],
                    ),
                )
                _LOGGER.error(
                    "Huawei resolver: Rated Capacity is disabled for battery device IDs: %s. "
                    "Enable the Rated Capacity sensor for every battery system.",
                    disabled_batteries,
                )
            elif missing_batteries or not battery_device_ids:
                missing_ids = [
                    active_by_device[device_id].entity_id
                    for device_id in battery_device_ids
                    if device_id in active_by_device
                ]
                sem_registry.set_entity_result(
                    HUAWEI_SYSTEM_ID,
                    function,
                    status="missing",
                    platform=HUAWEI_DOMAIN,
                    matches=missing_ids,
                    sources=_to_entity_sources(
                        function,
                        [
                            active_by_device[device_id]
                            for device_id in sorted(active_by_device)
                            if device_id in battery_device_ids
                        ],
                    ),
                )
                _LOGGER.error(
                    "Huawei resolver: Rated Capacity is missing for battery device IDs: %s.",
                    missing_batteries or "no battery devices identified",
                )
            else:
                capacity_matches = [
                    active_by_device[device_id]
                    for device_id in sorted(battery_device_ids)
                ]
                active_ids = [
                    entity.entity_id for entity in capacity_matches
                ]
                first = capacity_matches[0]
                sem_registry.set_entity_result(
                    HUAWEI_SYSTEM_ID,
                    function,
                    status="found",
                    entity_id=first.entity_id,
                    unique_id=first.unique_id,
                    platform=HUAWEI_DOMAIN,
                    device_id=first.device_id,
                    config_entry_id=first.config_entry_id,
                    matches=active_ids,
                    sources=_to_entity_sources(function, capacity_matches),
                )
                _LOGGER.info(
                    "Huawei resolver: Rated Capacity is enabled for all %s battery systems: %s",
                    len(battery_device_ids),
                    active_ids,
                )

            continue

        if len(active) == 1:
            entity = active[0]
            state = hass.states.get(entity.entity_id)
            state_value = state.state if state is not None else "not_loaded"

            sem_registry.set_entity_result(
                HUAWEI_SYSTEM_ID,
                function,
                status="found",
                entity_id=entity.entity_id,
                unique_id=entity.unique_id,
                platform=HUAWEI_DOMAIN,
                device_id=entity.device_id,
                config_entry_id=entity.config_entry_id,
                matches=[entity.entity_id],
                sources=_to_entity_sources(function, active),
            )

            _LOGGER.info(
                "Huawei resolver: %s found. Entity ID: %s | Unique ID: %s | State: %s",
                function,
                entity.entity_id,
                entity.unique_id,
                state_value,
            )

        elif len(active) > 1:
            match_ids = [entity.entity_id for entity in active]

            sem_registry.set_entity_result(
                HUAWEI_SYSTEM_ID,
                function,
                status="ambiguous",
                platform=HUAWEI_DOMAIN,
                matches=match_ids,
                sources=_to_entity_sources(function, active),
            )

            _LOGGER.error(
                "Huawei resolver: Multiple matches for %s: %s. "
                "All physical sources have been recorded, but aggregation "
                "is not enabled yet.",
                function,
                match_ids,
            )

        elif disabled:
            match_ids = [entity.entity_id for entity in disabled]

            sem_registry.set_entity_result(
                HUAWEI_SYSTEM_ID,
                function,
                status="disabled",
                platform=HUAWEI_DOMAIN,
                matches=match_ids,
                sources=_to_entity_sources(function, disabled),
            )

            _LOGGER.warning(
                "Huawei resolver: Matching entity for %s is disabled: %s",
                function,
                match_ids,
            )

        else:
            sem_registry.set_entity_result(
                HUAWEI_SYSTEM_ID,
                function,
                status="missing",
                platform=HUAWEI_DOMAIN,
            )

            _LOGGER.warning(
                "Huawei resolver: No entity found for %s",
                function,
            )


def mark_huawei_entities_missing(
    sem_registry: SemEntityRegistry,
) -> None:
    """Mark required Huawei entities as missing after setup failure."""
    sem_registry.register_system(
        HUAWEI_SYSTEM_ID,
        configured=True,
        required_entities=list(HUAWEI_ENTITY_MAP),
    )

    for function in HUAWEI_ENTITY_MAP:
        sem_registry.set_entity_result(
            HUAWEI_SYSTEM_ID,
            function,
            status="missing",
            platform=HUAWEI_DOMAIN,
        )


async def async_wait_for_huawei(
    hass: HomeAssistant,
    sem_registry: SemEntityRegistry,
) -> None:
    """Wait until Huawei Solar has completed its setup."""

    terminal_states = {
        ConfigEntryState.LOADED,
        ConfigEntryState.SETUP_ERROR,
        ConfigEntryState.MIGRATION_ERROR,
        ConfigEntryState.FAILED_UNLOAD,
    }

    required_entities = list(HUAWEI_ENTITY_MAP)
    entries = [
        entry
        for entry in hass.config_entries.async_entries(HUAWEI_DOMAIN)
        if entry.disabled_by is None
    ]

    if not entries:
        sem_registry.register_system(
            HUAWEI_SYSTEM_ID,
            configured=False,
            required_entities=required_entities,
        )
        _LOGGER.warning(
            "Huawei resolver: No enabled Huawei Solar config entry found"
        )
        return

    sem_registry.register_system(
        HUAWEI_SYSTEM_ID,
        configured=True,
        required_entities=required_entities,
    )

    _LOGGER.info(
        "Huawei resolver: Waiting for Huawei Solar to finish starting"
    )

    for _ in range(120):
        if hass.is_stopping:
            return

        entries = [
            entry
            for entry in hass.config_entries.async_entries(HUAWEI_DOMAIN)
            if entry.disabled_by is None
        ]

        if not entries:
            sem_registry.register_system(
                HUAWEI_SYSTEM_ID,
                configured=False,
                required_entities=required_entities,
            )
            _LOGGER.warning(
                "Huawei resolver: No enabled Huawei Solar config entry found"
            )
            return

        if all(entry.state in terminal_states for entry in entries):
            if any(entry.state is ConfigEntryState.LOADED for entry in entries):
                register_huawei_entities(hass, sem_registry)
            else:
                mark_huawei_entities_missing(sem_registry)
                _LOGGER.error(
                    "Huawei resolver: Huawei Solar failed to load. States: %s",
                    [(entry.title, entry.state.value) for entry in entries],
                )
            return

        await asyncio.sleep(5)

    _LOGGER.warning(
        "Huawei resolver: Timed out waiting for Huawei Solar (10 minutes)"
    )


class HuaweiSolarAdapter(SemSystemAdapter):
    """Adapter for the Huawei Solar integration."""

    system_id = HUAWEI_SYSTEM_ID
    integration_domain = HUAWEI_DOMAIN

    _WORKING_MODE_MAP: Final[dict[str, str]] = {
        "maximise_self_consumption": "self_consumption",
        "time_of_use_luna2000": "time_of_use",
        "fully_fed_to_grid": "feed_in_priority",
        "fixed_charge_discharge": "fixed_charge_discharge",
        "adaptive": "adaptive",
    }

    async def async_setup(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
    ) -> None:
        """Wait for Huawei Solar and resolve its required entities."""
        await async_wait_for_huawei(hass, registry)

    def get_sem_options(
        self,
        entity_key: str,
        source_options: list[str],
    ) -> list[str]:
        """Expose standard SEM working modes instead of Huawei options."""
        if entity_key != "battery_working_mode":
            return source_options

        return [
            self._WORKING_MODE_MAP[option]
            for option in source_options
            if option in self._WORKING_MODE_MAP
        ]

    def to_source_option(
        self,
        entity_key: str,
        sem_option: str,
        source_options: list[str],
    ) -> str:
        """Translate a standard SEM working mode into a Huawei option."""
        if entity_key != "battery_working_mode":
            return super().to_source_option(
                entity_key,
                sem_option,
                source_options,
            )

        reverse_map = {
            sem_value: source_value
            for source_value, sem_value in self._WORKING_MODE_MAP.items()
        }
        source_option = reverse_map.get(sem_option)

        if source_option is None or source_option not in source_options:
            raise ValueError(
                f"Unsupported SEM battery working mode: {sem_option}"
            )

        return source_option

    def to_sem_option(
        self,
        entity_key: str,
        source_option: str,
    ) -> str | None:
        """Translate a Huawei working mode into its SEM value."""
        if entity_key != "battery_working_mode":
            return source_option

        return self._WORKING_MODE_MAP.get(source_option)

    async def async_execute(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        command: str,
        data: dict[str, Any],
    ) -> None:
        """Execute a supported Huawei-specific operation for SEM."""
        if command == "set_battery_time_of_use_periods":
            service = "set_tou_periods"
            device_key = "battery_working_mode"
            required = {"periods"}
            service_data = {
                "periods": data.get("periods"),
            }

        elif command == "set_grid_export_power_limit":
            service = "set_maximum_feed_grid_power"
            device_key = "inverter_input_power"
            required = {"power"}
            service_data = {
                "power": data.get("power"),
            }

        elif command == "reset_grid_export_power_limit":
            service = "reset_maximum_feed_grid_power"
            device_key = "inverter_input_power"
            required = set()
            service_data = {}

        else:
            raise HomeAssistantError(
                f"Unsupported SEM command for Huawei: {command}"
            )

        missing = [
            key
            for key in required
            if key not in data
        ]
        if missing:
            raise HomeAssistantError(
                f"Missing data for SEM command {command}: {missing}"
            )

        device_id = registry.get_entity_device_id(
            HUAWEI_SYSTEM_ID,
            device_key,
        )
        if device_id is None:
            raise HomeAssistantError(
                f"No Huawei device found for SEM command {command}"
            )

        if not hass.services.has_service(HUAWEI_DOMAIN, service):
            raise HomeAssistantError(
                f"Huawei service is not available: {service}"
            )

        await hass.services.async_call(
            HUAWEI_DOMAIN,
            service,
            {
                "device_id": device_id,
                **service_data,
            },
            blocking=True,
        )
