"""Provider-independent capability definitions for SEM systems."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class EntityCapability:
    """Read and write capabilities for one logical SEM entity."""

    required: bool = False
    readable: bool = True
    writable: bool = False

    def as_dict(self) -> dict[str, bool]:
        """Return a serializable capability description."""
        return {
            "required": self.required,
            "readable": self.readable,
            "writable": self.writable,
        }


@dataclass(frozen=True, slots=True)
class SystemCapabilities:
    """Capabilities provided by one energy-system adapter."""

    entities: Mapping[str, EntityCapability] = field(default_factory=dict)
    commands: frozenset[str] = frozenset()

    @property
    def required_entity_keys(self) -> tuple[str, ...]:
        """Return entity keys required for the system to be operational."""
        return tuple(
            key for key, capability in self.entities.items()
            if capability.required
        )

    def has_entity(self, entity_key: str) -> bool:
        """Return whether the adapter exposes a logical entity."""
        return entity_key in self.entities

    def can_read_entity(self, entity_key: str) -> bool:
        """Return whether an entity provides readable data."""
        capability = self.entities.get(entity_key)
        return capability is not None and capability.readable

    def can_write_entity(self, entity_key: str) -> bool:
        """Return whether an entity supports control writes."""
        capability = self.entities.get(entity_key)
        return capability is not None and capability.writable

    def supports_command(self, command: str) -> bool:
        """Return whether the adapter implements a SEM command."""
        return command in self.commands

    def as_dict(self) -> dict:
        """Return capabilities in a JSON-compatible form."""
        return {
            "entities": {
                key: capability.as_dict()
                for key, capability in self.entities.items()
            },
            "commands": sorted(self.commands),
        }
