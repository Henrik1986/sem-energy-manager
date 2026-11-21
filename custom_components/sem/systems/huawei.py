"""Huawei Solar adapter with multi-inverter and multi-battery aggregation."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError

from .adapter import SemSystemAdapter
from .entity_resolver import EntityRegistryMatch, find_registry_entities
from .aggregation import (
    capacity_weighted_soc,
    common_sem_options,
    common_number_metadata,
    group_sources_by_owner,
    plant_meter_value,
    parse_numeric,
    sum_numeric_values,
)
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

BATTERY_ENTITY_KEYS: Final[frozenset[str]] = frozenset({
    "battery_working_mode",
    "battery_state_of_capacity",
    "battery_rated_capacity",
    "battery_charge_discharge_power",
    "battery_day_charge",
    "battery_day_discharge",
})

BATTERY_SETTING_KEYS: Final[frozenset[str]] = frozenset({
    "grid_charge_maximum_power",
    "maximum_discharging_power",
    "end_of_discharge_soc",
})


def _to_entity_sources(
    entity_key: str,
    matches: list[EntityRegistryMatch],
) -> list[EntitySource]:
    """Convert registry matches into physical sources with inverter ownership."""
    sources = []

    for entity in matches:
        owner_device_id = entity.device_id
        if entity.device_type in {"battery", "battery_module", "power_meter"}:
            owner_device_id = entity.parent_device_id or entity.device_id

        sources.append(
            EntitySource(
                entity_id=entity.entity_id,
                unique_id=entity.unique_id,
                platform=HUAWEI_DOMAIN,
                device_id=entity.device_id,
                parent_device_id=entity.parent_device_id,
                root_device_id=entity.root_device_id,
                device_type=entity.device_type,
                owner_device_id=owner_device_id,
                config_entry_id=entity.config_entry_id,
            )
        )

    return sources


def resolve_huawei_entities(
    hass: HomeAssistant,
) -> dict[str, list[EntityRegistryMatch]]:
    """Resolve Huawei entities belonging to currently loaded config entries."""
    loaded_entry_ids = {
        entry.entry_id
        for entry in hass.config_entries.async_entries(HUAWEI_DOMAIN)
        if entry.disabled_by is None and entry.state is ConfigEntryState.LOADED
    }

    resolved: dict[str, list[EntityRegistryMatch]] = {}
    for function, definition in HUAWEI_ENTITY_MAP.items():
        matches = find_registry_entities(
            hass,
            platform=HUAWEI_DOMAIN,
            domain=definition.domain,
            translation_key=definition.translation_key,
        )
        resolved[function] = [
            entity
            for entity in matches
            if entity.config_entry_id in loaded_entry_ids
        ]

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
        sources = sem_registry.get_entity_sources(
            HUAWEI_SYSTEM_ID,
            entity_key,
        )
        for inverter_device_id, grouped_sources in group_sources_by_owner(
            sources
        ).items():
            for source in grouped_sources:
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


def _matches_by_key(
    matches: Sequence[EntityRegistryMatch],
    *,
    key: str,
) -> dict[str, list[EntityRegistryMatch]]:
    """Index matches by the physical device or owning inverter."""
    indexed: dict[str, list[EntityRegistryMatch]] = defaultdict(list)
    for match in matches:
        source = _to_entity_sources(key, [match])[0]
        index_key = (
            match.device_id
            if key in BATTERY_ENTITY_KEYS
            else source.owner_device_id
        )
        if index_key is not None:
            indexed[index_key].append(match)
    return indexed


def _store_expected_sources(
    sem_registry: SemEntityRegistry,
    *,
    entity_key: str,
    active: Sequence[EntityRegistryMatch],
    disabled: Sequence[EntityRegistryMatch],
    expected_ids: Sequence[str],
) -> None:
    """Store one source for every expected physical device."""
    active_index = _matches_by_key(active, key=entity_key)
    disabled_index = _matches_by_key(disabled, key=entity_key)
    expected = list(dict.fromkeys(expected_ids))

    duplicates = {
        device_id: values
        for device_id, values in active_index.items()
        if device_id in expected and len(values) > 1
    }
    if duplicates:
        all_matches = [
            match
            for matches in duplicates.values()
            for match in matches
        ]
        sem_registry.set_entity_result(
            HUAWEI_SYSTEM_ID,
            entity_key,
            status="ambiguous",
            platform=HUAWEI_DOMAIN,
            matches=[match.entity_id for match in all_matches],
            sources=_to_entity_sources(entity_key, all_matches),
        )
        return

    selected = [
        active_index[device_id][0]
        for device_id in expected
        if len(active_index.get(device_id, [])) == 1
    ]
    missing = [
        device_id
        for device_id in expected
        if not active_index.get(device_id)
    ]
    disabled_matches = [
        match
        for device_id in missing
        for match in disabled_index.get(device_id, [])
    ]

    if disabled_matches:
        status = "disabled"
        result_sources = disabled_matches
    elif missing or not expected:
        status = "missing"
        result_sources = selected
    else:
        status = "found"
        result_sources = selected

    sem_registry.set_entity_result(
        HUAWEI_SYSTEM_ID,
        entity_key,
        status=status,
        platform=HUAWEI_DOMAIN,
        matches=[match.entity_id for match in result_sources],
        sources=_to_entity_sources(entity_key, result_sources),
    )

    if status == "found":
        _LOGGER.info(
            "Huawei resolver: %s mapped to %s physical sources",
            entity_key,
            len(result_sources),
        )
    elif status == "disabled":
        _LOGGER.error(
            "Huawei resolver: Required entity '%s' is disabled for device IDs %s. "
            "Disabled entity IDs: %s. Enable the entity in Huawei Solar on each "
            "affected device, then reload SEM.",
            entity_key,
            missing,
            [match.entity_id for match in disabled_matches],
        )
    else:
        _LOGGER.error(
            "Huawei resolver: %s is missing for device IDs %s",
            entity_key,
            missing or "no expected device IDs",
        )


def register_huawei_entities(
    hass: HomeAssistant,
    sem_registry: SemEntityRegistry,
) -> None:
    """Resolve all Huawei devices and register complete source collections."""
    sem_registry.register_system(
        HUAWEI_SYSTEM_ID,
        configured=True,
        required_entities=list(HUAWEI_ENTITY_MAP),
    )
    resolved = resolve_huawei_entities(hass)

    # Identify real connected-storage devices from the aggregate battery entities.
    # Huawei also exposes per-module template sensors with the same translation keys;
    # those are deliberately excluded because they are disabled by default and would
    # double-count the already aggregated connected-energy-storage sensors.
    battery_entities = [
        entity
        for entity_key in BATTERY_ENTITY_KEYS
        for entity in resolved[entity_key]
        if entity.device_id is not None and entity.device_type == "battery"
    ]
    battery_device_ids = sorted({
        entity.device_id for entity in battery_entities
        if entity.device_id is not None
    })
    battery_owner_ids = sorted({
        source.owner_device_id
        for entity in battery_entities
        for source in _to_entity_sources("battery_state_of_capacity", [entity])
        if source.owner_device_id is not None
    })

    active_soc = [
        entity
        for entity in resolved["battery_state_of_capacity"]
        if entity.disabled_by is None
        and entity.device_id is not None
        and entity.device_type == "battery"
    ]
    disabled_soc = [
        entity
        for entity in resolved["battery_state_of_capacity"]
        if entity.disabled_by is not None
        and entity.device_id is not None
        and entity.device_type == "battery"
    ]

    soc_index = _matches_by_key(active_soc, key="battery_state_of_capacity")
    disabled_soc_index = _matches_by_key(
        disabled_soc, key="battery_state_of_capacity"
    )
    duplicate_soc = [
        entity
        for values in soc_index.values()
        if len(values) > 1
        for entity in values
    ]
    disabled_soc_ids = [
        device_id
        for device_id in battery_device_ids
        if disabled_soc_index.get(device_id)
    ]
    missing_soc_ids = [
        device_id
        for device_id in battery_device_ids
        if not soc_index.get(device_id) and not disabled_soc_index.get(device_id)
    ]

    if duplicate_soc:
        soc_status = "ambiguous"
        soc_sources = duplicate_soc
    elif disabled_soc_ids:
        soc_status = "disabled"
        soc_sources = active_soc + disabled_soc
        _LOGGER.error(
            "Huawei resolver: Battery SOC is disabled for battery device IDs: %s",
            disabled_soc_ids,
        )
    elif missing_soc_ids or not battery_device_ids:
        soc_status = "missing"
        soc_sources = active_soc
        _LOGGER.error(
            "Huawei resolver: Battery SOC is missing for battery device IDs: %s",
            missing_soc_ids or "no battery devices identified",
        )
    else:
        soc_status = "found"
        soc_sources = active_soc

    sem_registry.set_entity_result(
        HUAWEI_SYSTEM_ID,
        "battery_state_of_capacity",
        status=soc_status,
        platform=HUAWEI_DOMAIN,
        matches=[entity.entity_id for entity in soc_sources],
        sources=_to_entity_sources(
            "battery_state_of_capacity", soc_sources
        ),
    )

    # Battery capacities must map one-to-one to the same physical battery devices.
    capacity_active = [
        entity
        for entity in resolved["battery_rated_capacity"]
        if entity.disabled_by is None
    ]
    capacity_disabled = [
        entity
        for entity in resolved["battery_rated_capacity"]
        if entity.disabled_by is not None
    ]
    _store_expected_sources(
        sem_registry,
        entity_key="battery_rated_capacity",
        active=capacity_active,
        disabled=capacity_disabled,
        expected_ids=battery_device_ids,
    )

    for entity_key in (
        "battery_working_mode",
        "battery_charge_discharge_power",
        "battery_day_charge",
        "battery_day_discharge",
    ):
        _store_expected_sources(
            sem_registry,
            entity_key=entity_key,
            active=[
                entity for entity in resolved[entity_key]
                if entity.disabled_by is None
            ],
            disabled=[
                entity for entity in resolved[entity_key]
                if entity.disabled_by is not None
            ],
            expected_ids=battery_device_ids,
        )

    # One inverter input-power entity is required for every inverter which has
    # a battery. Extra active inverter sources are also retained (for PV-only units).
    input_active = [
        entity
        for entity in resolved["inverter_input_power"]
        if entity.disabled_by is None
    ]
    input_disabled = [
        entity
        for entity in resolved["inverter_input_power"]
        if entity.disabled_by is not None
    ]
    input_index = _matches_by_key(input_active, key="inverter_input_power")
    duplicate_input = [
        entity
        for values in input_index.values()
        if len(values) > 1
        for entity in values
    ]
    missing_input_owners = [
        owner_id for owner_id in battery_owner_ids if owner_id not in input_index
    ]
    if duplicate_input:
        sem_registry.set_entity_result(
            HUAWEI_SYSTEM_ID,
            "inverter_input_power",
            status="ambiguous",
            platform=HUAWEI_DOMAIN,
            matches=[entity.entity_id for entity in duplicate_input],
            sources=_to_entity_sources("inverter_input_power", duplicate_input),
        )
    elif missing_input_owners:
        disabled_index = _matches_by_key(
            input_disabled, key="inverter_input_power"
        )
        disabled_matches = [
            entity
            for owner_id in missing_input_owners
            for entity in disabled_index.get(owner_id, [])
        ]
        sem_registry.set_entity_result(
            HUAWEI_SYSTEM_ID,
            "inverter_input_power",
            status="disabled" if disabled_matches else "missing",
            platform=HUAWEI_DOMAIN,
            matches=[entity.entity_id for entity in disabled_matches],
            sources=_to_entity_sources(
                "inverter_input_power", disabled_matches
            ),
        )
    elif not input_active:
        sem_registry.set_entity_result(
            HUAWEI_SYSTEM_ID,
            "inverter_input_power",
            status="disabled" if input_disabled else "missing",
            platform=HUAWEI_DOMAIN,
            matches=[entity.entity_id for entity in input_disabled],
            sources=_to_entity_sources("inverter_input_power", input_disabled),
        )
    else:
        sem_registry.set_entity_result(
            HUAWEI_SYSTEM_ID,
            "inverter_input_power",
            status="found",
            platform=HUAWEI_DOMAIN,
            matches=[entity.entity_id for entity in input_active],
            sources=_to_entity_sources("inverter_input_power", input_active),
        )

    # Battery settings belong to an inverter or its connected battery child.
    for entity_key in BATTERY_SETTING_KEYS:
        _store_expected_sources(
            sem_registry,
            entity_key=entity_key,
            active=[
                entity for entity in resolved[entity_key]
                if entity.disabled_by is None
            ],
            disabled=[
                entity for entity in resolved[entity_key]
                if entity.disabled_by is not None
            ],
            expected_ids=battery_owner_ids,
        )

    # The grid meter is plant-wide, not additive. Keep all candidates so the
    # adapter can detect duplicate or conflicting readings rather than summing them.
    meter_active = [
        entity
        for entity in resolved["power_meter_active_power"]
        if entity.disabled_by is None
    ]
    meter_disabled = [
        entity
        for entity in resolved["power_meter_active_power"]
        if entity.disabled_by is not None
    ]
    meter_status = "found" if meter_active else (
        "disabled" if meter_disabled else "missing"
    )
    meter_sources = meter_active if meter_active else meter_disabled
    sem_registry.set_entity_result(
        HUAWEI_SYSTEM_ID,
        "power_meter_active_power",
        status=meter_status,
        platform=HUAWEI_DOMAIN,
        matches=[entity.entity_id for entity in meter_sources],
        sources=_to_entity_sources("power_meter_active_power", meter_sources),
    )

    _LOGGER.info(
        "Huawei resolver: registered %s battery devices across %s inverter owners",
        len(battery_device_ids),
        len(battery_owner_ids),
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
    """Wait until all enabled Huawei Solar entries finish setting up."""
    terminal_states = {
        ConfigEntryState.LOADED,
        ConfigEntryState.SETUP_ERROR,
        ConfigEntryState.MIGRATION_ERROR,
        ConfigEntryState.FAILED_UNLOAD,
    }
    required_entities = list(HUAWEI_ENTITY_MAP)

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
            _LOGGER.warning("Huawei resolver: No enabled Huawei Solar config entry found")
            return

        if all(entry.state in terminal_states for entry in entries):
            loaded = [
                entry for entry in entries
                if entry.state is ConfigEntryState.LOADED
            ]
            if loaded:
                register_huawei_entities(hass, sem_registry)
            else:
                mark_huawei_entities_missing(sem_registry)
                _LOGGER.error(
                    "Huawei resolver: No Huawei Solar config entry loaded: %s",
                    [(entry.title, entry.state.value) for entry in entries],
                )
            return

        await asyncio.sleep(5)

    _LOGGER.warning("Huawei resolver: Timed out waiting for Huawei Solar (10 minutes)")


class HuaweiSolarAdapter(SemSystemAdapter):
    """Adapter for a Huawei plant containing one or more inverters and batteries."""

    system_id = HUAWEI_SYSTEM_ID
    integration_domain = HUAWEI_DOMAIN

    _WORKING_MODE_MAP: Final[dict[str, str]] = {
        "maximise_self_consumption": "self_consumption",
        "time_of_use_luna2000": "time_of_use",
        "fully_fed_to_grid": "feed_in_priority",
        "fixed_charge_discharge": "fixed_charge_discharge",
        "adaptive": "adaptive",
    }

    def __init__(self) -> None:
        self._hass: HomeAssistant | None = None
        self._registry: SemEntityRegistry | None = None

    async def async_setup(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
    ) -> None:
        """Wait for Huawei Solar and resolve every loaded config entry."""
        self._hass = hass
        self._registry = registry
        await async_wait_for_huawei(hass, registry)

    def _sources(self, entity_key: str) -> list[EntitySource]:
        if self._registry is None:
            return []
        return self._registry.get_entity_sources(HUAWEI_SYSTEM_ID, entity_key)

    def _battery_device_ids(self) -> set[str]:
        return {
            source.device_id
            for source in self._sources("battery_state_of_capacity")
            if source.device_id is not None
        }

    def _get_state_values(
        self,
        entity_key: str,
        source_states: Sequence[State | None],
    ) -> list[Any]:
        return [
            state.state if state is not None else None
            for state in source_states
        ]

    def get_sensor_value(
        self,
        entity_key: str,
        source_states: Sequence[State | None],
    ) -> float | str | None:
        """Aggregate physical sensor values into one plant-level value."""
        if not source_states or any(
            state is None or state.state in ("unknown", "unavailable")
            for state in source_states
        ):
            return None

        if entity_key == "battery_state_of_capacity":
            if (
                self._registry is None
                or self._registry.get_entity_status(
                    HUAWEI_SYSTEM_ID, "battery_rated_capacity"
                ) != "found"
            ):
                return None

            soc_sources = self._sources("battery_state_of_capacity")
            cap_sources = self._sources("battery_rated_capacity")
            if len(soc_sources) != len(source_states):
                return None
            soc_by_device = {
                source.device_id: state.state
                for source, state in zip(soc_sources, source_states)
                if source.device_id is not None and state is not None
            }
            if len(soc_by_device) != len(soc_sources):
                return None
            if self._hass is None:
                return None
            capacity_by_device = {
                source.device_id: (
                    self._hass.states.get(source.entity_id).state
                    if self._hass.states.get(source.entity_id) is not None
                    else None
                )
                for source in cap_sources
                if source.device_id is not None
            }
            return capacity_weighted_soc(soc_by_device, capacity_by_device)

        if entity_key == "battery_rated_capacity":
            return sum_numeric_values(
                self._get_state_values(entity_key, source_states)
            )

        if entity_key in {
            "battery_charge_discharge_power",
            "battery_day_charge",
            "battery_day_discharge",
        }:
            sources = self._sources(entity_key)
            if len(sources) != len(source_states):
                return None
            device_ids = {
                source.device_id
                for source in sources
                if source.device_id is not None
            }
            if device_ids != self._battery_device_ids():
                return None
            return sum_numeric_values(self._get_state_values(entity_key, source_states))

        if entity_key == "inverter_input_power":
            return sum_numeric_values(self._get_state_values(entity_key, source_states))

        if entity_key == "power_meter_active_power":
            return plant_meter_value(
                self._get_state_values(entity_key, source_states)
            )

        return super().get_sensor_value(entity_key, source_states)

    def get_sensor_metadata(
        self,
        entity_key: str,
        source_attributes: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Expose metadata only when all physical sources use the same units."""
        return super().get_sensor_metadata(entity_key, source_attributes)

    def get_number_value(
        self,
        entity_key: str,
        source_states: Sequence[State | None],
    ) -> float | None:
        """Expose a setting only if every physical device currently agrees."""
        return super().get_number_value(entity_key, source_states)

    def get_number_metadata(
        self,
        entity_key: str,
        source_attributes: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Expose the intersection of all physical number ranges."""
        return common_number_metadata(source_attributes) or {}

    def get_sem_options(
        self,
        entity_key: str,
        source_options: Sequence[Sequence[str]],
    ) -> list[str]:
        """Expose only common standard battery modes."""
        if entity_key != "battery_working_mode":
            return super().get_sem_options(entity_key, source_options)
        return common_sem_options(source_options, self._WORKING_MODE_MAP)

    def to_source_options(
        self,
        entity_key: str,
        sem_option: str,
        source_options: Sequence[Sequence[str]],
    ) -> list[str]:
        """Translate one SEM battery mode and prevalidate every source."""
        if entity_key != "battery_working_mode":
            return super().to_source_options(
                entity_key, sem_option, source_options
            )

        reverse_map = {
            sem_value: source_value
            for source_value, sem_value in self._WORKING_MODE_MAP.items()
        }
        source_option = reverse_map.get(sem_option)
        if source_option is None or not source_options:
            raise ValueError(f"Unsupported SEM battery working mode: {sem_option}")
        if any(source_option not in options for options in source_options):
            raise ValueError(
                f"Battery working mode {sem_option!r} is not supported by every battery"
            )
        return [source_option for _ in source_options]

    def to_sem_option(
        self,
        entity_key: str,
        source_options: Sequence[str | None],
    ) -> str | None:
        """Translate all Huawei modes and require a common value."""
        if entity_key != "battery_working_mode":
            return super().to_sem_option(entity_key, source_options)

        translated = [
            self._WORKING_MODE_MAP.get(option) if option is not None else None
            for option in source_options
        ]
        return super().to_sem_option(entity_key, translated)

    def get_runtime_diagnostics(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
    ) -> dict[str, Any]:
        """Report setting differences and duplicate meter disagreement."""
        mismatches: list[dict[str, Any]] = []

        mode_sources = registry.get_entity_sources(
            HUAWEI_SYSTEM_ID, "battery_working_mode"
        )
        mode_values = []
        for source in mode_sources:
            state = hass.states.get(source.entity_id)
            value = (
                self._WORKING_MODE_MAP.get(state.state)
                if state is not None else None
            )
            mode_values.append((source.entity_id, value))
        unique_modes = {value for _, value in mode_values}
        if len(unique_modes) > 1 or (mode_values and None in unique_modes):
            mismatches.append({
                "entity": "battery_working_mode",
                "sources": [
                    {"entity_id": entity_id, "value": value}
                    for entity_id, value in mode_values
                ],
            })

        for entity_key in BATTERY_SETTING_KEYS:
            sources = registry.get_entity_sources(HUAWEI_SYSTEM_ID, entity_key)
            values = []
            for source in sources:
                state = hass.states.get(source.entity_id)
                values.append((
                    source.entity_id,
                    parse_numeric(state.state) if state is not None else None,
                ))
            unique_values = {value for _, value in values}
            if len(unique_values) > 1 or (values and None in unique_values):
                mismatches.append({
                    "entity": entity_key,
                    "sources": [
                        {"entity_id": entity_id, "value": value}
                        for entity_id, value in values
                    ],
                })

        meter_sources = registry.get_entity_sources(
            HUAWEI_SYSTEM_ID, "power_meter_active_power"
        )
        meter_values = []
        for source in meter_sources:
            state = hass.states.get(source.entity_id)
            meter_values.append((
                source.entity_id,
                parse_numeric(state.state) if state is not None else None,
            ))
        if len(meter_values) > 1 and plant_meter_value(
            [value for _, value in meter_values]
        ) is None:
            mismatches.append({
                "entity": "power_meter_active_power",
                "message": (
                    "Several Huawei meter entities report conflicting plant values. "
                    "The SEM meter sensor is unavailable to prevent double counting."
                ),
                "sources": [
                    {"entity_id": entity_id, "value": value}
                    for entity_id, value in meter_values
                ],
            })

        groups = get_huawei_inverter_groups(registry)
        return {
            "inverter_count": len(groups),
            "battery_count": len(self._battery_device_ids()),
            "inverters": [
                {
                    "device_id": group.inverter_device_id,
                    "root_device_id": group.root_device_id,
                    "config_entry_ids": sorted(group.config_entry_ids),
                    "entities": {
                        key: [source.entity_id for source in sources]
                        for key, sources in group.entities.items()
                    },
                }
                for group in groups.values()
            ],
            "setting_mismatches": mismatches,
        }

    async def async_execute(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        command: str,
        data: dict[str, Any],
    ) -> None:
        """Execute a Huawei command against every applicable physical device."""
        if command == "set_battery_time_of_use_periods":
            service = "set_tou_periods"
            required = {"periods"}
            service_data = {"periods": data.get("periods")}
        elif command == "set_grid_export_power_limit":
            service = "set_maximum_feed_grid_power"
            required = {"power"}
            service_data = {"power": data.get("power")}
        elif command == "reset_grid_export_power_limit":
            service = "reset_maximum_feed_grid_power"
            device_key = "inverter_input_power"
            required = set()
            service_data = {}
        else:
            raise HomeAssistantError(
                f"Unsupported SEM command for Huawei: {command}"
            )

        missing = required - set(data)
        if missing:
            raise HomeAssistantError(
                f"Missing data for SEM command {command}: {sorted(missing)}"
            )

        source_key = (
            "battery_working_mode"
            if command == "set_battery_time_of_use_periods"
            else "inverter_input_power"
        )
        device_ids = registry.get_entity_device_ids(
            HUAWEI_SYSTEM_ID, source_key
        )
        if not device_ids:
            raise HomeAssistantError(
                f"No Huawei devices found for SEM command {command}"
            )
        if not hass.services.has_service(HUAWEI_DOMAIN, service):
            raise HomeAssistantError(
                f"Huawei service is not available: {service}"
            )

        async def call_device(device_id: str) -> None:
            await hass.services.async_call(
                HUAWEI_DOMAIN,
                service,
                {"device_id": device_id, **service_data},
                blocking=True,
            )

        results = await asyncio.gather(
            *(call_device(device_id) for device_id in device_ids),
            return_exceptions=True,
        )
        failures = [
            f"{device_id}: {result}"
            for device_id, result in zip(device_ids, results)
            if isinstance(result, Exception)
        ]
        if failures:
            raise HomeAssistantError(
                f"Huawei command {command} failed for some devices: "
                + "; ".join(failures)
            )
