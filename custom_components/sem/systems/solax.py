"""Adapter for supported SolaX Modbus inverter families."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .adapter import SemSystemAdapter
from .huawei_aggregation import (
    capacity_weighted_soc,
    parse_numeric,
    plant_meter_value,
    sum_numeric_values,
)
from .registry import EntitySource, SemEntityRegistry

_LOGGER = logging.getLogger(__name__)

SOLAX_DOMAIN = "solax_modbus"
SOLAX_SYSTEM_ID = "solax"
SOLAX_MANUFACTURER = "SolaX Power"

# The standard plugin covers the Gen2-6 product family. Dedicated
# product-line plugins are added only after their read contract is verified.
SUPPORTED_PLUGINS = frozenset({"solax"})

SOLAX_WORKING_MODE_SOURCE_KEY = "charger_use_mode"
SOLAX_NIGHT_CHARGE_SWITCH_SOURCE_KEY = "selfuse_night_charge_enable"
SOLAX_NIGHT_CHARGE_ENTITY_KEY = "battery_night_charge_enable"
SOLAX_TOU_PERIOD2_SWITCH_SOURCE_KEY = "charge_and_discharge_period2_enable"
SOLAX_TOU_PERIOD2_ENTITY_KEY = "battery_tou_period2_enable"
SOLAX_END_OF_DISCHARGE_SOC_SOURCE_KEYS = (
    "selfuse_discharge_min_soc",
    "feedin_discharge_min_soc",
)

# SEM registry key -> SolaX Modbus time entity unique-ID suffix.
SOLAX_TOU_TIME_ENTITY_MAP = {
    "battery_tou_charge_start_1": "charge_start_1",
    "battery_tou_charge_end_1": "charge_end_1",
    "battery_tou_charge_start_2": "charge_start_2",
    "battery_tou_charge_end_2": "charge_end_2",
    "battery_tou_discharge_start_1": "discharge_start_1",
    "battery_tou_discharge_end_1": "discharge_end_1",
    "battery_tou_discharge_start_2": "discharge_start_2",
    "battery_tou_discharge_end_2": "discharge_end_2",
}

SOLAX_TOU_OPTIONAL_ENTITIES = (
    SOLAX_NIGHT_CHARGE_ENTITY_KEY,
    SOLAX_TOU_PERIOD2_ENTITY_KEY,
    *SOLAX_TOU_TIME_ENTITY_MAP,
)

SOLAX_REQUIRED_ENTITIES = (
    "battery_working_mode",
    "battery_state_of_capacity",
    "battery_charge_discharge_power",
    "power_meter_active_power",
)

SOLAX_OPTIONAL_ENTITIES = (
    "battery_rated_capacity",
    "battery_day_charge",
    "battery_day_discharge",
    "inverter_input_power",
    "end_of_discharge_soc",
    *SOLAX_TOU_OPTIONAL_ENTITIES,
)

# SEM key -> (HA domain, SolaX entity key, source scope).
# "battery" means one source per battery-capable inverter, "inverter" means
# one source per SolaX inverter, and "any" means one or more plant meter sources.
SOLAX_ENTITY_MAP = {
    "battery_charge_discharge_power": (
        "sensor",
        "battery_power_charge",
        "battery",
    ),
    "battery_rated_capacity": (
        "sensor",
        "battery_system_installed_capacity",
        "battery",
    ),
    "battery_day_charge": (
        "sensor",
        "battery_input_energy_today",
        "battery",
    ),
    "battery_day_discharge": (
        "sensor",
        "battery_output_energy_today",
        "battery",
    ),
    "inverter_input_power": (
        "sensor",
        "pv_power_total",
        "inverter",
    ),
    "power_meter_active_power": (
        "sensor",
        "measured_power",
        "any",
    ),
}

# Gen5/6 expose the direct combined SOC register under this key. The separate
# battery_capacity entity can fall back to the lowest module SOC when BMS
# capacity data is absent, so SEM must not use it for these generations.
SOLAX_DIRECT_SOC_KEY = "battery_total_capacity_charge"
SOLAX_LEGACY_SOC_KEY = "battery_capacity"
SOLAX_GEN5_BATTERY_KEYS = frozenset(
    {
        "battery_total_capacity_charge",
        "battery_1_capacity_charge",
        "battery_2_capacity_charge",
    }
)
SOLAX_GEN5_MODEL_MARKERS = ("IES", "ULTRA", "VAST")

SOLAX_GEN4_MODEL_PREFIXES = (
    "X1-HYBRID-",
    "X3-HYBRID-",
    "X1-RETROFIT",
    "X3-RETROFIT",
    "X1-TIGO",
    "X3-TIGO",
)
SOLAX_GEN5_MODEL_PREFIXES = (
    "X1-IES-",
    "X3-IES-",
    "X3-ULTRA-",
    "X3-AELIO",
)
SOLAX_TOU_PERIOD_PATTERN = re.compile(
    r"(?P<start>(?:[01]\d|2[0-3]):[0-5]\d)-"
    r"(?P<end>(?:[01]\d|2[0-3]):[0-5]\d)/"
    r"(?P<days>[1-7]{7})/(?P<flag>[+-])"
)

SOLAX_GEN2_MODE_OPTIONS = frozenset(
    {"Self Use Mode", "Force Time Use", "Back Up Mode"}
)
SOLAX_GEN3_MODE_OPTIONS = frozenset(
    {"Self Use Mode", "Force Time Use", "Back Up Mode", "Feedin Priority"}
)
SOLAX_GEN4_PLUS_MODE_OPTIONS = frozenset(
    {
        "Self Use Mode",
        "Feedin Priority",
        "Back Up Mode",
        "Manual Mode",
        "PeakShaving",
        "Smart Schedule",
    }
)


def _detect_generation(options: Sequence[str]) -> str | None:
    """Detect the SolaX generation family from its published mode options."""
    option_set = set(options)
    if SOLAX_GEN2_MODE_OPTIONS.issubset(option_set) and (
        "Feedin Priority" not in option_set
    ):
        return "gen2"
    if SOLAX_GEN3_MODE_OPTIONS.issubset(option_set) and (
        "Manual Mode" not in option_set
    ):
        return "gen3"
    if SOLAX_GEN4_PLUS_MODE_OPTIONS.issubset(option_set) and (
        "Force Time Use" not in option_set
    ):
        return "gen4_plus"
    return None


def _parse_solax_tou_periods(
    value: str,
) -> dict[str, list[tuple[str, str]]]:
    """Validate SEM periods and split charge/discharge windows for SolaX."""
    if not isinstance(value, str) or not value.strip():
        raise HomeAssistantError("The SolaX TOU periods value is empty")

    result: dict[str, list[tuple[str, str]]] = {
        "charge": [],
        "discharge": [],
    }

    for line_number, raw_line in enumerate(value.strip().splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        match = SOLAX_TOU_PERIOD_PATTERN.fullmatch(line)
        if match is None:
            raise HomeAssistantError(
                f"Invalid SolaX TOU period on line {line_number}: {line!r}. "
                "Expected HH:MM-HH:MM/1234567/+ or /-."
            )

        days = match.group("days")
        if len(days) != 7 or set(days) != set("1234567"):
            raise HomeAssistantError(
                "SolaX TOU supports all seven days only; use 1234567."
            )

        start = match.group("start")
        end = match.group("end")
        start_hour, start_minute = (int(part) for part in start.split(":"))
        end_hour, end_minute = (int(part) for part in end.split(":"))
        start_minutes = start_hour * 60 + start_minute
        end_minutes = end_hour * 60 + end_minute
        if start_minutes >= end_minutes:
            raise HomeAssistantError(
                f"Invalid SolaX TOU period {start}-{end}: "
                "periods must start before they end within the same day."
            )

        target = "charge" if match.group("flag") == "+" else "discharge"
        result[target].append((start, end))
        if len(result[target]) > 2:
            raise HomeAssistantError(
                f"SolaX supports at most two {target} periods."
            )

    if not result["charge"] and not result["discharge"]:
        raise HomeAssistantError("No valid SolaX TOU periods were supplied")

    all_windows = sorted(
        (
            (
                int(start[:2]) * 60 + int(start[3:]),
                int(end[:2]) * 60 + int(end[3:]),
                kind,
            )
            for kind, windows in result.items()
            for start, end in windows
        ),
        key=lambda item: item[0],
    )
    for previous, current in zip(all_windows, all_windows[1:]):
        if current[0] < previous[1]:
            raise HomeAssistantError(
                "SolaX TOU charge and discharge periods must not overlap."
            )

    return result


def _entry_plugin(entry: Any) -> str:
    """Return the configured SolaX plugin name."""
    options = getattr(entry, "options", {}) or {}
    data = getattr(entry, "data", {}) or {}
    value = options.get("plugin") or data.get("plugin") or ""
    plugin = str(value).lower()
    if plugin.startswith("plugin_"):
        plugin = plugin[len("plugin_") :]
    if plugin.endswith(".py"):
        plugin = plugin[:-3]
    return plugin


def _entry_state(entry: Any) -> str:
    """Normalize a config-entry state enum or string."""
    state = getattr(entry, "state", "")
    return str(getattr(state, "value", state)).lower()


def _is_inverter_device(device: Any) -> bool:
    """Accept only SolaX inverter devices, not battery or virtual devices."""
    if device is None or getattr(device, "manufacturer", None) != SOLAX_MANUFACTURER:
        return False

    for identifier in getattr(device, "identifiers", set()):
        if (
            isinstance(identifier, (tuple, list))
            and len(identifier) >= 3
            and identifier[0] == SOLAX_DOMAIN
            and identifier[-1] == "inverter"
        ):
            return True

    return False


def _disabled_by(entry: Any) -> str | None:
    """Normalize the entity-registry disabled reason."""
    value = getattr(entry, "disabled_by", None)
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _entity_key_matches(unique_id: str, source_key: str) -> bool:
    """Match SolaX's unique ID suffix without depending on hub-name format."""
    return unique_id.endswith(f"_{source_key}")


