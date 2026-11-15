"""Manager for SEM energy-system adapters."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError

from .adapter import SemSystemAdapter
from .registry import SemEntityRegistry

_LOGGER = logging.getLogger(__name__)


class SemSystemManager:
    """Select and coordinate the active SEM energy-system adapter."""

    def __init__(
        self,
        hass: HomeAssistant,
        registry: SemEntityRegistry,
    ) -> None:
        self.hass = hass
        self.registry = registry
        self._adapters: dict[str, SemSystemAdapter] = {}
        self._setup_tasks: list[asyncio.Task] = []

    def register_adapter(self, adapter: SemSystemAdapter) -> None:
        """Register a supported energy-system adapter."""
        if adapter.system_id in self._adapters:
            raise ValueError(f"Duplicate SEM adapter: {adapter.system_id}")
        self._adapters[adapter.system_id] = adapter

    def async_start(self) -> None:
        """Start adapter discovery without blocking integration setup."""
        for adapter in self._adapters.values():
            task = self.hass.async_create_task(
                adapter.async_setup(self.hass, self.registry)
            )
            self._setup_tasks.append(task)

    async def async_unload(self) -> None:
        """Cancel outstanding adapter setup tasks."""
        for task in self._setup_tasks:
            if not task.done():
                task.cancel()
        if self._setup_tasks:
            await asyncio.gather(*self._setup_tasks, return_exceptions=True)
        self._setup_tasks.clear()

    @property
    def active_adapter(self) -> SemSystemAdapter | None:
        """Return the single configured adapter, if unambiguous."""
        system_id = self.registry.get_active_system_id()
        return self._adapters.get(system_id) if system_id is not None else None

    def get_entity_ids(self, entity_key: str) -> list[str]:
        """Return all active source entity IDs for a logical SEM entity."""
        adapter = self.active_adapter
        if adapter is None:
            return []
        return self.registry.get_entity_ids(adapter.system_id, entity_key)

    def get_entity_id(self, entity_key: str) -> str | None:
        """Return the first source entity ID for backwards compatibility."""
        entity_ids = self.get_entity_ids(entity_key)
        return entity_ids[0] if entity_ids else None

    def get_entity_device_ids(self, entity_key: str) -> list[str]:
        """Return device IDs for all physical sources."""
        adapter = self.active_adapter
        if adapter is None:
            return []
        return self.registry.get_entity_device_ids(adapter.system_id, entity_key)

    def get_entity_device_id(self, entity_key: str) -> str | None:
        """Return the first source device ID for backwards compatibility."""
        device_ids = self.get_entity_device_ids(entity_key)
        return device_ids[0] if device_ids else None

    def get_entity_states(self, entity_key: str) -> list[State | None]:
        """Read the state for every resolved physical source."""
        return [
            self.hass.states.get(entity_id)
            for entity_id in self.get_entity_ids(entity_key)
        ]

    def get_sensor_value(self, entity_key: str, source_states=None):
        """Return an aggregated sensor value from every physical source."""
        adapter = self.active_adapter
        if adapter is None:
            return None
        states = (
            list(source_states)
            if source_states is not None
            else self.get_entity_states(entity_key)
        )
        return adapter.get_sensor_value(entity_key, states)

    def get_sensor_metadata(
        self,
        entity_key: str,
        source_attributes: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return common measurement metadata from all source entities."""
        adapter = self.active_adapter
        if adapter is None:
            return {}
        if source_attributes is None:
            states = self.get_entity_states(entity_key)
            source_attributes = [
                state.attributes for state in states if state is not None
            ]
        return adapter.get_sensor_metadata(entity_key, source_attributes)

    def get_number_value(
        self,
        entity_key: str,
        source_states: Sequence[State | None] | None = None,
    ) -> float | None:
        """Return a shared number value from all physical sources."""
        adapter = self.active_adapter
        if adapter is None:
            return None
        states = (
            list(source_states)
            if source_states is not None
            else self.get_entity_states(entity_key)
        )
        return adapter.get_number_value(entity_key, states)

    def get_number_metadata(
        self,
        entity_key: str,
        source_attributes: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return the common number range across all physical sources."""
        adapter = self.active_adapter
        if adapter is None:
            return {}
        if source_attributes is None:
            states = self.get_entity_states(entity_key)
            source_attributes = [
                state.attributes for state in states if state is not None
            ]
        return adapter.get_number_metadata(entity_key, source_attributes)

    def get_select_options(
        self,
        entity_key: str,
        source_options: Sequence[Sequence[str]],
    ) -> list[str]:
        """Return SEM select options supported by all physical sources."""
        adapter = self.active_adapter
        return (
            adapter.get_sem_options(entity_key, source_options)
            if adapter is not None
            else []
        )

    def to_source_options(
        self,
        entity_key: str,
        sem_option: str,
        source_options: Sequence[Sequence[str]],
    ) -> list[str]:
        """Translate one SEM option for every physical source."""
        adapter = self.active_adapter
        if adapter is None:
            raise ValueError("No unambiguous SEM energy system is active")
        return adapter.to_source_options(
            entity_key,
            sem_option,
            source_options,
        )

    def to_sem_option(
        self,
        entity_key: str,
        source_options: Sequence[str | None],
    ) -> str | None:
        """Translate source options and require all sources to agree."""
        adapter = self.active_adapter
        if adapter is None:
            return None
        return adapter.to_sem_option(entity_key, source_options)

    def get_runtime_diagnostics(self) -> dict[str, Any]:
        """Return live adapter warnings for the SEM status sensor."""
        adapter = self.active_adapter
        if adapter is None:
            return {}
        return adapter.get_runtime_diagnostics(self.hass, self.registry)

    async def async_execute(
        self,
        command: str,
        data: dict[str, Any],
    ) -> None:
        """Execute a generic SEM command through the active adapter."""
        adapter = self.active_adapter
        if adapter is None:
            raise HomeAssistantError(
                "No unambiguous SEM energy-system adapter is active"
            )
        _LOGGER.debug(
            "Executing SEM command %s through adapter %s",
            command,
            adapter.system_id,
        )
        await adapter.async_execute(self.hass, self.registry, command, data)
