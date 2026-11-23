"""Adapter for Solis hybrid inverters using the SolaX Modbus integration."""

from __future__ import annotations

import asyncio
import logging
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .aggregation import plant_meter_value


from .adapter import SemSystemAdapter
from .capabilities import EntityCapability, SystemCapabilities
from .registry import EntitySource, SemEntityRegistry
from .solis_helpers import (
    SOLIS_BATTERY_SOC_ENTITY_KEY,
    SOLIS_BATTERY_SOC_SOURCE_KEY,
    SOLIS_INVERTER_INPUT_POWER_ENTITY_KEY,
    SOLIS_INVERTER_INPUT_POWER_SOURCE_KEY,
    SOLIS_BATTERY_RATED_CAPACITY_ENTITY_KEY,
    SOLIS_TOTAL_CAPACITY_HELPER,
    SOLIS_END_OF_DISCHARGE_SOC_ENTITY_KEY,
    SOLIS_GRID_CHARGE_MAXIMUM_POWER_ENTITY_KEY,
    SOLIS_GRID_CHARGE_NO_GRID_SOURCE_OPTION,
    SOLIS_END_OF_DISCHARGE_SOC_SOURCE_KEY,
    SOLIS_MODE_TO_SOURCE_OPTION,
    SOLIS_SOURCE_OPTION_TO_MODE,
    SOLIS_TOU_BUTTON_ENTITY_KEYS,
    SOLIS_TOU_NUMBER_ENTITY_KEYS,
    build_solis_time_values,
    build_solis_discharge_current_values,
    parse_solis_tou_periods,
    solis_grid_charge_blocked_option,
    solis_battery_soc_value,
    solis_total_capacity_kwh,
    sum_solis_pv_power,
)

_LOGGER = logging.getLogger(__name__)

SOLIS_DOMAIN = "solax_modbus"
SOLIS_SYSTEM_ID = "solis"
SOLIS_MANUFACTURER = "Ginlog Solis"
SOLIS_SUPPORTED_PLUGINS = frozenset({"solis"})
SOLIS_GRID_CHARGE_MAXIMUM_POWER_KEY = SOLIS_GRID_CHARGE_MAXIMUM_POWER_ENTITY_KEY
SOLIS_GRID_CHARGE_STORE_VERSION = 1
SOLIS_GRID_CHARGE_NUMBER_METADATA = {
    "min_value": 0,
    "max_value": 100_000,
    "step": 1,
    "unit_of_measurement": "W",
    "mode": "box",
}
SOLIS_MAXIMUM_DISCHARGING_POWER_KEY = "maximum_discharging_power"
SOLIS_MAXIMUM_DISCHARGING_STORE_VERSION = 1
SOLIS_MAXIMUM_DISCHARGING_NUMBER_METADATA = {
    "min_value": 0,
    "max_value": 100_000,
    "step": 1,
    "unit_of_measurement": "W",
    "mode": "box",
}
SOLIS_MAXIMUM_DISCHARGING_CURRENT_ENTITY_KEY = "maximum_discharging_current"
SOLIS_BATTERY_VOLTAGE_ENTITY_KEY = "battery_voltage"

SOLIS_ENTITY_KEYS = (
    "battery_working_mode",
    SOLIS_BATTERY_SOC_ENTITY_KEY,
    SOLIS_INVERTER_INPUT_POWER_ENTITY_KEY,
    SOLIS_POWER_METER_ENTITY_KEY,
    SOLIS_END_OF_DISCHARGE_SOC_ENTITY_KEY,
    SOLIS_MAXIMUM_DISCHARGING_CURRENT_ENTITY_KEY,
    SOLIS_BATTERY_VOLTAGE_ENTITY_KEY,
    *SOLIS_TOU_NUMBER_ENTITY_KEYS,
    *SOLIS_TOU_BUTTON_ENTITY_KEYS,
)

SOLIS_CAPABILITIES = SystemCapabilities(
    entities={
        "battery_working_mode": EntityCapability(
            required=True,
            readable=True,
            writable=True,
        ),
        SOLIS_BATTERY_SOC_ENTITY_KEY: EntityCapability(
            required=True,
            readable=True,
            writable=False,
        ),
        SOLIS_INVERTER_INPUT_POWER_ENTITY_KEY: EntityCapability(
            required=True,
            readable=True,
            writable=False,
        ),
        SOLIS_POWER_METER_ENTITY_KEY: EntityCapability(
            required=True,
            readable=True,
            writable=False,
        ),
        SOLIS_BATTERY_RATED_CAPACITY_ENTITY_KEY: EntityCapability(
            required=False,
            readable=True,
            writable=False,
        ),
        SOLIS_END_OF_DISCHARGE_SOC_ENTITY_KEY: EntityCapability(
            required=False,
            readable=True,
            writable=True,
        ),
        SOLIS_GRID_CHARGE_MAXIMUM_POWER_KEY: EntityCapability(
            required=False,
            readable=True,
            writable=True,
        ),
        SOLIS_MAXIMUM_DISCHARGING_POWER_KEY: EntityCapability(
            required=False,
            readable=True,
            writable=True,
        ),
        SOLIS_MAXIMUM_DISCHARGING_CURRENT_ENTITY_KEY: EntityCapability(
            required=False,
            readable=True,
            writable=True,
        ),
        SOLIS_BATTERY_VOLTAGE_ENTITY_KEY: EntityCapability(
            required=False,
            readable=True,
            writable=False,
        ),
        **{
            key: EntityCapability(
                required=False,
                readable=False,
                writable=False,
            )
            for key in (
                *SOLIS_TOU_NUMBER_ENTITY_KEYS,
                *SOLIS_TOU_BUTTON_ENTITY_KEYS,
            )
        },
    },
    commands=frozenset({"set_battery_time_of_use_periods"}),
)


def _entry_plugin(entry: Any) -> str:
    """Return the configured SolaX Modbus plugin name."""
    options = getattr(entry, "options", {}) or {}
    data = getattr(entry, "data", {}) or {}
    value = options.get("plugin") or data.get("plugin") or ""
    plugin = str(value).lower()
    if "plugin_" in plugin:
        plugin = plugin.rsplit("plugin_", 1)[1]
    if plugin.endswith(".py"):
        plugin = plugin[:-3]
    return plugin