def _root_device_id(
    device_registry: dr.DeviceRegistry,
    device_id: str,
) -> str:
    """Resolve the top-level parent of a source device."""
    current_id = device_id
    visited: set[str] = set()

    while current_id not in visited:
        visited.add(current_id)
        device = device_registry.async_get(current_id)
        if device is None:
            break
        parent_id = (
            getattr(device, "parent_device_id", None)
            or getattr(device, "via_device_id", None)
        )
        if parent_id is None:
            break
        current_id = parent_id

    return current_id


def _source_from_entry(
    entity_entry: Any,
    device_registry: dr.DeviceRegistry,
) -> EntitySource:
    """Convert an entity-registry entry to a SEM source."""
    device_id = entity_entry.device_id
    device = (
        device_registry.async_get(device_id)
        if device_id is not None
        else None
    )
    parent_id = (
        getattr(device, "parent_device_id", None)
        or getattr(device, "via_device_id", None)
    ) if device is not None else None

    return EntitySource(
        entity_id=entity_entry.entity_id,
        unique_id=entity_entry.unique_id,
        platform=entity_entry.platform,
        device_id=device_id,
        parent_device_id=parent_id,
        root_device_id=(
            _root_device_id(device_registry, device_id)
            if device_id is not None
            else None
        ),
        device_type="inverter",
        owner_device_id=device_id,
        config_entry_id=entity_entry.config_entry_id,
    )


def _is_gen5_or_gen6(
    device: Any,
    device_entities: Sequence[Any],
) -> bool:
    """Identify Gen5/6 devices where the computed SOC may use an unsafe fallback."""
    model = str(getattr(device, "model", "") or "").upper()
    if any(marker in model for marker in SOLAX_GEN5_MODEL_MARKERS):
        return True

    return any(
        _entity_key_matches(entity.unique_id, key)
        for entity in device_entities
        for key in SOLAX_GEN5_BATTERY_KEYS
    )


def _resolve_expected_sources(
    registry: SemEntityRegistry,
    device_registry: dr.DeviceRegistry,
    *,
    entity_key: str,
    matches: Sequence[Any],
    expected_device_ids: Sequence[str],
) -> None:
    """Resolve exactly one enabled source per expected inverter."""
    active_by_device: dict[str, list[Any]] = defaultdict(list)
    disabled_by_device: dict[str, list[Any]] = defaultdict(list)

    expected = set(expected_device_ids)
    for entity_entry in matches:
        device_id = entity_entry.device_id
        if device_id not in expected:
            continue
        if _disabled_by(entity_entry) is None:
            active_by_device[device_id].append(entity_entry)
        else:
            disabled_by_device[device_id].append(entity_entry)

    duplicates = {
        device_id: entries
        for device_id, entries in active_by_device.items()
        if len(entries) > 1
    }
    missing_ids = [
        device_id
        for device_id in expected_device_ids
        if not active_by_device.get(device_id)
    ]

    all_matches = [
        entry
        for device_id in expected_device_ids
        for entry in (
            active_by_device.get(device_id, [])
            + disabled_by_device.get(device_id, [])
        )
    ]

    if not expected_device_ids:
        status = "missing"
        selected = []
    elif duplicates:
        status = "ambiguous"
        selected = [
            entry
            for device_id in expected_device_ids
            for entry in active_by_device.get(device_id, [])
        ]
    elif missing_ids:
        status = (
            "disabled"
            if any(disabled_by_device.get(device_id) for device_id in missing_ids)
            else "missing"
        )
        selected = all_matches
    else:
        status = "found"
        selected = [
            active_by_device[device_id][0]
            for device_id in expected_device_ids
        ]

    sources = [
        _source_from_entry(entry, device_registry)
        for entry in selected
    ]
    registry.set_entity_result(
        SOLAX_SYSTEM_ID,
        entity_key,
        status=status,
        entity_id=sources[0].entity_id if sources else None,
        unique_id=sources[0].unique_id if sources else None,
        platform=SOLAX_DOMAIN if sources else None,
        device_id=sources[0].device_id if sources else None,
        config_entry_id=sources[0].config_entry_id if sources else None,
        matches=[entry.entity_id for entry in all_matches],
        sources=sources,
    )


