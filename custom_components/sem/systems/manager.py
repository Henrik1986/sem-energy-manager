"""Manager for SEM energy-system adapters."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant

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
            raise ValueError(
                f"Duplicate SEM adapter: {adapter.system_id}"
            )
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
            await asyncio.gather(
                *self._setup_tasks,
                return_exceptions=True,
            )

        self._setup_tasks.clear()

    @property
    def active_adapter(self) -> SemSystemAdapter | None:
        """Return the single configured adapter, if unambiguous."""
        system_id = self.registry.get_active_system_id()
        if system_id is None:
            return None
        return self._adapters.get(system_id)

    def get_entity_id(self, entity_key: str) -> str | None:
        """Return the active adapter's entity for a logical SEM key."""
        adapter = self.active_adapter
        if adapter is None:
            return None
        return self.registry.get_entity_id(
            adapter.system_id,
            entity_key,
        )

    def get_entity_device_id(self, entity_key: str) -> str | None:
        """Return the device ID for a logical SEM key."""
        adapter = self.active_adapter
        if adapter is None:
            return None
        return self.registry.get_entity_device_id(
            adapter.system_id,
            entity_key,
        )

    def get_select_options(
        self,
        entity_key: str,
        source_options: list[str],
    ) -> list[str]:
        """Return generic SEM options for a select entity."""
        adapter = self.active_adapter
        if adapter is None:
            return []
        return adapter.get_sem_options(entity_key, source_options)

    def to_source_option(
        self,
        entity_key: str,
        sem_option: str,
        source_options: list[str],
    ) -> str:
        """Translate a SEM select option for the active adapter."""
        adapter = self.active_adapter
        if adapter is None:
            raise ValueError("No unambiguous SEM energy system is active")
        return adapter.to_source_option(
            entity_key,
            sem_option,
            source_options,
        )

    def to_sem_option(
        self,
        entity_key: str,
        source_option: str,
    ) -> str | None:
        """Translate a source select option into the SEM option."""
        adapter = self.active_adapter
        if adapter is None:
            return None
        return adapter.to_sem_option(entity_key, source_option)

    async def async_execute(
        self,
        command: str,
        data: dict[str, Any],
    ) -> None:
        """Execute a generic SEM command through the active adapter."""
        adapter = self.active_adapter
        if adapter is None:
            raise RuntimeError(
                "No unambiguous SEM energy-system adapter is active"
            )

        _LOGGER.debug(
            "Executing SEM command %s through adapter %s",
            command,
            adapter.system_id,
        )
        await adapter.async_execute(
            self.hass,
            self.registry,
            command,
            data,
        )
