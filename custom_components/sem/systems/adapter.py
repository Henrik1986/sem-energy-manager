"""Base contract for SEM energy-system adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from homeassistant.core import HomeAssistant

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