def _resolve_paired_number_sources(
    registry: SemEntityRegistry,
    device_registry: dr.DeviceRegistry,
    *,
    entity_key: str,
    matches: Sequence[Any],
    expected_device_ids: Sequence[str],
    source_keys: Sequence[str],
) -> None:
    """Resolve every configured number source key once per battery inverter."""
    active_by_source: dict[tuple[str, str], list[Any]] = defaultdict(list)
    disabled_by_source: dict[tuple[str, str], list[Any]] = defaultdict(list)
    expected_pairs = [
        (device_id, source_key)
        for device_id in expected_device_ids
        for source_key in source_keys
    ]

    for entity_entry in matches:
        device_id = entity_entry.device_id
        if device_id is None or device_id not in expected_device_ids:
            continue
        source_key = next(
            (
                key
                for key in source_keys
                if _entity_key_matches(entity_entry.unique_id, key)
            ),
            None,
        )
        if source_key is None:
            continue

        target = (
            active_by_source
            if _disabled_by(entity_entry) is None
            else disabled_by_source
        )
        target[(device_id, source_key)].append(entity_entry)

    duplicates = [
        pair
        for pair in expected_pairs
        if len(active_by_source.get(pair, [])) > 1
    ]
    missing = [
        pair
        for pair in expected_pairs
        if not active_by_source.get(pair)
    ]
    all_matches = [
        entity_entry
        for pair in expected_pairs
        for entity_entry in (
            active_by_source.get(pair, [])
            + disabled_by_source.get(pair, [])
        )
    ]

    if not expected_device_ids or not source_keys:
        status = "missing"
        selected = []
    elif duplicates:
        status = "ambiguous"
        selected = [
            entity_entry
            for pair in expected_pairs
            for entity_entry in active_by_source.get(pair, [])
        ]
    elif missing:
        status = (
            "disabled"
            if any(disabled_by_source.get(pair) for pair in missing)
            else "missing"
        )
        selected = all_matches
    else:
        status = "found"
        selected = [
            active_by_source[pair][0]
            for pair in expected_pairs
        ]

    sources = [
        _source_from_entry(entity_entry, device_registry)
        for entity_entry in selected
    ]
    registry.set_entity_result(
        SOLAX_SYSTEM_ID,
        entity_key,
        status=status,
        entity_id=sources[0].entity_id if sources else None,
        unique_id=sources[0].unique_id if sources else None,
        platform=SOLAX_DOMAIN if sources else None,
        device_id=sources[0].device_id if sources else None,
        config_entry_id=sources[0].config_entry_id if sources else None,
        matches=[entity_entry.entity_id for entity_entry in all_matches],
        sources=sources,
    )


def _resolve_any_sources(
    registry: SemEntityRegistry,
    device_registry: dr.DeviceRegistry,
    *,
    entity_key: str,
    matches: Sequence[Any],
) -> None:
    """Resolve one or more plant-level sources without requiring one per inverter."""
    active_by_device: dict[str, list[Any]] = defaultdict(list)
    disabled: list[Any] = []

    for entity_entry in matches:
        if _disabled_by(entity_entry) is None:
            active_by_device[entity_entry.device_id].append(entity_entry)
        else:
            disabled.append(entity_entry)

    duplicates = any(len(entries) > 1 for entries in active_by_device.values())
    active = [
        entry
        for device_id in sorted(active_by_device, key=lambda value: str(value))
        for entry in active_by_device[device_id]
    ]

    if duplicates:
        status = "ambiguous"
        selected = active
    elif active:
        status = "found"
        selected = active
    elif disabled:
        status = "disabled"
        selected = disabled
    else:
        status = "missing"
        selected = []

    sources = [
        _source_from_entry(entry, device_registry)
        for entry in selected
    ]
    registry.set_entity_result(
        SOLAX_SYSTEM_ID,
        entity_key,
        status=status,
        entity_id=sources[0].entity_id if sources else None,
        unique_id=sources[0].unique_id if sources else None,
        platform=SOLAX_DOMAIN if sources else None,
        device_id=sources[0].device_id if sources else None,
        config_entry_id=sources[0].config_entry_id if sources else None,
        matches=[
            entry.entity_id
            for entry in active + disabled
        ],
        sources=sources,
    )


