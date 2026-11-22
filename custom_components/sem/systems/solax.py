"""Adapter for supported SolaX Modbus inverter families."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .adapter import SemSystemAdapter
from .capacity_aggregation import (
    complete_capacity_map,
    normalize_capacity_kwh,
    total_capacity_kwh,
)
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
SOLAX_GRID_CHARGE_MAXIMUM_POWER_KEY = "grid_charge_maximum_power"
SOLAX_GRID_CHARGE_STORE_VERSION = 1
SOLAX_GRID_CHARGE_NUMBER_METADATA = {
    "min_value": 0,
    "max_value": 100_000,
    "step": 1,
    "unit_of_measurement": "W",
    "mode": "box",
}
SOLAX_MAXIMUM_DISCHARGING_POWER_KEY = "maximum_discharging_power"
SOLAX_MAXIMUM_DISCHARGING_STORE_VERSION = 1
SOLAX_MAXIMUM_DISCHARGING_NUMBER_METADATA = {
    "min_value": 0,
    "max_value": 100_000,
    "step": 1,
    "unit_of_measurement": "W",
    "mode": "box",
}
SOLAX_GEN2_REMOTE_POWER_SETPOINT_SOURCE_KEY = "grid_export_limit"
SOLAX_GRID_EXPORT_POWER_LIMIT_KEY = "grid_export_power_limit"
SOLAX_GEN4_REMOTE_CONTROL_MODE_SOURCE_KEY = "remotecontrol_power_control_mode"
SOLAX_GEN4_REMOTE_CONTROL_TRIGGER_SOURCE_KEY = "powercontrolmode8_trigger"
SOLAX_GEN4_AUTOREPEAT_DURATION_SOURCE_KEY = "remotecontrol_autorepeat_duration"
SOLAX_GEN4_NO_DISCHARGE_MODE = "Enabled No Discharge"
SOLAX_GEN4_DISABLED_MODE = "Disabled"
SOLAX_GEN4_AUTOREPEAT_DURATION_SECONDS = 172_800

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
    SOLAX_GRID_EXPORT_POWER_LIMIT_KEY,
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
    SOLAX_GRID_EXPORT_POWER_LIMIT_KEY: (
        "number",
        "export_control_user_limit",
        "inverter",
    ),
}

# Gen5/6 expose the direct combined SOC register under this key. The separate
# battery_capacity entity can fall back to the lowest module SOC when BMS
# capacity data is absent, so SEM must not use it for these generations.
SOLAX_DIRECT_SOC_KEY = "battery_total_capacity_charge"
SOLAX_SOC_SOURCE_KEY = "battery_capacity"
SOLAX_CAPACITY_SOURCE_KEYS = (
    "battery_system_installed_capacity",
    "battery_install_capacity",
)
SOLAX_TOTAL_CAPACITY_HELPER = "input_number.sem_battery_total_capacity_kwh"
SOLAX_BATTERY_DAY_CHARGE_FALLBACK_ENTITY = (
    "sensor.sem_battery_charge_energy_daily"
)
SOLAX_BATTERY_DAY_DISCHARGE_FALLBACK_ENTITY = (
    "sensor.sem_battery_discharge_energy_daily"
)

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


def _resolve_capacity_sources(
    registry: SemEntityRegistry,
    device_registry: dr.DeviceRegistry,
    *,
    matches: Sequence[Any],
    expected_device_ids: Sequence[str],
) -> None:
    """Resolve optional installed-capacity sensors without discarding partial data."""
    active_by_device: dict[str, list[Any]] = defaultdict(list)
    disabled_by_device: dict[str, list[Any]] = defaultdict(list)
    expected = set(expected_device_ids)

    for entity_entry in matches:
        device_id = entity_entry.device_id
        if device_id not in expected:
            continue

        target = (
            active_by_device
            if _disabled_by(entity_entry) is None
            else disabled_by_device
        )
        target[device_id].append(entity_entry)

    duplicates = {
        device_id
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
        # Keep enabled sources for the devices that did resolve. They can be
        # combined with the configured plant total when exactly one is missing.
        selected = [
            active_by_device[device_id][0]
            for device_id in expected_device_ids
            if active_by_device.get(device_id)
        ]
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
        "battery_rated_capacity",
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
        self._grid_charge_store: Store | None = None
        self._grid_charge_value: float | None = None
        self._grid_charge_override: dict[str, Any] | None = None
        self._grid_charge_lock = asyncio.Lock()
        self._maximum_discharging_store: Store | None = None
        self._maximum_discharging_value: float | None = None
        self._maximum_discharging_override: dict[str, Any] | None = None
        self._battery_device_ids: list[str] = []

    async def async_setup(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
    ) -> None:
        """Wait for SolaX, then resolve entities on the inverter devices."""
        self._hass = hass
        self._registry = registry
        self._grid_charge_store = Store(
            hass,
            SOLAX_GRID_CHARGE_STORE_VERSION,
            f"{SOLAX_SYSTEM_ID}_grid_charge_override",
        )
        stored_grid_charge = await self._grid_charge_store.async_load()
        if isinstance(stored_grid_charge, Mapping):
            stored_value = parse_numeric(stored_grid_charge.get("value"))
            self._grid_charge_value = stored_value
            stored_override = stored_grid_charge.get("override")
            if isinstance(stored_override, dict):
                self._grid_charge_override = stored_override

        self._maximum_discharging_store = Store(
            hass,
            SOLAX_MAXIMUM_DISCHARGING_STORE_VERSION,
            f"{SOLAX_SYSTEM_ID}_maximum_discharging_override",
        )
        stored_maximum_discharging = (
            await self._maximum_discharging_store.async_load()
        )
        if isinstance(stored_maximum_discharging, Mapping):
            self._maximum_discharging_value = parse_numeric(
                stored_maximum_discharging.get("value")
            )
            stored_override = stored_maximum_discharging.get("override")
            if isinstance(stored_override, dict):
                self._maximum_discharging_override = stored_override

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
        if self._grid_charge_override is not None:
            try:
                await self._apply_grid_charge_override(
                    hass,
                    registry,
                    self._grid_charge_override,
                )
            except HomeAssistantError:
                _LOGGER.exception(
                    "Could not reapply the saved SolaX grid-charge block"
                )
        if self._maximum_discharging_override is not None:
            try:
                await self._apply_maximum_discharging_override(
                    hass,
                    registry,
                    self._maximum_discharging_override,
                )
            except HomeAssistantError:
                _LOGGER.exception(
                    "Could not reapply the saved SolaX no-discharge mode"
                )

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
                    or _entity_key_matches(entry.unique_id, SOLAX_SOC_SOURCE_KEY)
                    or _entity_key_matches(entry.unique_id, SOLAX_DIRECT_SOC_KEY)
                )
            }
        )
        if not battery_device_ids:
            # Keep missing required telemetry visible instead of silently
            # declaring a configured battery system inactive.
            battery_device_ids = inverter_device_ids

        self._battery_device_ids = list(battery_device_ids)

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

        # Use SolaX's common battery_capacity entity for Gen2-6. In Gen5/6
        # the upstream integration computes this value from the total SOC
        # register or the module SOC values when BMS data is incomplete.
        soc_matches = [
            entry
            for entry in entity_entries
            if entry.domain == "sensor"
            and _entity_key_matches(entry.unique_id, SOLAX_SOC_SOURCE_KEY)
        ]
        _resolve_expected_sources(
            registry,
            device_registry,
            entity_key="battery_state_of_capacity",
            matches=soc_matches,
            expected_device_ids=battery_device_ids,
        )

        # Gen3 exposes Battery Install Capacity in kWh. Gen4-6 expose
        # Battery System Installed Capacity in Wh. Gen2 has no equivalent
        # standard installed-capacity entity in the upstream integration.
        capacity_matches = [
            entry
            for entry in entity_entries
            if entry.domain == "sensor"
            and any(
                _entity_key_matches(entry.unique_id, source_key)
                for source_key in SOLAX_CAPACITY_SOURCE_KEYS
            )
        ]
        _resolve_capacity_sources(
            registry,
            device_registry,
            matches=capacity_matches,
            expected_device_ids=battery_device_ids,
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

        # Prefer SolaX's native daily battery-charge sensor when it resolves
        # for every battery inverter. Otherwise use SEM's local aggregate
        # sensor, which derives the daily total from the battery power data.
        if registry.get_entity_status(
            self.system_id,
            "battery_day_charge",
        ) != "found":
            fallback_state = hass.states.get(
                SOLAX_BATTERY_DAY_CHARGE_FALLBACK_ENTITY
            )
            if fallback_state is not None:
                registry.set_entity_result(
                    self.system_id,
                    "battery_day_charge",
                    status="found",
                    entity_id=SOLAX_BATTERY_DAY_CHARGE_FALLBACK_ENTITY,
                    matches=[SOLAX_BATTERY_DAY_CHARGE_FALLBACK_ENTITY],
                    sources=[
                        EntitySource(
                            entity_id=SOLAX_BATTERY_DAY_CHARGE_FALLBACK_ENTITY,
                        )
                    ],
                )
                _LOGGER.info(
                    "SolaX resolver: using local fallback %s for battery_day_charge",
                    SOLAX_BATTERY_DAY_CHARGE_FALLBACK_ENTITY,
                )

        # Prefer SolaX's native daily battery-discharge sensor when it resolves
        # for every battery inverter. Otherwise use SEM's local aggregate
        # sensor, which derives the daily total from the battery power data.
        if registry.get_entity_status(
            self.system_id,
            "battery_day_discharge",
        ) != "found":
            fallback_state = hass.states.get(
                SOLAX_BATTERY_DAY_DISCHARGE_FALLBACK_ENTITY
            )
            if fallback_state is not None:
                registry.set_entity_result(
                    self.system_id,
                    "battery_day_discharge",
                    status="found",
                    entity_id=SOLAX_BATTERY_DAY_DISCHARGE_FALLBACK_ENTITY,
                    matches=[SOLAX_BATTERY_DAY_DISCHARGE_FALLBACK_ENTITY],
                    sources=[
                        EntitySource(
                            entity_id=SOLAX_BATTERY_DAY_DISCHARGE_FALLBACK_ENTITY,
                        )
                    ],
                )
                _LOGGER.info(
                    "SolaX resolver: using local fallback %s for battery_day_discharge",
                    SOLAX_BATTERY_DAY_DISCHARGE_FALLBACK_ENTITY,
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

    def _build_capabilities(self, registry: SemEntityRegistry) -> dict[str, str]:
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
                "write.grid_charge_maximum_power": (
                    "implemented"
                    if self._grid_charge_override_supported()
                    else "not_supported"
                ),
                "write.maximum_discharging_power": (
                    "implemented"
                    if self._maximum_discharging_override_supported()
                    else "not_supported"
                ),
                "write.end_of_discharge_soc": (
                    "implemented"
                    if registry.get_entity_status(
                        SOLAX_SYSTEM_ID,
                        "end_of_discharge_soc",
                    ) == "found"
                    else "not_configured"
                ),
                "write.grid_export_power_limit": (
                    "implemented"
                    if registry.get_entity_status(
                        SOLAX_SYSTEM_ID,
                        SOLAX_GRID_EXPORT_POWER_LIMIT_KEY,
                    ) == "found"
                    else "not_configured"
                ),
                "write.time_of_use": "gen4_5_only",
            }
        )
        return capabilities

    def has_virtual_number(self, entity_key: str) -> bool:
        """Expose adapter-managed SolaX power permissions as virtual SEM numbers."""
        if entity_key == SOLAX_GRID_CHARGE_MAXIMUM_POWER_KEY:
            return self._grid_charge_override_supported()
        if entity_key == SOLAX_MAXIMUM_DISCHARGING_POWER_KEY:
            return self._maximum_discharging_override_supported()
        return False

    def get_virtual_number_value(self, entity_key: str) -> float | None:
        """Return the last requested value for an adapter-managed SolaX number."""
        if entity_key == SOLAX_GRID_CHARGE_MAXIMUM_POWER_KEY:
            return self._grid_charge_value
        if entity_key == SOLAX_MAXIMUM_DISCHARGING_POWER_KEY:
            return self._maximum_discharging_value
        return None

    def get_virtual_number_metadata(self, entity_key: str) -> dict[str, Any]:
        """Expose watt-based values; SolaX uses zero/non-zero as permission."""
        if entity_key == SOLAX_GRID_CHARGE_MAXIMUM_POWER_KEY:
            return dict(SOLAX_GRID_CHARGE_NUMBER_METADATA)
        if entity_key == SOLAX_MAXIMUM_DISCHARGING_POWER_KEY:
            return dict(SOLAX_MAXIMUM_DISCHARGING_NUMBER_METADATA)
        return {}

    async def async_set_number_value(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        entity_key: str,
        value: float,
    ) -> bool:
        """Apply adapter-managed grid-charge and maximum-discharge permissions."""
        if entity_key not in {
            SOLAX_GRID_CHARGE_MAXIMUM_POWER_KEY,
            SOLAX_MAXIMUM_DISCHARGING_POWER_KEY,
        }:
            return False
        if (
            not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise HomeAssistantError(
                f"SolaX value for {entity_key} must be a finite number greater than or equal to zero"
            )

        async with self._grid_charge_lock:
            if entity_key == SOLAX_GRID_CHARGE_MAXIMUM_POWER_KEY:
                if not self._grid_charge_override_supported():
                    raise HomeAssistantError(
                        "SolaX grid-charge blocking is not supported by every detected battery inverter"
                    )
                if value == 0:
                    if self._grid_charge_override is None:
                        devices = self._grid_charge_devices(hass, registry)
                        self._grid_charge_override = {
                            "active": True,
                            "devices": devices,
                        }
                    self._grid_charge_value = 0
                    await self._save_grid_charge_state()
                    await self._apply_grid_charge_override(
                        hass,
                        registry,
                        self._grid_charge_override,
                    )
                else:
                    if self._grid_charge_override is not None:
                        await self._restore_grid_charge_override(
                            hass,
                            registry,
                            self._grid_charge_override,
                        )
                        self._grid_charge_override = None
                    self._grid_charge_value = float(value)
                    await self._save_grid_charge_state()
                return True

            if not self._maximum_discharging_override_supported():
                raise HomeAssistantError(
                    "SolaX no-discharge control is not supported by every detected battery inverter"
                )
            if value == 0:
                if self._maximum_discharging_override is None:
                    devices = self._maximum_discharging_devices(hass, registry)
                    self._maximum_discharging_override = {
                        "active": True,
                        "devices": devices,
                    }
                self._maximum_discharging_value = 0
                await self._save_maximum_discharging_state()
                await self._apply_maximum_discharging_override(
                    hass,
                    registry,
                    self._maximum_discharging_override,
                )
            else:
                if self._maximum_discharging_override is not None:
                    await self._restore_maximum_discharging_override(
                        hass,
                        registry,
                        self._maximum_discharging_override,
                    )
                    self._maximum_discharging_override = None
                self._maximum_discharging_value = float(value)
                await self._save_maximum_discharging_state()

        return True

    def _grid_charge_override_supported(self) -> bool:
        """Require a recognized generation and all entities needed to block grid charging."""
        if self._hass is None or self._registry is None:
            return False
        try:
            self._grid_charge_devices(self._hass, self._registry)
        except HomeAssistantError:
            return False
        return True

    def _grid_charge_devices(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
    ) -> list[dict[str, Any]]:
        """Validate and describe every battery inverter participating in the override."""
        if registry.get_entity_status(
            self.system_id,
            "battery_working_mode",
        ) != "found":
            raise HomeAssistantError(
                "SolaX working-mode entities are not fully resolved"
            )

        mode_sources = registry.get_entity_sources(
            self.system_id,
            "battery_working_mode",
        )
        if not mode_sources:
            raise HomeAssistantError(
                "No SolaX battery working-mode entities were found"
            )

        device_ids = [source.device_id for source in mode_sources]
        if any(device_id is None for device_id in device_ids):
            raise HomeAssistantError(
                "Every SolaX working-mode entity must belong to an inverter"
            )
        if len(set(device_ids)) != len(device_ids):
            raise HomeAssistantError(
                "SolaX working-mode sources contain duplicate inverter devices"
            )

        all_night_sources = self._night_charge_sources_by_device()
        enabled_night_sources = self._night_charge_sources_by_device(
            enabled_only=True
        )
        night_issues = self._night_charge_switch_issues()

        devices = []
        for source in mode_sources:
            state = hass.states.get(source.entity_id)
            if state is None or state.state in ("unknown", "unavailable"):
                raise HomeAssistantError(
                    f"SolaX working-mode entity {source.entity_id} is unavailable"
                )

            options = state.attributes.get("options", [])
            generation = (
                _detect_generation(options)
                if isinstance(options, (list, tuple))
                else None
            )
            if generation not in {"gen2", "gen3", "gen4_plus"}:
                raise HomeAssistantError(
                    f"Unsupported or unidentified SolaX generation for {source.entity_id}"
                )
            if "Self Use Mode" not in options:
                raise HomeAssistantError(
                    f"SolaX inverter {source.entity_id} does not expose Self Use Mode"
                )

            night_entity_id = None
            previous_night_state = None
            if generation == "gen4_plus":
                if not self._is_gen4_or_gen5_device(source, options):
                    device = (
                        dr.async_get(hass).async_get(source.device_id)
                        if source.device_id is not None
                        else None
                    )
                    model = getattr(device, "model", None)
                    raise HomeAssistantError(
                        "Grid-charge blocking is not yet verified for SolaX Gen6 "
                        f"or this Gen4/5 model: {model or source.entity_id}"
                    )
                if source.device_id in night_issues:
                    raise HomeAssistantError(
                        "SolaX Selfuse Night Charge is disabled or ambiguous. "
                        "Enable exactly one switch for every Gen4/5 battery inverter."
                    )
                night_source = all_night_sources.get(source.device_id)
                enabled_source = enabled_night_sources.get(source.device_id)
                if night_source is None or enabled_source is None:
                    raise HomeAssistantError(
                        "SolaX Selfuse Night Charge is not resolved for every Gen4/5 inverter"
                    )
                night_state = hass.states.get(enabled_source.entity_id)
                if night_state is None or night_state.state not in ("on", "off"):
                    raise HomeAssistantError(
                        f"SolaX night-charge switch {enabled_source.entity_id} is unavailable"
                    )
                night_entity_id = enabled_source.entity_id
                previous_night_state = night_state.state

            devices.append(
                {
                    "device_id": source.device_id,
                    "mode_entity_id": source.entity_id,
                    "previous_mode": state.state,
                    "night_charge_entity_id": night_entity_id,
                    "previous_night_charge_state": previous_night_state,
                }
            )

        return devices

    async def _save_grid_charge_state(self) -> None:
        """Persist the requested value and restoration snapshot across restarts."""
        if self._grid_charge_store is None:
            return
        await self._grid_charge_store.async_save(
            {
                "value": self._grid_charge_value,
                "override": self._grid_charge_override,
            }
        )

    @staticmethod
    async def _call_select_options(
        hass: HomeAssistant,
        options_by_entity: Mapping[str, str],
    ) -> None:
        """Apply grouped select options only when the source differs."""
        by_option: dict[str, list[str]] = defaultdict(list)
        for entity_id, option in options_by_entity.items():
            state = hass.states.get(entity_id)
            if state is None or state.state in ("unknown", "unavailable"):
                raise HomeAssistantError(
                    f"SolaX working-mode entity {entity_id} is unavailable"
                )
            if state.state != option:
                by_option[option].append(entity_id)

        for option, entity_ids in by_option.items():
            await hass.services.async_call(
                "select",
                "select_option",
                {
                    "entity_id": sorted(entity_ids),
                    "option": option,
                },
                blocking=True,
            )

    @staticmethod
    async def _call_switch_states(
        hass: HomeAssistant,
        states_by_entity: Mapping[str, str],
    ) -> None:
        """Apply grouped switch states only when the source differs."""
        for target_state in ("off", "on"):
            entity_ids = []
            for entity_id, state_value in states_by_entity.items():
                if state_value != target_state:
                    continue
                state = hass.states.get(entity_id)
                if state is None or state.state not in ("on", "off"):
                    raise HomeAssistantError(
                        f"SolaX night-charge switch {entity_id} is unavailable"
                    )
                if state.state != target_state:
                    entity_ids.append(entity_id)
            if entity_ids:
                await hass.services.async_call(
                    "switch",
                    "turn_on" if target_state == "on" else "turn_off",
                    {"entity_id": sorted(entity_ids)},
                    blocking=True,
                )

    async def _apply_grid_charge_override(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        override: dict[str, Any],
    ) -> None:
        """Temporarily use Self Use and disable Gen4/5 scheduled grid charging."""
        current_devices = self._grid_charge_devices(hass, registry)
        saved_devices = override.get("devices")
        if not isinstance(saved_devices, list):
            raise HomeAssistantError("Saved SolaX grid-charge state is invalid")

        saved_by_device = {
            item.get("device_id"): item
            for item in saved_devices
            if isinstance(item, dict) and item.get("device_id") is not None
        }
        current_by_device = {
            item["device_id"]: item for item in current_devices
        }
        if set(saved_by_device) != set(current_by_device):
            raise HomeAssistantError(
                "The SolaX battery-inverter set changed while grid charging was blocked"
            )

        # Disable the Gen4/5 night-charge switch before changing working mode.
        switch_targets = {
            current_by_device[device_id]["night_charge_entity_id"]: "off"
            for device_id in saved_by_device
            if current_by_device[device_id]["night_charge_entity_id"] is not None
        }
        if switch_targets:
            await self._call_switch_states(hass, switch_targets)

        mode_targets = {
            current_by_device[device_id]["mode_entity_id"]: "Self Use Mode"
            for device_id in saved_by_device
        }
        await self._call_select_options(hass, mode_targets)

    async def _restore_grid_charge_override(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        override: dict[str, Any],
    ) -> None:
        """Restore each prior mode and night-charge state without overwriting later manual changes."""
        current_devices = self._grid_charge_devices(hass, registry)
        saved_devices = override.get("devices")
        if not isinstance(saved_devices, list):
            raise HomeAssistantError("Saved SolaX grid-charge state is invalid")

        saved_by_device = {
            item.get("device_id"): item
            for item in saved_devices
            if isinstance(item, dict) and item.get("device_id") is not None
        }
        current_by_device = {
            item["device_id"]: item for item in current_devices
        }
        if set(saved_by_device) != set(current_by_device):
            raise HomeAssistantError(
                "The SolaX battery-inverter set changed while grid charging was blocked"
            )

        mode_targets: dict[str, str] = {}
        switch_targets: dict[str, str] = {}
        for device_id, saved in saved_by_device.items():
            current = current_by_device[device_id]
            mode_state = hass.states.get(current["mode_entity_id"])
            if mode_state is None or mode_state.state in ("unknown", "unavailable"):
                raise HomeAssistantError(
                    f"SolaX working-mode entity {current['mode_entity_id']} is unavailable"
                )

            # A different mode means the user or another automation changed it
            # during the override. Do not overwrite that later decision.
            if mode_state.state == "Self Use Mode":
                previous_mode = saved.get("previous_mode")
                if isinstance(previous_mode, str):
                    mode_targets[current["mode_entity_id"]] = previous_mode

            switch_entity_id = current["night_charge_entity_id"]
            previous_switch_state = saved.get("previous_night_charge_state")
            if switch_entity_id is not None and previous_switch_state in ("on", "off"):
                switch_state = hass.states.get(switch_entity_id)
                if switch_state is None or switch_state.state not in ("on", "off"):
                    raise HomeAssistantError(
                        f"SolaX night-charge switch {switch_entity_id} is unavailable"
                    )
                # Only restore a switch that is still in the state applied by
                # this override; a later manual change is left untouched.
                if switch_state.state == "off":
                    switch_targets[switch_entity_id] = previous_switch_state

        # Restore the working mode before re-enabling any scheduled charge.
        await self._call_select_options(hass, mode_targets)
        if switch_targets:
            await self._call_switch_states(hass, switch_targets)

    def _maximum_discharging_override_supported(self) -> bool:
        """Require the generation-specific no-discharge controls on every battery inverter."""
        if self._hass is None or self._registry is None:
            return False
        try:
            self._maximum_discharging_devices(self._hass, self._registry)
        except HomeAssistantError:
            return False
        return True

    @staticmethod
    def _find_inverter_control_entity(
        hass: HomeAssistant,
        *,
        device_id: str,
        domain: str,
        source_key: str,
    ) -> tuple[str, State | None]:
        """Find exactly one enabled SolaX control entity on an inverter device."""
        entity_registry = er.async_get(hass)
        matches = [
            entry
            for entry in entity_registry.entities.values()
            if entry.platform == SOLAX_DOMAIN
            and entry.device_id == device_id
            and entry.entity_id.startswith(f"{domain}.")
            and _entity_key_matches(entry.unique_id, source_key)
            and _disabled_by(entry) is None
        ]
        if len(matches) != 1:
            raise HomeAssistantError(
                f"SolaX control entity {source_key} must be enabled exactly once "
                f"on inverter device {device_id}"
            )

        entity_id = matches[0].entity_id
        state = hass.states.get(entity_id)
        if domain != "button" and (
            state is None or state.state in ("unknown", "unavailable")
        ):
            raise HomeAssistantError(
                f"SolaX control entity {entity_id} is unavailable"
            )
        # Button entities may not have a state until the first press.
        return entity_id, state

    def _maximum_discharging_devices(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
    ) -> list[dict[str, Any]]:
        """Validate and describe the no-discharge controls for every battery inverter."""
        if registry.get_entity_status(
            self.system_id,
            "battery_working_mode",
        ) != "found":
            raise HomeAssistantError(
                "SolaX working-mode entities are not fully resolved"
            )

        mode_sources = registry.get_entity_sources(
            self.system_id,
            "battery_working_mode",
        )
        if not mode_sources:
            raise HomeAssistantError(
                "No SolaX battery working-mode entities were found"
            )

        device_ids = [source.device_id for source in mode_sources]
        if any(device_id is None for device_id in device_ids):
            raise HomeAssistantError(
                "Every SolaX working-mode entity must belong to an inverter"
            )
        if len(set(device_ids)) != len(device_ids):
            raise HomeAssistantError(
                "SolaX working-mode sources contain duplicate inverter devices"
            )

        devices = []
        for source in mode_sources:
            state = hass.states.get(source.entity_id)
            if state is None or state.state in ("unknown", "unavailable"):
                raise HomeAssistantError(
                    f"SolaX working-mode entity {source.entity_id} is unavailable"
                )

            options = state.attributes.get("options", [])
            generation = (
                _detect_generation(options)
                if isinstance(options, (list, tuple))
                else None
            )
            if generation not in {"gen2", "gen3", "gen4_plus"}:
                raise HomeAssistantError(
                    f"Unsupported or unidentified SolaX generation for {source.entity_id}"
                )

            device = {
                "device_id": source.device_id,
                "generation": generation,
                "working_mode_entity_id": source.entity_id,
                "previous_working_mode": state.state,
            }

            if generation == "gen2":
                if "Back Up Mode" not in options:
                    raise HomeAssistantError(
                        f"SolaX Gen2 inverter {source.entity_id} does not expose Back Up Mode"
                    )
                setpoint_entity_id, setpoint_state = (
                    self._find_inverter_control_entity(
                        hass,
                        device_id=source.device_id,
                        domain="number",
                        source_key=SOLAX_GEN2_REMOTE_POWER_SETPOINT_SOURCE_KEY,
                    )
                )
                previous_setpoint = parse_numeric(setpoint_state.state)
                if previous_setpoint is None:
                    raise HomeAssistantError(
                        f"SolaX Gen2 remote power setpoint {setpoint_entity_id} is not numeric"
                    )
                minimum = parse_numeric(setpoint_state.attributes.get("min"))
                maximum = parse_numeric(setpoint_state.attributes.get("max"))
                if (
                    (minimum is not None and minimum > 0)
                    or (maximum is not None and maximum < 0)
                ):
                    raise HomeAssistantError(
                        f"SolaX Gen2 remote power setpoint {setpoint_entity_id} cannot accept 0 W"
                    )
                device.update(
                    {
                        "strategy": "gen2_remote_control",
                        "remote_power_setpoint_entity_id": setpoint_entity_id,
                        "previous_remote_power_setpoint": previous_setpoint,
                    }
                )
            elif generation == "gen3":
                if "Feedin Priority" not in options:
                    raise HomeAssistantError(
                        f"SolaX Gen3 inverter {source.entity_id} does not expose Feedin Priority"
                    )
                device["strategy"] = "gen3_feed_in_priority"
            else:
                control_entity_id, control_state = (
                    self._find_inverter_control_entity(
                        hass,
                        device_id=source.device_id,
                        domain="select",
                        source_key=SOLAX_GEN4_REMOTE_CONTROL_MODE_SOURCE_KEY,
                    )
                )
                control_options = control_state.attributes.get("options", [])
                if not isinstance(control_options, (list, tuple)) or not {
                    SOLAX_GEN4_DISABLED_MODE,
                    SOLAX_GEN4_NO_DISCHARGE_MODE,
                }.issubset(set(control_options)):
                    raise HomeAssistantError(
                        f"SolaX Mode 8 no-discharge option is unavailable on {control_entity_id}"
                    )

                trigger_entity_id, _ = self._find_inverter_control_entity(
                    hass,
                    device_id=source.device_id,
                    domain="button",
                    source_key=SOLAX_GEN4_REMOTE_CONTROL_TRIGGER_SOURCE_KEY,
                )
                duration_entity_id, duration_state = (
                    self._find_inverter_control_entity(
                        hass,
                        device_id=source.device_id,
                        domain="number",
                        source_key=SOLAX_GEN4_AUTOREPEAT_DURATION_SOURCE_KEY,
                    )
                )
                previous_duration = parse_numeric(duration_state.state)
                if previous_duration is None:
                    raise HomeAssistantError(
                        f"SolaX autorepeat duration {duration_entity_id} is not numeric"
                    )
                maximum_duration = parse_numeric(
                    duration_state.attributes.get("max")
                )
                if (
                    maximum_duration is not None
                    and maximum_duration
                    < SOLAX_GEN4_AUTOREPEAT_DURATION_SECONDS
                ):
                    raise HomeAssistantError(
                        f"SolaX autorepeat duration {duration_entity_id} cannot be set for 48 hours"
                    )
                device.update(
                    {
                        "strategy": "gen4_mode8",
                        "remote_control_mode_entity_id": control_entity_id,
                        "previous_remote_control_mode": control_state.state,
                        "remote_control_trigger_entity_id": trigger_entity_id,
                        "autorepeat_duration_entity_id": duration_entity_id,
                        "previous_autorepeat_duration": previous_duration,
                    }
                )

            devices.append(device)

        return devices

    async def _save_maximum_discharging_state(self) -> None:
        """Persist the requested value and restoration snapshot across restarts."""
        if self._maximum_discharging_store is None:
            return
        await self._maximum_discharging_store.async_save(
            {
                "value": self._maximum_discharging_value,
                "override": self._maximum_discharging_override,
            }
        )

    @staticmethod
    async def _call_number_values(
        hass: HomeAssistant,
        values_by_entity: Mapping[str, float],
    ) -> None:
        """Set grouped number values, skipping entities already at the target."""
        by_value: dict[float, list[str]] = defaultdict(list)
        for entity_id, target_value in values_by_entity.items():
            state = hass.states.get(entity_id)
            current_value = (
                parse_numeric(state.state)
                if state is not None
                and state.state not in ("unknown", "unavailable")
                else None
            )
            if current_value is None:
                raise HomeAssistantError(
                    f"SolaX number entity {entity_id} is unavailable or non-numeric"
                )
            if not math.isclose(current_value, target_value, abs_tol=0.1):
                by_value[target_value].append(entity_id)

        for target_value, entity_ids in by_value.items():
            await hass.services.async_call(
                "number",
                "set_value",
                {
                    "entity_id": sorted(entity_ids),
                    "value": target_value,
                },
                blocking=True,
            )

    @staticmethod
    async def _call_button_presses(
        hass: HomeAssistant,
        entity_ids: Sequence[str],
    ) -> None:
        """Press one or more SolaX control buttons."""
        if entity_ids:
            await hass.services.async_call(
                "button",
                "press",
                {"entity_id": sorted(set(entity_ids))},
                blocking=True,
            )

    async def _apply_maximum_discharging_override(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        override: dict[str, Any],
    ) -> None:
        """Apply each generation's native no-discharge control."""
        current_devices = self._maximum_discharging_devices(hass, registry)
        saved_devices = override.get("devices")
        if not isinstance(saved_devices, list):
            raise HomeAssistantError(
                "Saved SolaX no-discharge state is invalid"
            )

        saved_by_device = {
            item.get("device_id"): item
            for item in saved_devices
            if isinstance(item, dict) and item.get("device_id") is not None
        }
        current_by_device = {
            item["device_id"]: item for item in current_devices
        }
        if set(saved_by_device) != set(current_by_device):
            raise HomeAssistantError(
                "The SolaX battery-inverter set changed while discharge was blocked"
            )

        number_targets: dict[str, float] = {}
        working_mode_targets: dict[str, str] = {}
        remote_control_mode_targets: dict[str, str] = {}
        trigger_entity_ids = []

        for device_id, current in current_by_device.items():
            saved = saved_by_device[device_id]
            strategy = saved.get("strategy")
            if strategy == "gen2_remote_control":
                number_targets[current["remote_power_setpoint_entity_id"]] = 0
                working_mode_targets[
                    current["working_mode_entity_id"]
                ] = "Back Up Mode"
            elif strategy == "gen3_feed_in_priority":
                working_mode_targets[
                    current["working_mode_entity_id"]
                ] = "Feedin Priority"
            elif strategy == "gen4_mode8":
                number_targets[
                    current["autorepeat_duration_entity_id"]
                ] = SOLAX_GEN4_AUTOREPEAT_DURATION_SECONDS
                remote_control_mode_targets[
                    current["remote_control_mode_entity_id"]
                ] = SOLAX_GEN4_NO_DISCHARGE_MODE
                trigger_entity_ids.append(
                    current["remote_control_trigger_entity_id"]
                )
            else:
                raise HomeAssistantError(
                    f"Unknown saved SolaX no-discharge strategy: {strategy!r}"
                )

        # Gen2 needs a neutral remote-power setpoint before Back Up Mode is selected.
        # Gen4-6 need an active autorepeat window before the Mode 8 trigger is pressed.
        await self._call_number_values(hass, number_targets)
        await self._call_select_options(hass, working_mode_targets)
        await self._call_select_options(hass, remote_control_mode_targets)
        await self._call_button_presses(hass, trigger_entity_ids)

    async def _restore_maximum_discharging_override(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        override: dict[str, Any],
    ) -> None:
        """Restore previous controls only when they are still in the state applied by SEM."""
        current_devices = self._maximum_discharging_devices(hass, registry)
        saved_devices = override.get("devices")
        if not isinstance(saved_devices, list):
            raise HomeAssistantError(
                "Saved SolaX no-discharge state is invalid"
            )

        saved_by_device = {
            item.get("device_id"): item
            for item in saved_devices
            if isinstance(item, dict) and item.get("device_id") is not None
        }
        current_by_device = {
            item["device_id"]: item for item in current_devices
        }
        if set(saved_by_device) != set(current_by_device):
            raise HomeAssistantError(
                "The SolaX battery-inverter set changed while discharge was blocked"
            )

        working_mode_targets: dict[str, str] = {}
        remote_control_mode_targets: dict[str, str] = {}
        setpoint_targets: dict[str, float] = {}
        autorepeat_targets: dict[str, float] = {}
        trigger_entity_ids = []

        for device_id, current in current_by_device.items():
            saved = saved_by_device[device_id]
            strategy = saved.get("strategy")

            if strategy == "gen2_remote_control":
                mode_state = hass.states.get(current["working_mode_entity_id"])
                if mode_state is None or mode_state.state in ("unknown", "unavailable"):
                    raise HomeAssistantError(
                        f"SolaX working-mode entity {current['working_mode_entity_id']} is unavailable"
                    )
                if mode_state.state == "Back Up Mode":
                    previous_mode = saved.get("previous_working_mode")
                    if isinstance(previous_mode, str):
                        working_mode_targets[
                            current["working_mode_entity_id"]
                        ] = previous_mode
                    setpoint_state = hass.states.get(
                        current["remote_power_setpoint_entity_id"]
                    )
                    current_setpoint = (
                        parse_numeric(setpoint_state.state)
                        if setpoint_state is not None
                        else None
                    )
                    if current_setpoint is not None and math.isclose(
                        current_setpoint,
                        0,
                        abs_tol=0.1,
                    ):
                        previous_setpoint = parse_numeric(
                            saved.get("previous_remote_power_setpoint")
                        )
                        if previous_setpoint is not None:
                            setpoint_targets[
                                current["remote_power_setpoint_entity_id"]
                            ] = previous_setpoint

            elif strategy == "gen3_feed_in_priority":
                mode_state = hass.states.get(current["working_mode_entity_id"])
                if mode_state is None or mode_state.state in ("unknown", "unavailable"):
                    raise HomeAssistantError(
                        f"SolaX working-mode entity {current['working_mode_entity_id']} is unavailable"
                    )
                if mode_state.state == "Feedin Priority":
                    previous_mode = saved.get("previous_working_mode")
                    if isinstance(previous_mode, str):
                        working_mode_targets[
                            current["working_mode_entity_id"]
                        ] = previous_mode

            elif strategy == "gen4_mode8":
                control_state = hass.states.get(
                    current["remote_control_mode_entity_id"]
                )
                if (
                    control_state is None
                    or control_state.state in ("unknown", "unavailable")
                ):
                    raise HomeAssistantError(
                        f"SolaX remote-control mode {current['remote_control_mode_entity_id']} is unavailable"
                    )
                if control_state.state == SOLAX_GEN4_NO_DISCHARGE_MODE:
                    previous_control_mode = saved.get(
                        "previous_remote_control_mode"
                    )
                    if isinstance(previous_control_mode, str):
                        remote_control_mode_targets[
                            current["remote_control_mode_entity_id"]
                        ] = previous_control_mode
                        trigger_entity_ids.append(
                            current["remote_control_trigger_entity_id"]
                        )

                duration_state = hass.states.get(
                    current["autorepeat_duration_entity_id"]
                )
                current_duration = (
                    parse_numeric(duration_state.state)
                    if duration_state is not None
                    else None
                )
                if (
                    current_duration is not None
                    and math.isclose(
                        current_duration,
                        SOLAX_GEN4_AUTOREPEAT_DURATION_SECONDS,
                        abs_tol=0.1,
                    )
                ):
                    previous_duration = parse_numeric(
                        saved.get("previous_autorepeat_duration")
                    )
                    if previous_duration is not None:
                        autorepeat_targets[
                            current["autorepeat_duration_entity_id"]
                        ] = previous_duration
            else:
                raise HomeAssistantError(
                    f"Unknown saved SolaX no-discharge strategy: {strategy!r}"
                )

        await self._call_select_options(hass, working_mode_targets)
        await self._call_select_options(hass, remote_control_mode_targets)
        await self._call_button_presses(hass, trigger_entity_ids)
        # Restore the Gen2 setpoint after leaving Back Up Mode and the Gen4-6
        # repeat duration after the prior remote-control mode has been restored.
        await self._call_number_values(hass, setpoint_targets)
        await self._call_number_values(hass, autorepeat_targets)

    def get_related_entity_ids(self, entity_key: str) -> list[str]:
        """Return auxiliary entities that affect a SEM entity's state."""
        if self._registry is None:
            return []

        related_ids = []
        if entity_key == "battery_working_mode":
            related_ids.extend(
                self._registry.get_entity_ids(
                    self.system_id,
                    SOLAX_NIGHT_CHARGE_ENTITY_KEY,
                )
            )

        if entity_key in {
            "battery_state_of_capacity",
            "battery_rated_capacity",
        }:
            # Capacity sources remain relevant when only some devices resolve.
            related_ids.extend(
                source.entity_id
                for source in self._registry.get_entity_sources(
                    self.system_id,
                    "battery_rated_capacity",
                )
            )
            related_ids.append(SOLAX_TOTAL_CAPACITY_HELPER)

        return sorted(set(related_ids))

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
        if entity_key == "battery_rated_capacity":
            return self._get_total_capacity_kwh()

        if not source_states or any(
            state is None or state.state in ("unknown", "unavailable")
            for state in source_states
        ):
            return None

        if entity_key == "battery_state_of_capacity":
            if (
                self._registry is None
                or self._registry.get_entity_status(
                    self.system_id,
                    "battery_state_of_capacity",
                ) != "found"
            ):
                return None

            soc_sources = self._get_sources(entity_key)
            if (
                len(soc_sources) != len(source_states)
                or len(soc_sources) != len(self._battery_device_ids)
            ):
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

            if set(soc_by_device) != set(self._battery_device_ids):
                return None
            if len(soc_by_device) == 1:
                return next(iter(soc_by_device.values()))

            capacity_by_device = self._get_capacity_by_device()
            return capacity_weighted_soc(
                soc_by_device,
                capacity_by_device,
            )

        if entity_key == "power_meter_active_power":
            # SolaX measured_power is positive for export and negative for import.
            # Preserve this sign convention. Multiple inverter entries can mirror
            # the same plant meter, so compare them rather than summing them.
            return plant_meter_value(
                [state.state for state in source_states if state is not None]
            )

        if entity_key == "battery_charge_discharge_power":
            # SolaX battery_power_charge is positive while charging and
            # negative while discharging. Preserve the sign and sum the
            # readings from every battery inverter, as the Huawei adapter does.
            return sum_numeric_values(
                state.state for state in source_states if state is not None
            )

        if entity_key in {
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
        warnings = []

        expected_device_ids = set(self._battery_device_ids)
        physical_capacity_by_device = self._get_physical_capacity_by_device()
        missing_capacity_ids = (
            expected_device_ids - set(physical_capacity_by_device)
        )

        if missing_capacity_ids:
            configured_total = self._get_configured_total_capacity_kwh()
            completed_capacity_map = self._get_capacity_by_device()
            fallback_can_weight = (
                set(completed_capacity_map) == expected_device_ids
                and bool(expected_device_ids)
            )

            if configured_total is not None and fallback_can_weight:
                message = (
                    "SEM använder "
                    f"{SOLAX_TOTAL_CAPACITY_HELPER} som reservvärde för att "
                    "komplettera installerad batterikapacitet. Aktivera ändå "
                    "de fysiska kapacitetssensorerna när de finns tillgängliga."
                )
            elif configured_total is not None:
                message = (
                    "SEM har ett manuellt totalvärde för batterikapaciteten, "
                    "men fysisk kapacitet saknas för flera SolaX-växelriktare. "
                    "Totalvärdet kan visas, men det går inte att fördela säkert "
                    "mellan flera batterier. Kapacitetsviktad SOC är därför "
                    "inte tillgänglig."
                )
            elif len(expected_device_ids) > 1:
                message = (
                    "SEM saknar installerad kapacitet för en eller flera "
                    "SolaX-växelriktare. Kapacitetsviktad SOC kan inte beräknas "
                    "utan individuella kapaciteter eller ett användbart "
                    f"reservvärde i {SOLAX_TOTAL_CAPACITY_HELPER}."
                )
            else:
                message = (
                    "SEM kan läsa SOC direkt från det enda batteriet, men "
                    "saknar en källa för total installerad batterikapacitet. "
                    f"Ange anläggningens totala kapacitet i "
                    f"{SOLAX_TOTAL_CAPACITY_HELPER}."
                )

            warnings.append(
                {
                    "code": "solax_battery_capacity_fallback",
                    "setting": "Installerad batterikapacitet",
                    "message": message,
                    "action": (
                        "Aktivera 'Battery Install Capacity' på Gen3. På "
                        "Gen4-Gen6 aktiverar du 'Battery System Installed "
                        "Capacity'. Gen2 saknar motsvarande standardentitet; "
                        f"ange då totalen i {SOLAX_TOTAL_CAPACITY_HELPER}. "
                        "Ladda därefter om SEM-integrationen."
                    ),
                    "sources": [
                        {
                            "entity_id": source.entity_id,
                            "device_id": source.device_id,
                        }
                        for source in registry.get_entity_sources(
                            self.system_id,
                            "battery_rated_capacity",
                        )
                    ],
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
        if warnings:
            diagnostics["warnings"] = warnings
        if working_mode_generations:
            diagnostics["working_mode_generations"] = working_mode_generations
        return diagnostics

    def _get_sources(self, entity_key: str) -> list[EntitySource]:
        """Return resolved sources for one SEM key."""
        if self._hass is None or self._registry is None:
            return []
        # State aggregation uses the same ordering as get_entity_ids().
        return self._registry.get_entity_sources(self.system_id, entity_key)

    def _get_configured_total_capacity_kwh(self) -> float | None:
        """Read the user-configured total installed battery capacity."""
        if self._hass is None:
            return None
        state = self._hass.states.get(SOLAX_TOTAL_CAPACITY_HELPER)
        if state is None:
            return None
        value = parse_numeric(state.state)
        return value if value is not None and value > 0 else None

    def _get_physical_capacity_by_device(self) -> dict[str, float]:
        """Read all currently available physical capacity values in kWh."""
        if self._hass is None or self._registry is None:
            return {}
        if (
            self._registry.get_entity_status(
                self.system_id,
                "battery_rated_capacity",
            ) == "ambiguous"
        ):
            return {}

        capacities = {}
        for source in self._get_sources("battery_rated_capacity"):
            if source.device_id is None or source.device_id in capacities:
                return {}

            state = self._hass.states.get(source.entity_id)
            if state is None:
                continue

            source_key = next(
                (
                    key
                    for key in SOLAX_CAPACITY_SOURCE_KEYS
                    if source.unique_id
                    and _entity_key_matches(source.unique_id, key)
                ),
                None,
            )
            capacity = normalize_capacity_kwh(
                state.state,
                state.attributes.get("unit_of_measurement"),
                source_key,
            )
            if capacity is not None:
                capacities[source.device_id] = capacity

        return capacities

    def _get_capacity_by_device(self) -> dict[str, float]:
        """Return complete per-inverter capacities, using one safe residual."""
        physical_capacities = self._get_physical_capacity_by_device()
        completed = complete_capacity_map(
            physical_capacities,
            self._battery_device_ids,
            self._get_configured_total_capacity_kwh(),
        )
        return completed or {}

    def _get_total_capacity_kwh(self) -> float | None:
        """Return the complete physical total or the configured plant total."""
        return total_capacity_kwh(
            self._get_physical_capacity_by_device(),
            self._battery_device_ids,
            self._get_configured_total_capacity_kwh(),
        )

    async def async_execute(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        command: str,
        data: dict[str, Any],
    ) -> None:
        """Execute supported SolaX commands with generation-specific guards."""
        if command == "set_grid_export_power_limit":
            power = parse_numeric(data.get("power"))
            if power is None or power < 0:
                raise HomeAssistantError(
                    "SolaX grid export power limit must be a finite number "
                    "greater than or equal to zero"
                )

            export_sources = registry.get_entity_sources(
                self.system_id,
                SOLAX_GRID_EXPORT_POWER_LIMIT_KEY,
            )
            device_ids = sorted({
                source.device_id
                for source in export_sources
                if source.device_id is not None
            })
            if (
                registry.get_entity_status(
                    self.system_id,
                    SOLAX_GRID_EXPORT_POWER_LIMIT_KEY,
                ) != "found"
                or not export_sources
                or any(source.device_id is None for source in export_sources)
                or len(device_ids) != len(export_sources)
            ):
                raise HomeAssistantError(
                    "SolaX export-control limit entities are not fully resolved"
                )

            entity_ids = self._require_sources_by_device(
                hass,
                registry,
                entity_key=SOLAX_GRID_EXPORT_POWER_LIMIT_KEY,
                domain="number",
                device_ids=device_ids,
            )
            targets = []
            for entity_id in entity_ids:
                state = hass.states.get(entity_id)
                current_value = (
                    parse_numeric(state.state) if state is not None else None
                )
                if current_value is None or not math.isclose(
                    current_value,
                    power,
                    rel_tol=0.0,
                    abs_tol=1e-6,
                ):
                    targets.append(entity_id)

            if targets:
                if not hass.services.has_service("number", "set_value"):
                    raise HomeAssistantError(
                        "Home Assistant number.set_value service is not available"
                    )
                await hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": targets, "value": power},
                    blocking=True,
                )
            return

        if command == "reset_grid_export_power_limit":
            export_sources = registry.get_entity_sources(
                self.system_id,
                SOLAX_GRID_EXPORT_POWER_LIMIT_KEY,
            )
            device_ids = sorted({
                source.device_id
                for source in export_sources
                if source.device_id is not None
            })
            if (
                registry.get_entity_status(
                    self.system_id,
                    SOLAX_GRID_EXPORT_POWER_LIMIT_KEY,
                ) != "found"
                or not export_sources
                or any(source.device_id is None for source in export_sources)
                or len(device_ids) != len(export_sources)
            ):
                raise HomeAssistantError(
                    "SolaX export-control limit entities are not fully resolved"
                )

            entity_ids = self._require_sources_by_device(
                hass,
                registry,
                entity_key=SOLAX_GRID_EXPORT_POWER_LIMIT_KEY,
                domain="number",
                device_ids=device_ids,
            )

            # Restore each inverter to its own allowed maximum. The number
            # entity exposes the effective model/configuration-specific limit
            # through its Home Assistant "max" state attribute.
            targets_by_limit: dict[float, list[str]] = defaultdict(list)
            for entity_id in entity_ids:
                state = hass.states.get(entity_id)
                maximum = (
                    parse_numeric(state.attributes.get("max"))
                    if state is not None
                    else None
                )
                if maximum is None or maximum < 0:
                    raise HomeAssistantError(
                        f"SolaX export-control entity {entity_id} has no valid "
                        "maximum value in its 'max' attribute"
                    )

                current_value = (
                    parse_numeric(state.state) if state is not None else None
                )
                if current_value is None or not math.isclose(
                    current_value,
                    maximum,
                    rel_tol=0.0,
                    abs_tol=1e-6,
                ):
                    targets_by_limit[maximum].append(entity_id)

            if targets_by_limit:
                if not hass.services.has_service("number", "set_value"):
                    raise HomeAssistantError(
                        "Home Assistant number.set_value service is not available"
                    )

                results = await asyncio.gather(
                    *(
                        hass.services.async_call(
                            "number",
                            "set_value",
                            {"entity_id": targets, "value": maximum},
                            blocking=True,
                        )
                        for maximum, targets in sorted(targets_by_limit.items())
                    ),
                    return_exceptions=True,
                )
                failures = [
                    str(result)
                    for result in results
                    if isinstance(result, Exception)
                ]
                if failures:
                    raise HomeAssistantError(
                        "SolaX grid export power limit reset failed: "
                        + "; ".join(failures)
                    )
            return

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
