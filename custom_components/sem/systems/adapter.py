"""Base contract for SEM energy-system adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any

from homeassistant.core import HomeAssistant, State

from .registry import SemEntityRegistry


class SemSystemAdapter(ABC):
    """Base class implemented by each supported energy-system adapter."""

    system_id: str
    integration_domain: str

    @abstractmethod
    async def async_setup(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
    ) -> None:
        """Detect and resolve the external system."""

    def get_sensor_value(
        self,
        entity_key: str,
        source_states: Sequence[State | None],
    ) -> float | str | None:
        """Return a normalized sensor value from resolved source entities."""
        if not source_states or any(
            state is None or state.state in ("unknown", "unavailable")
            for state in source_states
        ):
            return None

        if len(source_states) != 1:
            return None

        state = source_states[0]
        assert state is not None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return state.state

    def get_sensor_metadata(
        self,
        entity_key: str,
        source_attributes: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Return shared measurement metadata for resolved source sensors."""
        if not source_attributes:
            return {}

        first = dict(source_attributes[0])
        metadata_keys = (
            "device_class",
            "state_class",
            "unit_of_measurement",
            "suggested_display_precision",
        )
        for key in metadata_keys:
            values = {item.get(key) for item in source_attributes}
            if len(values) > 1:
                return {}

        return {key: first.get(key) for key in metadata_keys}

    def get_number_value(
        self,
        entity_key: str,
        source_states: Sequence[State | None],
    ) -> float | None:
        """Return a shared numeric setting if all physical sources agree."""
        if not source_states or any(
            state is None or state.state in ("unknown", "unavailable")
            for state in source_states
        ):
            return None

        values = []
        for state in source_states:
            assert state is not None
            try:
                values.append(float(state.state))
            except (TypeError, ValueError):
                return None

        first = values[0]
        return first if all(abs(value - first) <= 1e-6 for value in values) else None

    def get_number_metadata(
        self,
        entity_key: str,
        source_attributes: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Return a safe common number range accepted by every source."""
        if not source_attributes:
            return {}

        units = {item.get("unit_of_measurement") for item in source_attributes}
        modes = {item.get("mode", "auto") for item in source_attributes}
        if len(units) != 1 or len(modes) != 1:
            return {}

        try:
            minimum = max(float(item.get("min", 0)) for item in source_attributes)
            maximum = min(float(item.get("max", 100)) for item in source_attributes)
            steps = {float(item.get("step", 1)) for item in source_attributes}
        except (TypeError, ValueError):
            return {}

        if minimum > maximum or len(steps) != 1 or next(iter(steps)) <= 0:
            return {}

        return {
            "min_value": minimum,
            "max_value": maximum,
            "step": next(iter(steps)),
            "unit_of_measurement": next(iter(units)),
            "mode": next(iter(modes)),
        }

    def get_sem_options(
        self,
        entity_key: str,
        source_options: Sequence[Sequence[str]],
    ) -> list[str]:
        """Expose only options supported by every physical source."""
        if not source_options:
            return []
        common = set(source_options[0])
        for options in source_options[1:]:
            common.intersection_update(options)
        return [option for option in source_options[0] if option in common]

    def to_source_options(
        self,
        entity_key: str,
        sem_option: str,
        source_options: Sequence[Sequence[str]],
    ) -> list[str]:
        """Translate a SEM select option for each physical source."""
        results = []
        for options in source_options:
            if sem_option not in options:
                raise ValueError(
                    f"Unsupported option {sem_option!r} for {entity_key}"
                )
            results.append(sem_option)
        if not results:
            raise ValueError(f"No source entities resolved for {entity_key}")
        return results

    def to_sem_option(
        self,
        entity_key: str,
        source_options: Sequence[str | None],
    ) -> str | None:
        """Return the shared SEM option only when all sources agree."""
        if not source_options or any(option is None for option in source_options):
            return None
        first = source_options[0]
        return first if all(option == first for option in source_options) else None

    def get_related_entity_ids(self, entity_key: str) -> list[str]:
        """Return additional entities whose state affects a SEM entity."""
        return []

    async def async_after_select_option(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        entity_key: str,
        sem_option: str,
    ) -> None:
        """Apply adapter-specific side effects after a SEM select changes."""

    def get_runtime_diagnostics(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
    ) -> dict[str, Any]:
        """Return live warnings about mismatched physical source values."""
        return {}

    @abstractmethod
    async def async_execute(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
        command: str,
        data: dict[str, Any],
    ) -> None:
        """Execute a system-specific command requested through SEM."""
        raise NotImplementedError(
            f"Adapter {self.system_id} does not support {command}"
        )
