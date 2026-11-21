"""Read-only adapter for supported SolaX Modbus inverter families."""

from __future__ import annotations

import asyncio
import logging
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

# The standard plugin covers the supported Gen2-6 product family. The LV
# plugin uses the same read entity keys and is included as a compatible profile.
SUPPORTED_PLUGINS = frozenset({"solax", "solax_lv"})

SOLAX_REQUIRED_ENTITIES = (
    "battery_state_of_capacity",
    "battery_charge_discharge_power",
    "power_meter_active_power",
)

SOLAX_OPTIONAL_ENTITIES = (
    "battery_rated_capacity",
    "battery_day_charge",
    "battery_day_discharge",
    "inverter_input_power",
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
    """Read-only adapter for SolaX Modbus hybrid inverters."""

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
        """Describe read availability separately from unimplemented write features."""
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
        capabilities.update(
            {
                "read.battery_working_mode": "not_implemented",
                "write.battery_working_mode": "not_implemented",
                "write.grid_charge_maximum_power": "not_supported",
                "write.maximum_discharging_power": "not_supported",
                "write.end_of_discharge_soc": "not_implemented",
                "write.grid_export_power_limit": "not_implemented",
                "write.time_of_use": "not_implemented",
            }
        )
        return capabilities

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
        """Report unsafe SOC aggregation or conflicting plant-meter readings."""
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

        return {"errors": errors} if errors else {}

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
        """Keep all SolaX write operations disabled in the read-only phase."""
        raise HomeAssistantError(
            "SolaX write support is not enabled. No device setting was changed."
        )
