"""Base contract for SEM energy-system adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
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
        source_state: State | None,
    ) -> float | str | None:
        """Return a normalized sensor value for a SEM logical entity."""
        if source_state is None or source_state.state in (
            "unknown",
            "unavailable",
        ):
            return None

        try:
            return float(source_state.state)
        except (TypeError, ValueError):
            return source_state.state

    def get_sensor_metadata(
        self,
        entity_key: str,
        source_attributes: dict[str, Any],
    ) -> dict[str, Any]:
        """Return standardized measurement metadata for a SEM sensor."""
        return {
            "device_class": source_attributes.get("device_class"),
            "state_class": source_attributes.get("state_class"),
            "unit_of_measurement": source_attributes.get(
                "unit_of_measurement"
            ),
            "suggested_display_precision": source_attributes.get(
                "suggested_display_precision"
            ),
        }

    def get_number_value(
        self,
        entity_key: str,
        source_state: State | None,
    ) -> float | None:
        """Return a normalized numeric value for a SEM number entity."""
        if source_state is None or source_state.state in (
            "unknown",
            "unavailable",
        ):
            return None

        try:
            return float(source_state.state)
        except (TypeError, ValueError):
            return None

    def get_number_metadata(
        self,
        entity_key: str,
        source_attributes: dict[str, Any],
    ) -> dict[str, Any]:
        """Return standardized capabilities for a SEM number entity."""
        return {
            "min_value": source_attributes.get("min", 0),
            "max_value": source_attributes.get("max", 100),
            "step": source_attributes.get("step", 1),
            "unit_of_measurement": source_attributes.get(
                "unit_of_measurement"
            ),
            "mode": source_attributes.get("mode", "auto"),
        }

    def get_sem_options(
        self,
        entity_key: str,
        source_options: list[str],
    ) -> list[str]:
        """Translate source options into options exposed by SEM."""
        return source_options

    def to_source_option(
        self,
        entity_key: str,
        sem_option: str,
        source_options: list[str],
    ) -> str:
        """Translate a SEM option into the source integration option."""
        if sem_option not in source_options:
            raise ValueError(
                f"Unsupported option {sem_option!r} for {entity_key}"
            )
        return sem_option

    def to_sem_option(
        self,
        entity_key: str,
        source_option: str,
    ) -> str | None:
        """Translate a source integration option into a SEM option."""
        return source_option

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