def _entry_state(entry: Any) -> str:
    """Normalize a config-entry state enum or string."""
    state = getattr(entry, "state", "")
    return str(getattr(state, "value", state)).lower()


def _disabled_by(entry: Any) -> str | None:
    """Normalize the entity-registry disabled reason."""
    value = getattr(entry, "disabled_by", None)
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _is_solis_inverter_device(device: Any) -> bool:
    """Accept only inverter devices created by the standard Solis plugin."""
    if (
        device is None
        or getattr(device, "manufacturer", None) != SOLIS_MANUFACTURER
    ):
        return False

    return any(
        isinstance(identifier, (tuple, list))
        and len(identifier) >= 3
        and identifier[0] == SOLIS_DOMAIN
        and identifier[-1] == "inverter"
        for identifier in getattr(device, "identifiers", set())
    )


def _entity_key_matches(unique_id: str, source_key: str) -> bool:
    """Match the integration's source key without depending on hub naming."""
    return unique_id.endswith(f"_{source_key}")


def _source_from_entry(
    entity_entry: Any,
    device_registry: dr.DeviceRegistry,
) -> EntitySource:
    """Convert an entity-registry entry into a SEM source."""
    device_id = entity_entry.device_id
    device = (
        device_registry.async_get(device_id)
        if device_id is not None
        else None
    )
    parent_device_id = (
        getattr(device, "parent_device_id", None)
        or getattr(device, "via_device_id", None)
    ) if device is not None else None

    return EntitySource(
        entity_id=entity_entry.entity_id,
        unique_id=entity_entry.unique_id,
        platform=entity_entry.platform,
        device_id=device_id,
        parent_device_id=parent_device_id,
        root_device_id=parent_device_id or device_id,
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
    """Resolve one enabled source entity per expected inverter."""
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
        SOLIS_SYSTEM_ID,
        entity_key,
        status=status,
        entity_id=sources[0].entity_id if sources else None,
        unique_id=sources[0].unique_id if sources else None,
        platform=SOLIS_DOMAIN if sources else None,
        device_id=sources[0].device_id if sources else None,
        config_entry_id=sources[0].config_entry_id if sources else None,
        matches=[entry.entity_id for entry in all_matches],
        sources=sources,
    )



