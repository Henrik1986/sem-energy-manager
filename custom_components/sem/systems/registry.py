"""Central entity registry for SEM."""

from dataclasses import dataclass, field
from typing import Callable, Literal


EntityStatus = Literal[
    "pending",
    "found",
    "missing",
    "disabled",
    "ambiguous",
]

VALID_STATUSES = {
    "pending",
    "found",
    "missing",
    "disabled",
    "ambiguous",
}


@dataclass
class EntityResolution:
    """Resolution result for one entity."""

    status: EntityStatus = "pending"
    entity_id: str | None = None
    unique_id: str | None = None
    platform: str | None = None
    device_id: str | None = None
    config_entry_id: str | None = None
    matches: list[str] = field(default_factory=list)


@dataclass
class SystemResolution:
    """Resolution information for one energy system."""

    configured: bool
    required_entities: tuple[str, ...]
    entities: dict[str, EntityResolution] = field(default_factory=dict)


class SemEntityRegistry:
    """Central registry containing entities used by SEM."""

    def __init__(self) -> None:
        self._systems: dict[str, SystemResolution] = {}
        self._listeners: set[Callable[[], None]] = set()

    def register_system(
        self,
        system_id: str,
        *,
        configured: bool,
        required_entities: list[str],
    ) -> None:
        """Register a system and its required entities."""

        previous = self._systems.get(system_id)
        required = tuple(required_entities)

        entities = {
            key: (
                previous.entities[key]
                if previous and key in previous.entities
                else EntityResolution()
            )
            for key in required
        }

        self._systems[system_id] = SystemResolution(
            configured=configured,
            required_entities=required,
            entities=entities if configured else {},
        )

        self._notify()

    def set_entity_result(
        self,
        system_id: str,
        entity_key: str,
        *,
        status: EntityStatus,
        entity_id: str | None = None,
        unique_id: str | None = None,
        platform: str | None = None,
        device_id: str | None = None,
        config_entry_id: str | None = None,
        matches: list[str] | None = None,
    ) -> None:
        """Store the resolution result for an entity."""

        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid entity status: {status}")

        system = self._systems[system_id]

        if entity_key not in system.required_entities:
            raise KeyError(
                f"Unknown entity {entity_key} for system {system_id}"
            )

        system.entities[entity_key] = EntityResolution(
            status=status,
            entity_id=entity_id,
            unique_id=unique_id,
            platform=platform,
            device_id=device_id,
            config_entry_id=config_entry_id,
            matches=matches or [],
        )

        self._notify()

    def get_active_system_id(self) -> str | None:
        """Return the only configured system, or None if ambiguous."""
        configured = [
            system_id
            for system_id, system in self._systems.items()
            if system.configured
        ]

        if len(configured) != 1:
            return None

        return configured[0]

    def get_entity_device_id(
        self,
        system_id: str,
        entity_key: str,
    ) -> str | None:
        """Return the device ID for a resolved system entity."""
        system = self._systems.get(system_id)
        if system is None:
            return None

        result = system.entities.get(entity_key)
        if result is None or result.status != "found":
            return None

        return result.device_id

    def get_entity_id(
        self,
        system_id: str,
        entity_key: str,
    ) -> str | None:
        """Return a resolved entity ID for a logical system entity."""
        system = self._systems.get(system_id)
        if system is None:
            return None

        result = system.entities.get(entity_key)
        if result is None or result.status != "found":
            return None

        return result.entity_id

    def get_status(self) -> str:
        """Return the overall SEM entity status."""

        configured = [
            system
            for system in self._systems.values()
            if system.configured
        ]

        if not configured:
            return "FEL"

        for system in configured:
            if not system.required_entities:
                return "FEL"

            if any(
                system.entities.get(key, EntityResolution()).status != "found"
                for key in system.required_entities
            ):
                return "FEL"

        return "OK"

    def as_dict(self) -> dict:
        """Return registry data for sensor attributes."""

        systems = {}
        missing_entities = []
        ambiguous_entities = []

        for system_id, system in self._systems.items():
            entities = {}

            for key in system.required_entities:
                result = system.entities.get(key, EntityResolution())

                entities[key] = {
                    "status": result.status,
                    "entity_id": result.entity_id,
                    "unique_id": result.unique_id,
                    "platform": result.platform,
                    "device_id": result.device_id,
                    "config_entry_id": result.config_entry_id,
                    "matches": result.matches,
                }

                item = {
                    "system": system_id,
                    "entity": key,
                    "status": result.status,
                }

                if result.status == "ambiguous":
                    ambiguous_entities.append({
                        **item,
                        "matches": result.matches,
                    })
                elif result.status != "found" and system.configured:
                    missing_entities.append(item)

            if not system.configured:
                system_status = "not_configured"
            elif all(
                system.entities.get(key, EntityResolution()).status == "found"
                for key in system.required_entities
            ) and system.required_entities:
                system_status = "OK"
            else:
                system_status = "FEL"

            systems[system_id] = {
                "configured": system.configured,
                "status": system_status,
                "entities": entities if system.configured else {},
            }

        return {
            "status": self.get_status(),
            "systems": systems,
            "missing_entities": missing_entities,
            "ambiguous_entities": ambiguous_entities,
        }

    def add_listener(
        self,
        listener: Callable[[], None],
    ) -> Callable[[], None]:
        """Register a callback for registry changes."""

        self._listeners.add(listener)

        def remove_listener() -> None:
            self._listeners.discard(listener)

        return remove_listener

    def _notify(self) -> None:
        """Notify listeners that registry data has changed."""

        for listener in tuple(self._listeners):
            listener()
