"""Adapter for Solis hybrid inverters using the SolaX Modbus integration."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .adapter import SemSystemAdapter
from .capabilities import EntityCapability, SystemCapabilities
from .registry import EntitySource, SemEntityRegistry
from .solis_helpers import (
    SOLIS_END_OF_DISCHARGE_SOC_ENTITY_KEY,
    SOLIS_END_OF_DISCHARGE_SOC_SOURCE_KEY,
    SOLIS_MODE_TO_SOURCE_OPTION,
    SOLIS_SOURCE_OPTION_TO_MODE,
    SOLIS_TOU_BUTTON_ENTITY_KEYS,
    SOLIS_TOU_NUMBER_ENTITY_KEYS,
    build_solis_time_values,
    parse_solis_tou_periods,
)

_LOGGER = logging.getLogger(__name__)

SOLIS_DOMAIN = "solax_modbus"
SOLIS_SYSTEM_ID = "solis"
SOLIS_MANUFACTURER = "Ginlog Solis"
SOLIS_SUPPORTED_PLUGINS = frozenset({"solis"})

SOLIS_ENTITY_KEYS = (
    "battery_working_mode",
    SOLIS_END_OF_DISCHARGE_SOC_ENTITY_KEY,
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
        SOLIS_END_OF_DISCHARGE_SOC_ENTITY_KEY: EntityCapability(
            required=False,
            readable=True,
            writable=True,
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

    async def async_setup(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
    ) -> None:
        """Wait for the standard Solis plugin and resolve its entities."""
        self._hass = hass
        self._registry = registry

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
            SOLIS_END_OF_DISCHARGE_SOC_ENTITY_KEY: (
                "number",
                SOLIS_END_OF_DISCHARGE_SOC_SOURCE_KEY,
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