class SolaXAdapter(SemSystemAdapter):
    """SolaX adapter with telemetry and generation-specific TOU control."""

    system_id = SOLAX_SYSTEM_ID
    integration_domain = SOLAX_DOMAIN

    def __init__(self) -> None:
        self._hass: HomeAssistant | None = None
        self._registry: SemEntityRegistry | None = None

    async def async_setup(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
    ) -> None:
        """Wait for SolaX, then resolve entities on the inverter devices."""
        self._hass = hass
        self._registry = registry
        entries = [
            entry
            for entry in hass.config_entries.async_entries(SOLAX_DOMAIN)
            if entry.disabled_by is None
            and _entry_plugin(entry) in SUPPORTED_PLUGINS
        ]

        if not entries:
            registry.register_system(
                self.system_id,
                configured=False,
                required_entities=list(SOLAX_REQUIRED_ENTITIES),
                optional_entities=list(SOLAX_OPTIONAL_ENTITIES),
            )
            _LOGGER.debug(
                "No supported SolaX Modbus config entry found; adapter inactive"
            )
            return

        terminal_states = {"loaded", "failed", "not_loaded"}
        for _ in range(60):
            if all(_entry_state(entry) in terminal_states for entry in entries):
                break
            await asyncio.sleep(2)

        loaded_entries = [
            entry for entry in entries if _entry_state(entry) == "loaded"
        ]
        if not loaded_entries:
            registry.register_system(
                self.system_id,
                configured=True,
                required_entities=list(SOLAX_REQUIRED_ENTITIES),
                optional_entities=list(SOLAX_OPTIONAL_ENTITIES),
            )
            for entity_key in SOLAX_REQUIRED_ENTITIES + SOLAX_OPTIONAL_ENTITIES:
                registry.set_entity_result(
                    self.system_id,
                    entity_key,
                    status="missing",
                )
            registry.set_capabilities(
                self.system_id,
                self._build_capabilities(registry),
            )
            _LOGGER.warning(
                "SolaX Modbus is configured but no supported config entry is loaded"
            )
            return

        # Allow platform setup and entity-registry writes to complete.
        await asyncio.sleep(2)
        self._resolve_entities(hass, registry, loaded_entries)

    def _resolve_entities(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        config_entries: Sequence[Any],
    ) -> None:
        """Resolve the supported SolaX source entities."""
        registry.register_system(
            self.system_id,
            configured=True,
            required_entities=list(SOLAX_REQUIRED_ENTITIES),
            optional_entities=list(SOLAX_OPTIONAL_ENTITIES),
        )

        loaded_entry_ids = {entry.entry_id for entry in config_entries}
        entity_registry = er.async_get(hass)
        device_registry = dr.async_get(hass)

        inverter_devices = {}
        for device in device_registry.devices.values():
            configured_entries = set(getattr(device, "config_entries", set()))
            if (
                configured_entries.intersection(loaded_entry_ids)
                and _is_inverter_device(device)
            ):
                inverter_devices[device.id] = device

        inverter_device_ids = sorted(inverter_devices)
        inverter_id_set = set(inverter_device_ids)

        entity_entries = [
            entry
            for entry in entity_registry.entities.values()
            if entry.platform == SOLAX_DOMAIN
            and entry.config_entry_id in loaded_entry_ids
            and entry.device_id in inverter_id_set
        ]

        # Use a real battery telemetry entity as the battery-inverter anchor.
        battery_device_ids = sorted(
            {
                entry.device_id
                for entry in entity_entries
                if entry.domain == "sensor"
                and entry.device_id is not None
                and (
                    _entity_key_matches(entry.unique_id, "battery_power_charge")
                    or _entity_key_matches(entry.unique_id, SOLAX_LEGACY_SOC_KEY)
                    or _entity_key_matches(entry.unique_id, SOLAX_DIRECT_SOC_KEY)
                )
            }
        )
        if not battery_device_ids:
            # Keep missing required telemetry visible instead of silently
            # declaring a configured battery system inactive.
            battery_device_ids = inverter_device_ids

        entities_by_device: dict[str, list[Any]] = defaultdict(list)
        for entry in entity_entries:
            if entry.device_id is not None:
                entities_by_device[entry.device_id].append(entry)

        # Both SolaX discharge thresholds back the single SEM end-of-discharge
        # setting. Require exactly one enabled number entity for each threshold
        # on every battery inverter before exposing the SEM number.
        end_of_discharge_matches = [
            entry
            for entry in entity_entries
            if entry.domain == "number"
            and any(
                _entity_key_matches(entry.unique_id, source_key)
                for source_key in SOLAX_END_OF_DISCHARGE_SOC_SOURCE_KEYS
            )
        ]
        _resolve_paired_number_sources(
            registry,
            device_registry,
            entity_key="end_of_discharge_soc",
            matches=end_of_discharge_matches,
            expected_device_ids=battery_device_ids,
            source_keys=SOLAX_END_OF_DISCHARGE_SOC_SOURCE_KEYS,
        )

        # Resolve SOC with a generation-aware source key.
        soc_matches = []
        soc_expected_ids = []
        for device_id in battery_device_ids:
            device_entries = entities_by_device.get(device_id, [])
            device = inverter_devices.get(device_id)
            gen5_or_6 = _is_gen5_or_gen6(device, device_entries)
            source_key = (
                SOLAX_DIRECT_SOC_KEY
                if gen5_or_6
                else SOLAX_LEGACY_SOC_KEY
            )
            soc_expected_ids.append(device_id)
            soc_matches.extend(
                entry
                for entry in device_entries
                if entry.domain == "sensor"
                and _entity_key_matches(entry.unique_id, source_key)
            )

        _resolve_expected_sources(
            registry,
            device_registry,
            entity_key="battery_state_of_capacity",
            matches=soc_matches,
            expected_device_ids=soc_expected_ids,
        )

        mode_matches = [
            entry
            for entry in entity_entries
            if entry.domain == "select"
            and _entity_key_matches(
                entry.unique_id,
                SOLAX_WORKING_MODE_SOURCE_KEY,
            )
        ]
        _resolve_expected_sources(
            registry,
            device_registry,
            entity_key="battery_working_mode",
            matches=mode_matches,
            expected_device_ids=battery_device_ids,
        )

        night_charge_matches = [
            entry
            for entry in entity_entries
            if entry.domain == "switch"
            and _entity_key_matches(
                entry.unique_id,
                SOLAX_NIGHT_CHARGE_SWITCH_SOURCE_KEY,
            )
        ]
        _resolve_expected_sources(
            registry,
            device_registry,
            entity_key=SOLAX_NIGHT_CHARGE_ENTITY_KEY,
            matches=night_charge_matches,
            expected_device_ids=battery_device_ids,
        )

        period2_matches = [
            entry
            for entry in entity_entries
            if entry.domain == "switch"
            and _entity_key_matches(
                entry.unique_id,
                SOLAX_TOU_PERIOD2_SWITCH_SOURCE_KEY,
            )
        ]
        _resolve_expected_sources(
            registry,
            device_registry,
            entity_key=SOLAX_TOU_PERIOD2_ENTITY_KEY,
            matches=period2_matches,
            expected_device_ids=battery_device_ids,
        )

        for registry_key, source_key in SOLAX_TOU_TIME_ENTITY_MAP.items():
            time_matches = [
                entry
                for entry in entity_entries
                if entry.domain == "time"
                and _entity_key_matches(entry.unique_id, source_key)
            ]
            _resolve_expected_sources(
                registry,
                device_registry,
                entity_key=registry_key,
                matches=time_matches,
                expected_device_ids=battery_device_ids,
            )

        for entity_key, (domain, source_key, scope) in SOLAX_ENTITY_MAP.items():
            matches = [
                entry
                for entry in entity_entries
                if entry.domain == domain
                and _entity_key_matches(entry.unique_id, source_key)
            ]

            if scope == "battery":
                expected_ids = battery_device_ids
            elif scope == "inverter":
                expected_ids = inverter_device_ids
            else:
                _resolve_any_sources(
                    registry,
                    device_registry,
                    entity_key=entity_key,
                    matches=matches,
                )
                continue

            _resolve_expected_sources(
                registry,
                device_registry,
                entity_key=entity_key,
                matches=matches,
                expected_device_ids=expected_ids,
            )

        registry.set_capabilities(
            self.system_id,
            self._build_capabilities(registry),
        )

        _LOGGER.info(
            "SolaX resolver completed: %s inverter device(s), %s battery device(s)",
            len(inverter_device_ids),
            len(battery_device_ids),
        )

    @staticmethod
    def _build_capabilities(registry: SemEntityRegistry) -> dict[str, str]:
        """Describe read availability and generation-specific write support."""
        capabilities = {
            f"read.{entity_key}": (
                registry.get_entity_status(SOLAX_SYSTEM_ID, entity_key)
                or "not_configured"
            )
            for entity_key in (
                "battery_state_of_capacity",
                "battery_charge_discharge_power",
                "battery_rated_capacity",
                "battery_day_charge",
                "battery_day_discharge",
                "inverter_input_power",
                "power_meter_active_power",
            )
        }
        capabilities["read.battery_working_mode"] = (
            registry.get_entity_status(SOLAX_SYSTEM_ID, "battery_working_mode")
            or "not_configured"
        )
        capabilities.update(
            {
                "write.battery_working_mode": (
                    "implemented"
                    if registry.get_entity_status(
                        SOLAX_SYSTEM_ID,
                        "battery_working_mode",
                    ) == "found"
                    else "not_configured"
                ),
                "write.grid_charge_maximum_power": "not_supported",
                "write.maximum_discharging_power": "not_supported",
                "write.end_of_discharge_soc": (
                    "implemented"
                    if registry.get_entity_status(
                        SOLAX_SYSTEM_ID,
                        "end_of_discharge_soc",
                    ) == "found"
                    else "not_configured"
                ),
                "write.grid_export_power_limit": "not_implemented",
                "write.time_of_use": "gen4_5_only",
            }
        )
        return capabilities

    def get_related_entity_ids(self, entity_key: str) -> list[str]:
        """Return auxiliary entities that affect a SEM entity's state."""
        if entity_key != "battery_working_mode" or self._registry is None:
            return []
        return self._registry.get_entity_ids(
            self.system_id,
            SOLAX_NIGHT_CHARGE_ENTITY_KEY,
        )

    def _night_charge_sources_by_device(
        self,
        *,
        enabled_only: bool = False,
    ) -> dict[str, EntitySource]:
        """Return at most one night-charge switch per inverter device."""
        if self._hass is None:
            return {}

        entity_registry = er.async_get(self._hass)
        candidates: dict[str, list[EntitySource]] = defaultdict(list)
        for source in self._get_sources(SOLAX_NIGHT_CHARGE_ENTITY_KEY):
            if source.device_id is None:
                continue
            entity_entry = entity_registry.async_get(source.entity_id)
            if entity_entry is None:
                continue
            if enabled_only and _disabled_by(entity_entry) is not None:
                continue
            candidates[source.device_id].append(source)

        return {
            device_id: sources[0]
            for device_id, sources in candidates.items()
            if len(sources) == 1
        }

    def _mode_generation(self, source: EntitySource) -> str | None:
        """Detect the generation family from the source select's options."""
        if self._hass is None:
            return None
        state = self._hass.states.get(source.entity_id)
        if state is None:
            return None
        options = state.attributes.get("options", [])
        if not isinstance(options, (list, tuple)):
            return None
        return _detect_generation(options)

    def _night_charge_switch_issues(self) -> dict[str, str]:
        """Report missing uniqueness or enablement for known night-charge switches."""
        if self._hass is None:
            return {}

        entity_registry = er.async_get(self._hass)
        sources_by_device: dict[str, list[EntitySource]] = defaultdict(list)
        for source in self._get_sources(SOLAX_NIGHT_CHARGE_ENTITY_KEY):
            if source.device_id is not None:
                sources_by_device[source.device_id].append(source)

        issues = {}
        for device_id, sources in sources_by_device.items():
            if len(sources) != 1:
                issues[device_id] = "ambiguous"
                continue
            entity_entry = entity_registry.async_get(sources[0].entity_id)
            if entity_entry is None or _disabled_by(entity_entry) is not None:
                issues[device_id] = "disabled"
        return issues

    def _is_gen4_or_gen5_device(
        self,
        source: EntitySource,
        options: Sequence[str] | None = None,
    ) -> bool:
        """Accept only explicitly recognized SolaX Gen4 and Gen5 model families."""
        if self._hass is None or source.device_id is None:
            return False

        if options is None:
            state = self._hass.states.get(source.entity_id)
            options = (
                state.attributes.get("options", [])
                if state is not None
                else []
            )

        if _detect_generation(options) != "gen4_plus":
            return False

        device = dr.async_get(self._hass).async_get(source.device_id)
        model = str(getattr(device, "model", "") or "").upper()
        return model.startswith(
            SOLAX_GEN4_MODEL_PREFIXES + SOLAX_GEN5_MODEL_PREFIXES
        )

    def _require_sources_by_device(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        *,
        entity_key: str,
        domain: str,
        device_ids: Sequence[str],
    ) -> list[str]:
        """Require one active, available source entity per inverter device."""
        if registry.get_entity_status(self.system_id, entity_key) != "found":
            raise HomeAssistantError(
                f"SolaX entity {entity_key} is missing, disabled or ambiguous"
            )

        sources = registry.get_entity_sources(self.system_id, entity_key)
        sources_by_device: dict[str, list[EntitySource]] = defaultdict(list)
        for source in sources:
            if source.device_id is None:
                raise HomeAssistantError(
                    f"SolaX entity {source.entity_id} has no inverter device"
                )
            sources_by_device[source.device_id].append(source)

        if set(sources_by_device) != set(device_ids) or any(
            len(sources_by_device[device_id]) != 1
            for device_id in device_ids
        ):
            raise HomeAssistantError(
                f"SolaX entity {entity_key} must resolve exactly once "
                "for every battery inverter"
            )

        entity_ids = []
        for device_id in device_ids:
            entity_id = sources_by_device[device_id][0].entity_id
            if not entity_id.startswith(f"{domain}."):
                raise HomeAssistantError(
                    f"Unexpected source domain for SolaX entity {entity_id}"
                )
            state = hass.states.get(entity_id)
            if state is None or state.state in ("unknown", "unavailable"):
                raise HomeAssistantError(
                    f"SolaX entity {entity_id} is currently unavailable"
                )
            entity_ids.append(entity_id)

        return entity_ids

    def get_sem_options(
        self,
        entity_key: str,
        source_options: Sequence[Sequence[str]],
    ) -> list[str]:
        """Expose SEM modes that every detected SolaX device can implement."""
        if entity_key != "battery_working_mode":
            return super().get_sem_options(entity_key, source_options)

        if (
            self._registry is None
            or self._registry.get_entity_status(
                self.system_id,
                entity_key,
            ) != "found"
        ):
            return []

        sources = self._get_sources(entity_key)
        if not sources or len(sources) != len(source_options):
            return []

        generations = [_detect_generation(options) for options in source_options]
        if any(generation is None for generation in generations):
            return []

        night_charge_issues = self._night_charge_switch_issues()
        if any(
            generation == "gen4_plus"
            and source.device_id in night_charge_issues
            for source, generation in zip(sources, generations)
        ):
            return []

        sem_options = []
        if all("Self Use Mode" in options for options in source_options):
            sem_options.append("self_consumption")

        if all("Feedin Priority" in options for options in source_options):
            sem_options.append("feed_in_priority")

        night_charge_sources = self._night_charge_sources_by_device(
            enabled_only=True
        )
        time_of_use_supported = []
        for source, options, generation in zip(
            sources,
            source_options,
            generations,
        ):
            if generation in {"gen2", "gen3"}:
                time_of_use_supported.append("Force Time Use" in options)
            else:
                time_of_use_supported.append(
                    self._is_gen4_or_gen5_device(source, options)
                    and source.device_id in night_charge_sources
                )

        if time_of_use_supported and all(time_of_use_supported):
            sem_options.append("time_of_use")

        return sem_options

    def to_source_options(
        self,
        entity_key: str,
        sem_option: str,
        source_options: Sequence[Sequence[str]],
    ) -> list[str]:
        """Translate a SEM working mode for each SolaX generation."""
        if entity_key != "battery_working_mode":
            return super().to_source_options(
                entity_key,
                sem_option,
                source_options,
            )

        if (
            self._registry is None
            or self._registry.get_entity_status(
                self.system_id,
                entity_key,
            ) != "found"
        ):
            raise ValueError(
                "SolaX working-mode source is missing, duplicated or ambiguous"
            )

        sources = self._get_sources(entity_key)
        if not sources or len(sources) != len(source_options):
            raise ValueError("No complete set of SolaX working-mode sources found")

        night_charge_sources = self._night_charge_sources_by_device(
            enabled_only=True
        )
        night_charge_issues = self._night_charge_switch_issues()
        translated = []
        for source, options in zip(sources, source_options):
            generation = _detect_generation(options)
            if generation is None:
                raise ValueError(
                    f"Could not identify SolaX generation for {source.entity_id}"
                )
            if (
                generation == "gen4_plus"
                and source.device_id in night_charge_issues
            ):
                raise ValueError(
                    "SolaX Selfuse Night Charge is disabled or ambiguous. "
                    "Enable exactly one switch for each affected Gen4+ inverter."
                )

            if sem_option == "self_consumption":
                source_option = "Self Use Mode"
            elif sem_option == "feed_in_priority":
                source_option = "Feedin Priority"
            elif sem_option == "time_of_use":
                if generation in {"gen2", "gen3"}:
                    source_option = "Force Time Use"
                elif (
                    generation == "gen4_plus"
                    and self._is_gen4_or_gen5_device(source, options)
                    and source.device_id in night_charge_sources
                ):
                    source_option = "Self Use Mode"
                else:
                    raise ValueError(
                        "SolaX time_of_use is not available for every device. "
                        "Gen4+ requires an enabled Selfuse Night Charge switch."
                    )
            else:
                raise ValueError(
                    f"Unsupported SEM battery working mode: {sem_option}"
                )

            if source_option not in options:
                raise ValueError(
                    f"SolaX mode {source_option!r} is not supported by "
                    f"{source.entity_id}"
                )
            translated.append(source_option)

        return translated

    def to_sem_option(
        self,
        entity_key: str,
        source_options: Sequence[str | None],
    ) -> str | None:
        """Translate physical SolaX modes back to the SEM mode."""
        if entity_key != "battery_working_mode":
            return super().to_sem_option(entity_key, source_options)

        if (
            self._registry is None
            or self._registry.get_entity_status(
                self.system_id,
                entity_key,
            ) != "found"
        ):
            return None

        sources = self._get_sources(entity_key)
        if not sources or len(sources) != len(source_options):
            return None

        all_night_charge_sources = self._night_charge_sources_by_device()
        enabled_night_charge_sources = self._night_charge_sources_by_device(
            enabled_only=True
        )
        night_charge_issues = self._night_charge_switch_issues()
        translated = []

        for source, source_option in zip(sources, source_options):
            generation = self._mode_generation(source)
            if generation is None or source_option is None:
                translated.append(None)
                continue

            if generation in {"gen2", "gen3"}:
                mode_map = {
                    "Self Use Mode": "self_consumption",
                    "Force Time Use": "time_of_use",
                    "Feedin Priority": "feed_in_priority",
                }
                translated.append(mode_map.get(source_option))
                continue

            if source_option == "Feedin Priority":
                translated.append("feed_in_priority")
            elif source_option == "Self Use Mode":
                if (
                    not self._is_gen4_or_gen5_device(source)
                    and source.device_id in all_night_charge_sources
                ):
                    translated.append(None)
                    continue
                if source.device_id in night_charge_issues:
                    translated.append(None)
                    continue
                if self._hass is None or source.device_id is None:
                    translated.append(None)
                    continue

                night_source = all_night_charge_sources.get(
                    source.device_id
                )
                enabled_source = enabled_night_charge_sources.get(
                    source.device_id
                )
                if night_source is None:
                    # No corresponding switch is exposed by the integration:
                    # this device cannot be represented as SEM time_of_use.
                    translated.append("self_consumption")
                elif enabled_source is None:
                    # The switch exists but is disabled in the entity registry.
                    translated.append(None)
                else:
                    switch_state = self._hass.states.get(
                        enabled_source.entity_id
                    )
                    if switch_state is None:
                        translated.append(None)
                    elif switch_state.state == "on":
                        translated.append("time_of_use")
                    elif switch_state.state == "off":
                        translated.append("self_consumption")
                    else:
                        translated.append(None)
            else:
                translated.append(None)

        return super().to_sem_option(entity_key, translated)

    async def async_after_select_option(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        entity_key: str,
        sem_option: str,
    ) -> None:
        """Toggle Gen4/5 night charging after changing the SEM working mode."""
        if entity_key != "battery_working_mode":
            return

        night_charge_sources = self._night_charge_sources_by_device(
            enabled_only=True
        )
        all_night_charge_sources = self._night_charge_sources_by_device()
        switch_entity_ids = []
        for source in self._get_sources(entity_key):
            state = hass.states.get(source.entity_id)
            options = (
                state.attributes.get("options", [])
                if state is not None
                else []
            )
            generation = _detect_generation(options)
            if (
                generation != "gen4_plus"
                or not self._is_gen4_or_gen5_device(source, options)
            ):
                continue

            night_source = all_night_charge_sources.get(source.device_id)
            enabled_source = night_charge_sources.get(source.device_id)
            if enabled_source is None:
                if sem_option == "time_of_use":
                    raise HomeAssistantError(
                        "SolaX time_of_use requires an enabled "
                        "Selfuse Night Charge switch on every Gen4/5 device."
                    )
                if night_source is not None:
                    raise HomeAssistantError(
                        "The SolaX Selfuse Night Charge switch is disabled. "
                        "Enable it before changing the working mode."
                    )
                continue

            switch_entity_ids.append(enabled_source.entity_id)

        if switch_entity_ids:
            service = (
                "turn_on"
                if sem_option == "time_of_use"
                else "turn_off"
            )
            await hass.services.async_call(
                "switch",
                service,
                {"entity_id": sorted(set(switch_entity_ids))},
                blocking=True,
            )

    def get_sensor_value(
        self,
        entity_key: str,
        source_states: Sequence[State | None],
    ) -> float | str | None:
        """Normalize SolaX readings to the SEM sensor contract."""
        if not source_states or any(
            state is None or state.state in ("unknown", "unavailable")
            for state in source_states
        ):
            return None

        if entity_key == "battery_state_of_capacity":
            soc_sources = self._get_sources(entity_key)
            if len(soc_sources) != len(source_states):
                return None

            soc_by_device = {}
            for source, state in zip(soc_sources, source_states):
                if source.device_id is None:
                    return None
                value = parse_numeric(state.state)
                if value is None or not 0 <= value <= 100:
                    return None
                if source.device_id in soc_by_device:
                    return None
                soc_by_device[source.device_id] = value

            if len(soc_by_device) == 1:
                return next(iter(soc_by_device.values()))

            capacity_by_device = self._get_capacity_by_device()
            return capacity_weighted_soc(
                soc_by_device,
                capacity_by_device,
            )

        if entity_key == "battery_rated_capacity":
            # SolaX exposes installed capacity in Wh; SEM standardizes it to kWh.
            total_wh = sum_numeric_values(
                state.state for state in source_states if state is not None
            )
            return total_wh / 1000 if total_wh is not None else None

        if entity_key == "power_meter_active_power":
            # Multiple inverter entries can mirror the same plant meter.
            # Compare them; never add duplicate plant-level meter readings.
            return plant_meter_value(
                [state.state for state in source_states if state is not None]
            )

        if entity_key in {
            "battery_charge_discharge_power",
            "battery_day_charge",
            "battery_day_discharge",
            "inverter_input_power",
        }:
            return sum_numeric_values(
                state.state for state in source_states if state is not None
            )

        return super().get_sensor_value(entity_key, source_states)

    def get_sensor_metadata(
        self,
        entity_key: str,
        source_attributes: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Return shared metadata, normalizing installed capacity to kWh."""
        metadata = super().get_sensor_metadata(
            entity_key,
            source_attributes,
        )
        if entity_key == "battery_state_of_capacity":
            return {
                "device_class": "battery",
                "state_class": "measurement",
                "unit_of_measurement": "%",
                "suggested_display_precision": 0,
            }
        if entity_key == "battery_rated_capacity":
            metadata.update(
                {
                    "device_class": "energy",
                    "unit_of_measurement": "kWh",
                    "suggested_display_precision": 2,
                }
            )
        return metadata

    def get_runtime_diagnostics(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
    ) -> dict[str, Any]:
        """Report unsafe aggregation, meter conflicts and working-mode support."""
        errors = []

        soc_sources = registry.get_entity_sources(
            self.system_id,
            "battery_state_of_capacity",
        )
        capacity_by_device = self._get_capacity_by_device()
        soc_device_ids = {
            source.device_id
            for source in soc_sources
            if source.device_id is not None
        }
        if (
            registry.get_entity_status(
                self.system_id,
                "battery_state_of_capacity",
            ) == "found"
            and len(soc_device_ids) > 1
            and (
                registry.get_entity_status(
                    self.system_id,
                    "battery_rated_capacity",
                ) != "found"
                or set(capacity_by_device) != soc_device_ids
            )
        ):
            errors.append(
                {
                    "code": "solax_soc_capacity_missing",
                    "setting": "Batteriets SOC",
                    "message": (
                        "SEM kan inte beräkna ett kapacitetsviktat SOC-värde "
                        "för flera SolaX-växelriktare eftersom installerad "
                        "batterikapacitet saknas eller är inaktiverad."
                    ),
                    "action": (
                        "Aktivera entiteten Battery System Installed Capacity "
                        "för varje SolaX-växelriktare som stöder den. "
                        "Gen2/Gen3 saknar denna standardentitet; använd då "
                        "en enda batterikälla eller avvakta stöd för "
                        "kapacitetsuppgift från annan källa."
                    ),
                    "sources": [],
                }
            )

        meter_sources = registry.get_entity_sources(
            self.system_id,
            "power_meter_active_power",
        )
        meter_values = []
        for source in meter_sources:
            state = hass.states.get(source.entity_id)
            if state is None:
                meter_values.append(None)
            else:
                meter_values.append(parse_numeric(state.state))

        if len(meter_values) > 1 and all(
            value is not None for value in meter_values
        ):
            if plant_meter_value(meter_values) is None:
                errors.append(
                    {
                        "code": "solax_grid_meter_conflict",
                        "setting": "Nätmätarens effekt",
                        "message": (
                            "SolaX:s nätmätarkällor rapporterar motstridiga "
                            "värden. SEM använder inte ett osäkert mätvärde."
                        ),
                        "action": (
                            "Kontrollera att växelriktarna använder samma "
                            "nätmätare och att mätvärdenas tecken och "
                            "anslutningar är korrekta."
                        ),
                        "sources": [
                            {
                                "entity_id": source.entity_id,
                                "value": value,
                            }
                            for source, value in zip(
                                meter_sources,
                                meter_values,
                            )
                        ],
                    }
                )

        working_mode_generations = []
        all_night_charge_sources = self._night_charge_sources_by_device()
        enabled_night_charge_sources = self._night_charge_sources_by_device(
            enabled_only=True
        )
        for source in registry.get_entity_sources(
            self.system_id,
            "battery_working_mode",
        ):
            state = hass.states.get(source.entity_id)
            options = (
                state.attributes.get("options", [])
                if state is not None
                else []
            )
            generation = _detect_generation(options)
            supported_modes = []
            if "Self Use Mode" in options:
                supported_modes.append("self_consumption")
            if "Feedin Priority" in options:
                supported_modes.append("feed_in_priority")
            if generation in {"gen2", "gen3"} and "Force Time Use" in options:
                supported_modes.append("time_of_use")
            elif (
                generation == "gen4_plus"
                and self._is_gen4_or_gen5_device(source, options)
                and source.device_id in enabled_night_charge_sources
            ):
                supported_modes.append("time_of_use")

            device = (
                dr.async_get(hass).async_get(source.device_id)
                if source.device_id is not None
                else None
            )
            working_mode_generations.append(
                {
                    "device_id": source.device_id,
                    "entity_id": source.entity_id,
                    "model": getattr(device, "model", None),
                    "generation": generation or "unknown",
                    "supported_sem_modes": supported_modes,
                    "night_charge_switch": (
                        enabled_night_charge_sources[source.device_id].entity_id
                        if source.device_id in enabled_night_charge_sources
                        else None
                    ),
                    "night_charge_switch_disabled": (
                        source.device_id in all_night_charge_sources
                        and source.device_id not in enabled_night_charge_sources
                    ),
                }
            )

        diagnostics = {}
        if errors:
            diagnostics["errors"] = errors
        if working_mode_generations:
            diagnostics["working_mode_generations"] = working_mode_generations
        return diagnostics

    def _get_sources(self, entity_key: str) -> list[EntitySource]:
        """Return resolved sources for one SEM key."""
        if self._hass is None or self._registry is None:
            return []
        # State aggregation uses the same ordering as get_entity_ids().
        return self._registry.get_entity_sources(self.system_id, entity_key)

    def _get_capacity_by_device(self) -> dict[str, float]:
        """Read installed capacity for every inverter, in its native Wh value."""
        if self._hass is None or self._registry is None:
            return {}
        if (
            self._registry.get_entity_status(
                self.system_id,
                "battery_rated_capacity",
            ) != "found"
        ):
            return {}

        capacities = {}
        for source in self._get_sources("battery_rated_capacity"):
            if source.device_id is None:
                return {}
            state = self._hass.states.get(source.entity_id)
            value = (
                parse_numeric(state.state)
                if state is not None
                else None
            )
            if value is None or value <= 0:
                return {}
            if source.device_id in capacities:
                return {}
            capacities[source.device_id] = value
        return capacities

    async def async_execute(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        command: str,
        data: dict[str, Any],
    ) -> None:
        """Execute supported SolaX commands with generation-specific guards."""
        if command != "set_battery_time_of_use_periods":
            raise HomeAssistantError(
                f"Unsupported SEM command for SolaX: {command}"
            )
        if "periods" not in data:
            raise HomeAssistantError(
                "Missing data for SEM command set_battery_time_of_use_periods"
            )

        periods = _parse_solax_tou_periods(data["periods"])
        mode_sources = registry.get_entity_sources(
            self.system_id,
            "battery_working_mode",
        )
        if (
            registry.get_entity_status(
                self.system_id,
                "battery_working_mode",
            ) != "found"
            or not mode_sources
        ):
            raise HomeAssistantError(
                "SolaX battery working-mode entities are not fully resolved"
            )

        device_ids = []
        for source in mode_sources:
            if source.device_id is None:
                raise HomeAssistantError(
                    f"SolaX working-mode entity {source.entity_id} "
                    "has no inverter device"
                )
            if not self._is_gen4_or_gen5_device(source):
                device = dr.async_get(hass).async_get(source.device_id)
                model = getattr(device, "model", None)
                raise HomeAssistantError(
                    "The SEM TOU action is supported only on SolaX Gen4/Gen5. "
                    f"Device {model or source.entity_id} is not a verified "
                    "Gen4/Gen5 model."
                )
            device_ids.append(source.device_id)

        if len(set(device_ids)) != len(device_ids):
            raise HomeAssistantError(
                "SolaX working-mode sources contain duplicate inverter devices"
            )
        device_ids = sorted(device_ids)

        # Require every related entity on every supported battery inverter.
        night_charge_ids = self._require_sources_by_device(
            hass,
            registry,
            entity_key=SOLAX_NIGHT_CHARGE_ENTITY_KEY,
            domain="switch",
            device_ids=device_ids,
        )
        period2_ids = self._require_sources_by_device(
            hass,
            registry,
            entity_key=SOLAX_TOU_PERIOD2_ENTITY_KEY,
            domain="switch",
            device_ids=device_ids,
        )
        time_entity_ids = {
            registry_key: self._require_sources_by_device(
                hass,
                registry,
                entity_key=registry_key,
                domain="time",
                device_ids=device_ids,
            )
            for registry_key in SOLAX_TOU_TIME_ENTITY_MAP
        }

        charge_periods = periods["charge"]
        discharge_periods = periods["discharge"]
        period2_required = (
            len(charge_periods) > 1 or len(discharge_periods) > 1
        )

        # SolaX defaults the normal self-use discharge window to all day.
        # Keep that behaviour when SEM supplies no explicit discharge periods.
        if discharge_periods:
            discharge_slots = list(discharge_periods)
        else:
            discharge_slots = [("00:00", "23:59")]
        charge_slots = list(charge_periods)

        disabled_window = ("00:00", "00:00")
        while len(charge_slots) < 2:
            charge_slots.append(disabled_window)
        while len(discharge_slots) < 2:
            discharge_slots.append(disabled_window)

        time_values = {
            "battery_tou_charge_start_1": charge_slots[0][0],
            "battery_tou_charge_end_1": charge_slots[0][1],
            "battery_tou_charge_start_2": charge_slots[1][0],
            "battery_tou_charge_end_2": charge_slots[1][1],
            "battery_tou_discharge_start_1": discharge_slots[0][0],
            "battery_tou_discharge_end_1": discharge_slots[0][1],
            "battery_tou_discharge_start_2": discharge_slots[1][0],
            "battery_tou_discharge_end_2": discharge_slots[1][1],
        }

        async def set_switches(
            entity_ids: Sequence[str],
            *,
            turn_on: bool,
        ) -> None:
            """Write only switch states that actually need to change."""
            target_state = "on" if turn_on else "off"
            targets = [
                entity_id
                for entity_id in entity_ids
                if hass.states.get(entity_id) is not None
                and hass.states.get(entity_id).state != target_state
            ]
            if targets:
                await hass.services.async_call(
                    "switch",
                    "turn_on" if turn_on else "turn_off",
                    {"entity_id": targets},
                    blocking=True,
                )

        # Disable grid charging and Period 2 while all time registers are updated.
        await set_switches(night_charge_ids, turn_on=False)
        await set_switches(period2_ids, turn_on=False)

        for registry_key, value in time_values.items():
            target_ids = [
                entity_id
                for entity_id in time_entity_ids[registry_key]
                if hass.states.get(entity_id) is not None
                and hass.states.get(entity_id).state[:5] != value
            ]
            if target_ids:
                await hass.services.async_call(
                    "time",
                    "set_value",
                    {
                        "entity_id": target_ids,
                        "time": f"{value}:00",
                    },
                    blocking=True,
                )

        if period2_required:
            await set_switches(period2_ids, turn_on=True)

        # Enable grid charging only after the charge time windows are written.
        if charge_periods:
            await set_switches(night_charge_ids, turn_on=True)