def _resolve_any_sources(
    registry: SemEntityRegistry,
    device_registry: dr.DeviceRegistry,
    *,
    entity_key: str,
    matches: Sequence[Any],
) -> None:
    """Resolve plant-level sources without requiring one meter per inverter."""
    active_by_device: dict[str, list[Any]] = defaultdict(list)
    disabled: list[Any] = []

    for entry in matches:
        if _disabled_by(entry) is None:
            active_by_device[str(entry.device_id)].append(entry)
        else:
            disabled.append(entry)

    duplicates = any(len(entries) > 1 for entries in active_by_device.values())
    active = [
        entry
        for device_id in sorted(active_by_device)
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

    sources = [_source_from_entry(entry, device_registry) for entry in selected]
    registry.set_entity_result(
        SOLIS_SYSTEM_ID,
        entity_key,
        status=status,
        entity_id=sources[0].entity_id if sources else None,
        unique_id=sources[0].unique_id if sources else None,
        platform=SOLIS_DOMAIN if sources else None,
        device_id=sources[0].device_id if sources else None,
        config_entry_id=sources[0].config_entry_id if sources else None,
        matches=[entry.entity_id for entry in active + disabled],
        sources=sources,
    )


class SolisAdapter(SemSystemAdapter):
    """Solis working-mode and time-of-use control adapter."""

    system_id = SOLIS_SYSTEM_ID
    integration_domain = SOLIS_DOMAIN

    @property
    def capabilities(self) -> SystemCapabilities:
        """Declare the SEM entities and command currently supported."""
        return SOLIS_CAPABILITIES

    def __init__(self) -> None:
        self._hass: HomeAssistant | None = None
        self._registry: SemEntityRegistry | None = None
        self._grid_charge_store: Store | None = None
        self._grid_charge_value: float | None = None
        self._grid_charge_override: dict[str, Any] | None = None
        self._grid_charge_lock = asyncio.Lock()
        self._maximum_discharging_power_store: Store | None = None
        self._maximum_discharging_power_value: float | None = None
        self._maximum_discharging_power_lock = asyncio.Lock()

    async def async_setup(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
    ) -> None:
        """Wait for the standard Solis plugin and resolve its entities."""
        self._hass = hass
        self._registry = registry
        self._grid_charge_store = Store(
            hass,
            SOLIS_GRID_CHARGE_STORE_VERSION,
            f"{SOLIS_SYSTEM_ID}_grid_charge_override",
        )
        stored_grid_charge = await self._grid_charge_store.async_load()
        if isinstance(stored_grid_charge, Mapping):
            try:
                stored_value = float(stored_grid_charge.get("value"))
            except (TypeError, ValueError):
                stored_value = None
            if (
                stored_value is not None
                and math.isfinite(stored_value)
                and stored_value >= 0
            ):
                self._grid_charge_value = stored_value
            stored_override = stored_grid_charge.get("override")
            if isinstance(stored_override, dict):
                self._grid_charge_override = stored_override

        self._maximum_discharging_power_store = Store(
            hass,
            SOLIS_MAXIMUM_DISCHARGING_STORE_VERSION,
            f"{SOLIS_SYSTEM_ID}_maximum_discharging_power",
        )
        stored_maximum_discharging_power = (
            await self._maximum_discharging_power_store.async_load()
        )
        if isinstance(stored_maximum_discharging_power, Mapping):
            try:
                stored_value = float(stored_maximum_discharging_power.get("value"))
            except (TypeError, ValueError):
                stored_value = None
            if (
                stored_value is not None
                and math.isfinite(stored_value)
                and stored_value >= 0
            ):
                self._maximum_discharging_power_value = stored_value

        entries = [
            entry
            for entry in hass.config_entries.async_entries(SOLIS_DOMAIN)
            if entry.disabled_by is None
            and _entry_plugin(entry) in SOLIS_SUPPORTED_PLUGINS
        ]

        if not entries:
            registry.register_system(
                self.system_id,
                configured=False,
                capabilities=self.capabilities,
            )
            _LOGGER.debug(
                "No standard Solis Modbus config entry found; adapter inactive"
            )
            return

        terminal_states = {"loaded", "failed", "not_loaded"}
        for _ in range(120):
            if all(_entry_state(entry) in terminal_states for entry in entries):
                break
            await asyncio.sleep(5)

        loaded_entries = [
            entry for entry in entries if _entry_state(entry) == "loaded"
        ]
        if not loaded_entries:
            registry.register_system(
                self.system_id,
                configured=True,
                capabilities=self.capabilities,
            )
            self._mark_all_entities_missing(registry)
            _LOGGER.warning(
                "Solis Modbus is configured but no standard Solis entry is loaded"
            )
            return

        # Let entity-registry entries appear after the integration has loaded.
        await asyncio.sleep(2)
        self._resolve_entities(hass, registry, loaded_entries)
        if self._grid_charge_value == 0 and self._grid_charge_override:
            try:
                await self._apply_grid_charge_override(
                    hass,
                    registry,
                    self._grid_charge_override,
                )
            except HomeAssistantError:
                _LOGGER.exception(
                    "Could not reapply the saved Solis grid-charge block"
                )
        if self._maximum_discharging_power_value is not None:
            try:
                await self._apply_maximum_discharging_power(
                    hass,
                    registry,
                    self._maximum_discharging_power_value,
                )
            except HomeAssistantError:
                _LOGGER.exception(
                    "Could not reapply the saved Solis maximum discharge power"
                )

    def _mark_all_entities_missing(
        self,
        registry: SemEntityRegistry,
    ) -> None:
        """Expose missing source entities in SEM's status data."""
        for entity_key in SOLIS_ENTITY_KEYS:
            registry.set_entity_result(
                self.system_id,
                entity_key,
                status="missing",
            )

    def _resolve_entities(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        config_entries: Sequence[Any],
    ) -> None:
        """Resolve mode, schedule-setting and schedule-button entities."""
        registry.register_system(
            self.system_id,
            configured=True,
            capabilities=self.capabilities,
        )

        loaded_entry_ids = {entry.entry_id for entry in config_entries}
        entity_registry = er.async_get(hass)
        device_registry = dr.async_get(hass)

        inverter_devices = {
            device.id: device
            for device in device_registry.devices.values()
            if set(getattr(device, "config_entries", set())).intersection(
                loaded_entry_ids
            )
            and _is_solis_inverter_device(device)
        }
        inverter_device_ids = sorted(inverter_devices)

        inverter_id_set = set(inverter_device_ids)
        entity_entries = [
            entry
            for entry in entity_registry.entities.values()
            if entry.platform == SOLIS_DOMAIN
            and entry.config_entry_id in loaded_entry_ids
            and entry.device_id in inverter_id_set
        ]

        # As in the SolaX adapter, use battery telemetry to distinguish
        # battery-capable inverters from any separate PV-only inverter.
        battery_device_ids = sorted(
            {
                entry.device_id
                for entry in entity_entries
                if entry.domain == "sensor"
                and entry.device_id is not None
                and (
                    _entity_key_matches(entry.unique_id, "battery_soc")
                    or _entity_key_matches(entry.unique_id, "battery_power")
                )
            }
        )
        if not battery_device_ids:
            # Preserve a visible missing-entity result if telemetry is absent.
            battery_device_ids = inverter_device_ids

        definitions = {
            "battery_working_mode": (
                "select",
                "energy_storage_control_switch",
            ),
            SOLIS_BATTERY_SOC_ENTITY_KEY: (
                "sensor",
                SOLIS_BATTERY_SOC_SOURCE_KEY,
            ),
            SOLIS_END_OF_DISCHARGE_SOC_ENTITY_KEY: (
                "number",
                SOLIS_END_OF_DISCHARGE_SOC_SOURCE_KEY,
            ),
            SOLIS_MAXIMUM_DISCHARGING_CURRENT_ENTITY_KEY: (
                "number",
                "battery_discharge_current",
            ),
            SOLIS_BATTERY_VOLTAGE_ENTITY_KEY: (
                "sensor",
                "battery_voltage",
            ),
            **{
                key: ("number", key)
                for key in SOLIS_TOU_NUMBER_ENTITY_KEYS
            },
            **{
                key: ("button", key)
                for key in SOLIS_TOU_BUTTON_ENTITY_KEYS
            },
        }

        for entity_key, (domain, source_key) in definitions.items():
            matches = [
                entry
                for entry in entity_entries
                if entry.domain == domain
                and _entity_key_matches(entry.unique_id, source_key)
            ]
            _resolve_expected_sources(
                registry,
                device_registry,
                entity_key=entity_key,
                matches=matches,
                expected_device_ids=battery_device_ids,
            )

        # PV production is measured once per inverter, including units without
        # a battery. pv_total_power already combines the inverter's PV strings.
        pv_power_matches = [
            entry
            for entry in entity_entries
            if entry.domain == "sensor"
            and _entity_key_matches(
                entry.unique_id,
                SOLIS_INVERTER_INPUT_POWER_SOURCE_KEY,
            )
        ]
        _resolve_expected_sources(
            registry,
            device_registry,
            entity_key=SOLIS_INVERTER_INPUT_POWER_ENTITY_KEY,
            matches=pv_power_matches,
            expected_device_ids=inverter_device_ids,
        )

        meter_power_matches = [
            entry
            for entry in entity_entries
            if entry.domain == "sensor"
            and _entity_key_matches(
                entry.unique_id,
                SOLIS_POWER_METER_SOURCE_KEY,
            )
        ]
        _resolve_any_sources(
            registry,
            device_registry,
            entity_key=SOLIS_POWER_METER_ENTITY_KEY,
            matches=meter_power_matches,
        )

    def get_sensor_value(
        self,
        entity_key: str,
        source_states: Sequence[State | None],
    ) -> float | str | None:
        """Return Solis battery telemetry using safe source data."""
        if entity_key == SOLIS_BATTERY_RATED_CAPACITY_ENTITY_KEY:
            if self._hass is None:
                return None
            helper_state = self._hass.states.get(SOLIS_TOTAL_CAPACITY_HELPER)
            if helper_state is None:
                return None
            return solis_total_capacity_kwh(helper_state.state)

        if entity_key == SOLIS_POWER_METER_ENTITY_KEY:
            if not source_states or any(
                state is None or state.state in ("unknown", "unavailable")
                for state in source_states
            ):
                return None

            if (
                self._registry is None
                or self._registry.get_entity_status(
                    self.system_id,
                    SOLIS_POWER_METER_ENTITY_KEY,
                ) != "found"
            ):
                return None

            sources = self._registry.get_entity_sources(
                self.system_id,
                SOLIS_POWER_METER_ENTITY_KEY,
            )
            device_ids = [source.device_id for source in sources]
            if (
                len(sources) != len(source_states)
                or any(device_id is None for device_id in device_ids)
                or len(set(device_ids)) != len(device_ids)
            ):
                return None

            # Solis register 33130: positive is export; negative is import.
            # Multiple inverters may mirror the same plant meter reading.
            return plant_meter_value(
                [state.state for state in source_states if state is not None]
            )

        if entity_key == SOLIS_INVERTER_INPUT_POWER_ENTITY_KEY:
            if not source_states or any(
                state is None or state.state in ("unknown", "unavailable")
                for state in source_states
            ):
                return None

            if (
                self._registry is None
                or self._registry.get_entity_status(
                    self.system_id,
                    SOLIS_INVERTER_INPUT_POWER_ENTITY_KEY,
                ) != "found"
            ):
                return None

            sources = self._registry.get_entity_sources(
                self.system_id,
                SOLIS_INVERTER_INPUT_POWER_ENTITY_KEY,
            )
            device_ids = [source.device_id for source in sources]
            if (
                len(sources) != len(source_states)
                or any(device_id is None for device_id in device_ids)
                or len(set(device_ids)) != len(device_ids)
            ):
                return None

            return sum_solis_pv_power(
                [state.state for state in source_states if state is not None]
            )

        if entity_key != SOLIS_BATTERY_SOC_ENTITY_KEY:
            return super().get_sensor_value(entity_key, source_states)

        if not source_states or any(
            state is None or state.state in ("unknown", "unavailable")
            for state in source_states
        ):
            return None

        if (
            self._registry is None
            or self._registry.get_entity_status(
                self.system_id,
                SOLIS_BATTERY_SOC_ENTITY_KEY,
            ) != "found"
        ):
            return None

        sources = self._registry.get_entity_sources(
            self.system_id,
            SOLIS_BATTERY_SOC_ENTITY_KEY,
        )
        if len(sources) != len(source_states):
            return None

        device_ids = [source.device_id for source in sources]
        if (
            any(device_id is None for device_id in device_ids)
            or len(set(device_ids)) != len(device_ids)
        ):
            return None

        # The standard plugin does not expose per-battery rated capacity.
        # Multiple readings are safe only when they agree; never publish an
        # unweighted average for batteries with different SOC values.
        return solis_battery_soc_value(
            [state.state for state in source_states if state is not None]
        )

    def get_related_entity_ids(self, entity_key: str) -> list[str]:
        """Return helper entities that affect derived Solis sensors."""
        if entity_key == SOLIS_BATTERY_RATED_CAPACITY_ENTITY_KEY:
            return [SOLIS_TOTAL_CAPACITY_HELPER]
        return []

    def get_sensor_metadata(
        self,
        entity_key: str,
        source_attributes: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Expose the Solis battery capacity in the shared SEM kWh unit."""
        if entity_key == SOLIS_BATTERY_RATED_CAPACITY_ENTITY_KEY:
            return {
                "device_class": "energy",
                "unit_of_measurement": "kWh",
                "suggested_display_precision": 2,
            }
        if entity_key == SOLIS_INVERTER_INPUT_POWER_ENTITY_KEY:
            return {
                "device_class": "power",
                "state_class": "measurement",
                "unit_of_measurement": "W",
                "suggested_display_precision": 0,
            }
        if entity_key == SOLIS_POWER_METER_ENTITY_KEY:
            return {
                "device_class": "power",
                "state_class": "measurement",
                "unit_of_measurement": "W",
                "suggested_display_precision": 0,
            }
        return super().get_sensor_metadata(entity_key, source_attributes)

    def _time_control_available(self) -> bool:
        """Require all local time values and all three update buttons."""
        if self._hass is None or self._registry is None:
            return False

        for entity_key in (
            *SOLIS_TOU_NUMBER_ENTITY_KEYS,
            *SOLIS_TOU_BUTTON_ENTITY_KEYS,
        ):
            if self._registry.get_entity_status(
                self.system_id,
                entity_key,
            ) != "found":
                return False

            sources = self._registry.get_entity_sources(
                self.system_id,
                entity_key,
            )
            if not sources:
                return False

            for source in sources:
                state = self._hass.states.get(source.entity_id)
                if state is None or state.state == "unavailable":
                    return False
                if (
                    entity_key in SOLIS_TOU_NUMBER_ENTITY_KEYS
                    and state.state == "unknown"
                ):
                    return False

        return True


    def has_virtual_number(self, entity_key: str) -> bool:
        """Expose the grid-charge permission as an adapter-managed number."""
        if entity_key == SOLIS_GRID_CHARGE_MAXIMUM_POWER_KEY:
            return self._grid_charge_override_supported()
        if entity_key == SOLIS_MAXIMUM_DISCHARGING_POWER_KEY:
            return self._maximum_discharging_power_supported()
        return False

    def get_virtual_number_value(self, entity_key: str) -> float | None:
        """Return the configured Solis grid-charge permission value."""
        if entity_key == SOLIS_GRID_CHARGE_MAXIMUM_POWER_KEY:
            return self._grid_charge_value
        if entity_key == SOLIS_MAXIMUM_DISCHARGING_POWER_KEY:
            return self._maximum_discharging_power_value
        return None

    def get_virtual_number_metadata(self, entity_key: str) -> dict[str, Any]:
        """Expose the watt-based SEM number range."""
        if entity_key == SOLIS_GRID_CHARGE_MAXIMUM_POWER_KEY:
            return dict(SOLIS_GRID_CHARGE_NUMBER_METADATA)
        if entity_key == SOLIS_MAXIMUM_DISCHARGING_POWER_KEY:
            return dict(SOLIS_MAXIMUM_DISCHARGING_NUMBER_METADATA)
        return {}

    def _grid_charge_mode_entities(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
    ) -> dict[str, str]:
        """Resolve exactly one working-mode select for every battery inverter."""
        if registry.get_entity_status(
            self.system_id,
            "battery_working_mode",
        ) != "found":
            raise HomeAssistantError(
                "Solis battery working-mode entities are not fully resolved"
            )

        sources = registry.get_entity_sources(
            self.system_id,
            "battery_working_mode",
        )
        entities: dict[str, str] = {}
        for source in sources:
            if source.device_id is None or source.device_id in entities:
                raise HomeAssistantError(
                    "Solis working-mode sources do not map one-to-one "
                    "to battery inverters"
                )
            state = hass.states.get(source.entity_id)
            if (
                state is None
                or state.state in ("unknown", "unavailable")
                or SOLIS_GRID_CHARGE_NO_GRID_SOURCE_OPTION
                not in state.attributes.get("options", [])
            ):
                raise HomeAssistantError(
                    f"Solis working-mode entity {source.entity_id} is "
                    "unavailable or lacks the no-grid-charging option"
                )
            entities[source.device_id] = source.entity_id

        if not entities:
            raise HomeAssistantError(
                "No Solis battery working-mode entities were found"
            )
        return entities

    def _build_grid_charge_override(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
    ) -> dict[str, Any]:
        """Capture the current SEM mode for restoration after blocking grid charge."""
        entities = self._grid_charge_mode_entities(hass, registry)
        devices = []
        for device_id, entity_id in sorted(entities.items()):
            state = hass.states.get(entity_id)
            assert state is not None
            current_option = state.state
            if current_option not in SOLIS_SOURCE_OPTION_TO_MODE:
                raise HomeAssistantError(
                    f"Cannot safely block Solis grid charging while "
                    f"{entity_id} is in unsupported mode {current_option!r}"
                )
            blocked_option = solis_grid_charge_blocked_option(
                SOLIS_SOURCE_OPTION_TO_MODE[current_option]
            )
            if blocked_option is None or blocked_option not in state.attributes.get(
                "options", []
            ):
                raise HomeAssistantError(
                    f"Solis inverter {entity_id} does not support a safe "
                    "no-grid-charging working mode"
                )
            devices.append(
                {
                    "device_id": device_id,
                    "previous_option": current_option,
                    "blocked_option": blocked_option,
                }
            )

        return {"active": True, "devices": devices}

    def _grid_charge_override_supported(self) -> bool:
        """Require every battery inverter to support safe mode switching."""
        if self._hass is None or self._registry is None:
            return False
        try:
            entities = self._grid_charge_mode_entities(
                self._hass,
                self._registry,
            )
            if self._grid_charge_override is None:
                self._build_grid_charge_override(
                    self._hass,
                    self._registry,
                )
                return True

            saved_devices = self._grid_charge_override.get("devices")
            if not isinstance(saved_devices, list):
                return False
            saved_by_device = {
                item.get("device_id"): item
                for item in saved_devices
                if isinstance(item, dict)
            }
            if set(saved_by_device) != set(entities):
                return False
            for device_id, entity_id in entities.items():
                item = saved_by_device[device_id]
                state = self._hass.states.get(entity_id)
                if state is None:
                    return False
                options = state.attributes.get("options", [])
                if item.get("previous_option") not in options:
                    return False
                if item.get("blocked_option") not in options:
                    return False
            return True
        except (HomeAssistantError, TypeError):
            return False

    async def _save_grid_charge_state(self) -> None:
        """Persist the requested number and working-mode restoration snapshot."""
        if self._grid_charge_store is None:
            return
        await self._grid_charge_store.async_save(
            {
                "value": self._grid_charge_value,
                "override": self._grid_charge_override,
            }
        )

    @staticmethod
    async def _set_working_mode_options(
        hass: HomeAssistant,
        options_by_entity: Mapping[str, str],
    ) -> None:
        """Apply select options by group, avoiding unnecessary Modbus writes."""
        by_option: dict[str, list[str]] = defaultdict(list)
        for entity_id, option in options_by_entity.items():
            state = hass.states.get(entity_id)
            if (
                state is None
                or state.state in ("unknown", "unavailable")
                or option not in state.attributes.get("options", [])
            ):
                raise HomeAssistantError(
                    f"Solis working-mode entity {entity_id} cannot select "
                    f"{option!r}"
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

    async def _apply_grid_charge_override(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        override: Mapping[str, Any],
    ) -> None:
        """Apply saved no-grid-charging modes to every battery inverter."""
        entities = self._grid_charge_mode_entities(hass, registry)
        saved_devices = override.get("devices")
        if not isinstance(saved_devices, list):
            raise HomeAssistantError("Solis grid-charge override is invalid")
        saved_by_device = {
            item.get("device_id"): item
            for item in saved_devices
            if isinstance(item, dict)
        }
        if set(saved_by_device) != set(entities):
            raise HomeAssistantError(
                "Solis grid-charge override does not match the detected batteries"
            )
        options_by_entity = {
            entity_id: saved_by_device[device_id]["blocked_option"]
            for device_id, entity_id in entities.items()
        }
        await self._set_working_mode_options(hass, options_by_entity)

    async def _restore_grid_charge_override(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        override: Mapping[str, Any],
    ) -> None:
        """Restore the exact working mode that was active before the block."""
        entities = self._grid_charge_mode_entities(hass, registry)
        saved_devices = override.get("devices")
        if not isinstance(saved_devices, list):
            raise HomeAssistantError("Solis grid-charge override is invalid")
        saved_by_device = {
            item.get("device_id"): item
            for item in saved_devices
            if isinstance(item, dict)
        }
        if set(saved_by_device) != set(entities):
            raise HomeAssistantError(
                "Solis grid-charge override does not match the detected batteries"
            )
        options_by_entity = {
            entity_id: saved_by_device[device_id]["previous_option"]
            for device_id, entity_id in entities.items()
        }
        await self._set_working_mode_options(hass, options_by_entity)


    def _maximum_discharging_device_ids(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
    ) -> list[str]:
        """Return battery inverter IDs resolved through working-mode entities."""
        if registry.get_entity_status(
            self.system_id,
            "battery_working_mode",
        ) != "found":
            raise HomeAssistantError(
                "Solis battery working-mode entities are not fully resolved"
            )
        sources = registry.get_entity_sources(
            self.system_id,
            "battery_working_mode",
        )
        device_ids = [source.device_id for source in sources]
        if (
            not device_ids
            or any(device_id is None for device_id in device_ids)
            or len(set(device_ids)) != len(device_ids)
        ):
            raise HomeAssistantError(
                "Solis working-mode sources do not map one-to-one "
                "to battery inverters"
            )
        return sorted(device_ids)

    def _maximum_discharging_power_supported(self) -> bool:
        """Require voltage telemetry and a writable current number per inverter."""
        if self._hass is None or self._registry is None:
            return False
        try:
            device_ids = self._maximum_discharging_device_ids(
                self._hass,
                self._registry,
            )
            self._require_device_entities(
                self._hass,
                self._registry,
                entity_key=SOLIS_MAXIMUM_DISCHARGING_CURRENT_ENTITY_KEY,
                domain="number",
                device_ids=device_ids,
            )
            voltage_entities = self._require_device_entities(
                self._hass,
                self._registry,
                entity_key=SOLIS_BATTERY_VOLTAGE_ENTITY_KEY,
                domain="sensor",
                device_ids=device_ids,
            )
            for entity_id in voltage_entities.values():
                state = self._hass.states.get(entity_id)
                if state is None or state.state in ("unknown", "unavailable"):
                    return False
                if state.attributes.get("unit_of_measurement") not in ("V", "volt"):
                    return False
                voltage = float(state.state)
                if not math.isfinite(voltage) or voltage <= 0:
                    return False
            return True
        except (HomeAssistantError, TypeError, ValueError):
            return False

    async def _save_maximum_discharging_power_state(self) -> None:
        """Persist the plant discharge-power ceiling across restarts."""
        if self._maximum_discharging_power_store is None:
            return
        await self._maximum_discharging_power_store.async_save(
            {"value": self._maximum_discharging_power_value}
        )

    async def _apply_maximum_discharging_power(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        maximum_power_w: float,
    ) -> None:
        """Convert the total W ceiling into current limits for all battery inverters."""
        device_ids = self._maximum_discharging_device_ids(hass, registry)
        current_entities = self._require_device_entities(
            hass,
            registry,
            entity_key=SOLIS_MAXIMUM_DISCHARGING_CURRENT_ENTITY_KEY,
            domain="number",
            device_ids=device_ids,
        )
        voltage_entities = self._require_device_entities(
            hass,
            registry,
            entity_key=SOLIS_BATTERY_VOLTAGE_ENTITY_KEY,
            domain="sensor",
            device_ids=device_ids,
        )

        voltages: dict[str, float] = {}
        current_limits: dict[str, dict[str, float]] = {}
        previous_values: dict[str, float] = {}
        for device_id in device_ids:
            voltage_state = hass.states.get(voltage_entities[device_id])
            current_state = hass.states.get(current_entities[device_id])
            if (
                voltage_state is None
                or current_state is None
                or voltage_state.state in ("unknown", "unavailable")
                or current_state.state in ("unknown", "unavailable")
            ):
                raise HomeAssistantError(
                    f"Solis battery data is unavailable for inverter {device_id}"
                )
            if voltage_state.attributes.get("unit_of_measurement") not in ("V", "volt"):
                raise HomeAssistantError(
                    f"Solis battery voltage {voltage_entities[device_id]} "
                    "does not use volts"
                )
            if current_state.attributes.get("unit_of_measurement") not in ("A", "ampere"):
                raise HomeAssistantError(
                    f"Solis discharge current {current_entities[device_id]} "
                    "does not use amperes"
                )
            try:
                voltage = float(voltage_state.state)
                current = float(current_state.state)
                limits = {
                    "min": float(current_state.attributes["min"]),
                    "max": float(current_state.attributes["max"]),
                    "step": float(current_state.attributes["step"]),
                }
            except (KeyError, TypeError, ValueError) as err:
                raise HomeAssistantError(
                    f"Solis battery voltage or discharge-current limits are "
                    f"invalid for inverter {device_id}"
                ) from err
            if (
                not math.isfinite(voltage)
                or voltage <= 0
                or not math.isfinite(current)
                or not all(math.isfinite(item) for item in limits.values())
                or limits["min"] > limits["max"]
                or limits["step"] <= 0
            ):
                raise HomeAssistantError(
                    f"Solis battery voltage or discharge-current limits are "
                    f"invalid for inverter {device_id}"
                )
            voltages[device_id] = voltage
            current_limits[device_id] = limits
            previous_values[current_entities[device_id]] = current

        try:
            target_values = build_solis_discharge_current_values(
                maximum_power_w,
                voltages,
                current_limits,
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

        changed: dict[str, float] = {}
        for device_id, target_current in target_values.items():
            entity_id = current_entities[device_id]
            if not math.isclose(
                previous_values[entity_id],
                target_current,
                rel_tol=0.0,
                abs_tol=1e-6,
            ):
                changed[entity_id] = target_current

        if not changed:
            return
        if not hass.services.has_service("number", "set_value"):
            raise HomeAssistantError(
                "Home Assistant number.set_value service is not available"
            )

        results = await asyncio.gather(
            *(
                hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": entity_id, "value": target_current},
                    blocking=True,
                )
                for entity_id, target_current in changed.items()
            ),
            return_exceptions=True,
        )
        failures = [
            f"{entity_id}: {result}"
            for entity_id, result in zip(changed, results)
            if isinstance(result, Exception)
        ]
        if failures:
            rollback_results = await asyncio.gather(
                *(
                    hass.services.async_call(
                        "number",
                        "set_value",
                        {"entity_id": entity_id, "value": previous_values[entity_id]},
                        blocking=True,
                    )
                    for entity_id in changed
                ),
                return_exceptions=True,
            )
            rollback_failures = [
                f"{entity_id}: {result}"
                for entity_id, result in zip(changed, rollback_results)
                if isinstance(result, Exception)
            ]
            message = (
                "Solis maximum discharge power could not be applied to every "
                "battery inverter: " + "; ".join(failures)
            )
            if rollback_failures:
                message += (
                    ". Rollback also failed for: "
                    + "; ".join(rollback_failures)
                )
            raise HomeAssistantError(message)

    async def _async_set_maximum_discharging_power(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        value: float,
    ) -> None:
        """Set and persist the requested total Solis battery discharge power."""
        if (
            not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise HomeAssistantError(
                "Solis maximum discharge power must be a finite non-negative number"
            )
        if not self._maximum_discharging_power_supported():
            raise HomeAssistantError(
                "Solis maximum discharge power requires an enabled Battery "
                "Discharge Current number and Battery Voltage sensor on every "
                "battery inverter"
            )

        async with self._maximum_discharging_power_lock:
            await self._apply_maximum_discharging_power(
                hass,
                registry,
                float(value),
            )
            self._maximum_discharging_power_value = float(value)
            await self._save_maximum_discharging_power_state()

    async def async_set_number_value(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        entity_key: str,
        value: float,
    ) -> bool:
        """Block grid charging at zero and restore the previous mode above zero."""
        if entity_key == SOLIS_MAXIMUM_DISCHARGING_POWER_KEY:
            await self._async_set_maximum_discharging_power(
                hass,
                registry,
                value,
            )
            return True
        if entity_key != SOLIS_GRID_CHARGE_MAXIMUM_POWER_KEY:
            return False
        if (
            not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise HomeAssistantError(
                "Solis grid-charge maximum power must be a finite number "
                "greater than or equal to zero"
            )

        async with self._grid_charge_lock:
            if value == 0:
                if not self._grid_charge_override_supported():
                    raise HomeAssistantError(
                        "Solis grid-charge blocking is not supported by every "
                        "detected battery inverter"
                    )
                if self._grid_charge_override is None:
                    self._grid_charge_override = self._build_grid_charge_override(
                        hass,
                        registry,
                    )
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

    async def async_after_select_option(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        entity_key: str,
        sem_option: str,
    ) -> None:
        """Keep grid charging blocked when SEM changes the working mode."""
        if (
            entity_key != "battery_working_mode"
            or self._grid_charge_value != 0
            or self._grid_charge_override is None
        ):
            return

        previous_option = SOLIS_MODE_TO_SOURCE_OPTION.get(sem_option)
        blocked_option = solis_grid_charge_blocked_option(sem_option)
        if previous_option is None or blocked_option is None:
            raise HomeAssistantError(
                f"Unsupported SEM Solis working mode while grid charging is "
                f"blocked: {sem_option}"
            )

        entities = self._grid_charge_mode_entities(hass, registry)
        saved_devices = self._grid_charge_override.get("devices")
        if not isinstance(saved_devices, list):
            raise HomeAssistantError("Solis grid-charge override is invalid")
        saved_by_device = {
            item.get("device_id"): item
            for item in saved_devices
            if isinstance(item, dict)
        }
        if set(saved_by_device) != set(entities):
            raise HomeAssistantError(
                "Solis grid-charge override does not match the detected batteries"
            )

        for device_id, entity_id in entities.items():
            state = hass.states.get(entity_id)
            if state is None or blocked_option not in state.attributes.get(
                "options", []
            ):
                raise HomeAssistantError(
                    f"Solis inverter {entity_id} cannot use {blocked_option!r}"
                )
            saved_by_device[device_id]["previous_option"] = previous_option
            saved_by_device[device_id]["blocked_option"] = blocked_option

        self._grid_charge_override["devices"] = list(saved_by_device.values())
        await self._save_grid_charge_state()

    def get_sem_options(
        self,
        entity_key: str,
        source_options: Sequence[Sequence[str]],
    ) -> list[str]:
        """Expose only the three normalized modes supported on every inverter."""
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

        sources = self._registry.get_entity_sources(
            self.system_id,
            entity_key,
        )
        if not sources or len(sources) != len(source_options):
            return []

        sem_options = [
            sem_option
            for sem_option, source_option in SOLIS_MODE_TO_SOURCE_OPTION.items()
            if all(source_option in options for options in source_options)
            and (
                sem_option != "time_of_use"
                or self._time_control_available()
            )
        ]
        return sem_options

    def to_source_options(
        self,
        entity_key: str,
        sem_option: str,
        source_options: Sequence[Sequence[str]],
    ) -> list[str]:
        """Translate a SEM working mode into Solis option labels."""
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
                "Solis working-mode source is missing, disabled or ambiguous"
            )

        sources = self._registry.get_entity_sources(
            self.system_id,
            entity_key,
        )
        if not sources or len(sources) != len(source_options):
            raise ValueError("No complete set of Solis working-mode sources found")

        source_option = SOLIS_MODE_TO_SOURCE_OPTION.get(sem_option)
        if self._grid_charge_value == 0 and self._grid_charge_override is not None:
            source_option = solis_grid_charge_blocked_option(sem_option)
        if source_option is None:
            raise ValueError(f"Unsupported SEM Solis working mode: {sem_option}")

        if sem_option == "time_of_use" and not self._time_control_available():
            raise ValueError(
                "Solis time-of-use control requires all three schedule blocks "
                "and their update buttons to be enabled."
            )

        for source, options in zip(sources, source_options):
            if source_option not in options:
                raise ValueError(
                    f"Solis mode {source_option!r} is not supported by "
                    f"{source.entity_id}"
                )

        return [source_option for _ in sources]

    def to_sem_option(
        self,
        entity_key: str,
        source_options: Sequence[str | None],
    ) -> str | None:
        """Translate a physical Solis working mode back to the SEM option."""
        if entity_key != "battery_working_mode":
            return super().to_sem_option(entity_key, source_options)

        translated = []
        for source_option in source_options:
            if (
                self._grid_charge_value == 0
                and self._grid_charge_override is not None
                and source_option == SOLIS_GRID_CHARGE_NO_GRID_SOURCE_OPTION
            ):
                sem_option = "time_of_use"
            else:
                sem_option = SOLIS_SOURCE_OPTION_TO_MODE.get(source_option)
            if sem_option == "time_of_use" and not self._time_control_available():
                sem_option = None
            translated.append(sem_option)

        return super().to_sem_option(entity_key, translated)

    def _require_device_entities(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        *,
        entity_key: str,
        domain: str,
        device_ids: Sequence[str],
    ) -> dict[str, str]:
        """Return one available entity per expected inverter."""
        if registry.get_entity_status(self.system_id, entity_key) != "found":
            raise HomeAssistantError(
                f"Solis entity {entity_key} is missing, disabled or ambiguous"
            )

        entities_by_device: dict[str, str] = {}
        for source in registry.get_entity_sources(self.system_id, entity_key):
            if source.device_id is None or source.device_id not in device_ids:
                raise HomeAssistantError(
                    f"Solis entity {source.entity_id} has no expected inverter"
                )
            if source.device_id in entities_by_device:
                raise HomeAssistantError(
                    f"Multiple Solis {entity_key} entities found for "
                    f"inverter {source.device_id}"
                )
            if not source.entity_id.startswith(f"{domain}."):
                raise HomeAssistantError(
                    f"Unexpected entity domain for Solis entity {source.entity_id}"
                )

            state = hass.states.get(source.entity_id)
            if (
                state is None
                or state.state == "unavailable"
                or (
                    domain == "number"
                    and state.state == "unknown"
                )
            ):
                raise HomeAssistantError(
                    f"Solis entity {source.entity_id} is unavailable"
                )
            entities_by_device[source.device_id] = source.entity_id

        if set(entities_by_device) != set(device_ids):
            raise HomeAssistantError(
                f"Solis entity {entity_key} is not resolved on every inverter"
            )

        return entities_by_device

    async def async_execute(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        command: str,
        data: dict[str, Any],
    ) -> None:
        """Write SEM TOU periods into Solis local time values and device registers."""
        if command != "set_battery_time_of_use_periods":
            raise HomeAssistantError(
                f"Unsupported SEM command for Solis: {command}"
            )
        if "periods" not in data:
            raise HomeAssistantError(
                "Missing data for SEM command set_battery_time_of_use_periods"
            )

        try:
            periods = parse_solis_tou_periods(data["periods"])
        except (TypeError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

        if registry.get_entity_status(
            self.system_id,
            "battery_working_mode",
        ) != "found":
            raise HomeAssistantError(
                "Solis battery working-mode entities are not fully resolved"
            )

        mode_sources = registry.get_entity_sources(
            self.system_id,
            "battery_working_mode",
        )
        device_ids = [
            source.device_id
            for source in mode_sources
            if source.device_id is not None
        ]
        if (
            not device_ids
            or len(device_ids) != len(mode_sources)
            or len(set(device_ids)) != len(device_ids)
        ):
            raise HomeAssistantError(
                "Solis working-mode sources do not resolve one-to-one "
                "to inverter devices"
            )
        device_ids = sorted(device_ids)

        if not hass.services.has_service("number", "set_value"):
            raise HomeAssistantError(
                "Home Assistant number.set_value service is not available"
            )
        if not hass.services.has_service("button", "press"):
            raise HomeAssistantError(
                "Home Assistant button.press service is not available"
            )

        number_entities = {
            entity_key: self._require_device_entities(
                hass,
                registry,
                entity_key=entity_key,
                domain="number",
                device_ids=device_ids,
            )
            for entity_key in SOLIS_TOU_NUMBER_ENTITY_KEYS
        }
        button_entities = {
            entity_key: self._require_device_entities(
                hass,
                registry,
                entity_key=entity_key,
                domain="button",
                device_ids=device_ids,
            )
            for entity_key in SOLIS_TOU_BUTTON_ENTITY_KEYS
        }

        time_values = build_solis_time_values(periods)

        # Stage the complete schedule on every inverter first. The Solis
        # number entities store these values locally until their slot button
        # is pressed, so no device schedule is changed during this phase.
        try:
            for device_id in device_ids:
                for entity_key, value in time_values.items():
                    await hass.services.async_call(
                        "number",
                        "set_value",
                        {
                            "entity_id": number_entities[entity_key][device_id],
                            "value": value,
                        },
                        blocking=True,
                    )
        except Exception as err:
            raise HomeAssistantError(
                f"Could not stage Solis TOU values: {err}"
            ) from err

        # Commit each of the three physical time slots on every inverter.
        # Unused slots were explicitly cleared to 00:00-00:00 above.
        try:
            for device_id in device_ids:
                for entity_key in SOLIS_TOU_BUTTON_ENTITY_KEYS:
                    await hass.services.async_call(
                        "button",
                        "press",
                        {
                            "entity_id": button_entities[entity_key][device_id],
                        },
                        blocking=True,
                    )
        except Exception as err:
            raise HomeAssistantError(
                "Solis TOU schedule button press failed for inverter "
                f"{device_id}: {err}"
            ) from err

        _LOGGER.info(
            "Applied SEM time-of-use schedule to %d Solis inverter(s)",
            len(device_ids),
        )

    def get_setup_errors(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
    ) -> list[dict[str, Any]]:
        """Report a missing or ambiguous Solis working-mode select."""
        status = registry.get_entity_status(
            self.system_id,
            "battery_working_mode",
        )
        if status in (None, "pending", "found"):
            return []

        return [
            {
                "code": "solis_working_mode_unresolved",
                "setting": "Solis arbetsläge",
                "message": (
                    "SEM kunde inte hitta en entydig Energy Storage Control "
                    "Switch på samtliga Solis-växelriktare."
                ),
                "action": (
                    "Kontrollera att standardpluginen Solis är vald i SolaX "
                    "Modbus-integrationen och att Energy Storage Control "
                    "Switch är aktiverad."
                ),
            }
        ]
