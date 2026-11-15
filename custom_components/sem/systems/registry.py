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


@dataclass(frozen=True)
class EntitySource:
    """One physical source entity resolved for a logical SEM entity."""

    entity_id: str
    unique_id: str | None = None
    platform: str | None = None
    device_id: str | None = None
    parent_device_id: str | None = None
    root_device_id: str | None = None
    owner_device_id: str | None = None
    config_entry_id: str | None = None


@dataclass
class EntityResolution:
    """Resolution result for one logical SEM entity."""

    status: EntityStatus = "pending"
    entity_id: str | None = None
    unique_id: str | None = None
    platform: str | None = None
    device_id: str | None = None
    config_entry_id: str | None = None
    matches: list[str] = field(default_factory=list)
    sources: list[EntitySource] = field(default_factory=list)


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
        sources: list[EntitySource] | None = None,
    ) -> None:
        """Store one or more physical sources for a logical SEM entity."""

        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid entity status: {status}")

        system = self._systems[system_id]

        if entity_key not in system.required_entities:
            raise KeyError(
                f"Unknown entity {entity_key} for system {system_id}"
            )

        resolved_sources = list(sources or [])
        first_source = resolved_sources[0] if resolved_sources else None

        if first_source is not None:
            entity_id = entity_id or first_source.entity_id
            unique_id = unique_id or first_source.unique_id
            platform = platform or first_source.platform
            device_id = device_id or first_source.device_id
            config_entry_id = config_entry_id or first_source.config_entry_id

        resolved_matches = list(matches or [])
        if not resolved_matches and resolved_sources:
            resolved_matches = [
                source.entity_id for source in resolved_sources
            ]
        elif not resolved_matches and entity_id is not None:
            resolved_matches = [entity_id]

        system.entities[entity_key] = EntityResolution(
            status=status,
            entity_id=entity_id,
            unique_id=unique_id,
            platform=platform,
            device_id=device_id,
            config_entry_id=config_entry_id,
            matches=resolved_matches,
            sources=resolved_sources,
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

    def get_entity_sources(
        self,
        system_id: str,
        entity_key: str,
    ) -> list[EntitySource]:
        """Return discovered physical sources, including ambiguous matches."""
        system = self._systems.get(system_id)
        if system is None:
            return []

        result = system.entities.get(entity_key)
        if result is None:
            return []

        if result.sources:
            return list(result.sources)

        if result.entity_id is None:
            return []

        return [
            EntitySource(
                entity_id=result.entity_id,
                unique_id=result.unique_id,
                platform=result.platform,
                device_id=result.device_id,
                config_entry_id=result.config_entry_id,
            )
        ]

    def get_entity_ids(
        self,
        system_id: str,
        entity_key: str,
    ) -> list[str]:
        """Return source IDs only after the logical entity is resolved."""
        system = self._systems.get(system_id)
        if system is None:
            return []

        result = system.entities.get(entity_key)
        if result is None or result.status != "found":
            return []

        return [
            source.entity_id
            for source in self.get_entity_sources(system_id, entity_key)
        ]

    def get_entity_device_ids(
        self,
        system_id: str,
        entity_key: str,
    ) -> list[str]:
        """Return unique device IDs for a resolved logical SEM entity."""
        system = self._systems.get(system_id)
        if system is None:
            return []

        result = system.entities.get(entity_key)
        if result is None or result.status != "found":
            return []

        return list(dict.fromkeys(
            source.device_id
            for source in self.get_entity_sources(system_id, entity_key)
            if source.device_id is not None
        ))

    def get_entity_device_id(
        self,
        system_id: str,
        entity_key: str,
    ) -> str | None:
        """Return the first device ID for backwards compatibility."""
        device_ids = self.get_entity_device_ids(system_id, entity_key)
        if device_ids:
            return device_ids[0]

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
        """Return the first entity ID for backwards compatibility."""
        entity_ids = self.get_entity_ids(system_id, entity_key)
        if entity_ids:
            return entity_ids[0]

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
                    "sources": [
                        {
                            "entity_id": source.entity_id,
                            "unique_id": source.unique_id,
                            "platform": source.platform,
                            "device_id": source.device_id,
                            "parent_device_id": source.parent_device_id,
                            "root_device_id": source.root_device_id,
                            "owner_device_id": source.owner_device_id,
                            "config_entry_id": source.config_entry_id,
                        }
                        for source in result.sources
                    ],
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
