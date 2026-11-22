"""SEM sensors for installation, diagnostics and aggregated energy values."""

import logging
from pathlib import Path

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN
from .systems.proxy import SENSOR_ENTITY_DEFINITIONS, SemEntityProxyMixin
from .systems.registry import SemEntityRegistry

_LOGGER = logging.getLogger(__name__)


def _migrate_entity_status_id(hass: HomeAssistant) -> None:
    """Rename the default Entity Status ID without overriding user changes."""
    registry = er.async_get(hass)
    current_entity_id = registry.async_get_entity_id(
        "sensor",
        DOMAIN,
        "sem_entity_status",
    )
    if current_entity_id != "sensor.entity_status":
        return

    target_entity_id = "sensor.sem_entity_status"
    if registry.async_get(target_entity_id) is not None:
        _LOGGER.warning(
            "SEM: Could not rename %s to %s because the target ID is already "
            "in use. Rename the existing entity manually if desired.",
            current_entity_id,
            target_entity_id,
        )
        return

    registry.async_update_entity(
        current_entity_id,
        new_entity_id=target_entity_id,
    )
    _LOGGER.info(
        "SEM: Renamed entity %s to %s",
        current_entity_id,
        target_entity_id,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up SEM sensors."""
    package_path = hass.config.path("sem")
    info = await hass.async_add_executor_job(check_installation, package_path)

    entities = []
    entity_registry = hass.data[DOMAIN]["entity_registries"].get(entry.entry_id)
    system_manager = hass.data[DOMAIN]["system_managers"].get(entry.entry_id)

    if entity_registry is not None and system_manager is not None:
        _migrate_entity_status_id(hass)
        entities.append(SemEntityStatusSensor(entity_registry, system_manager))
    else:
        _LOGGER.error("SEM entity registry not found")

    entities.append(SemInstallationStatusSensor(info))

    if system_manager is not None:
        entities.extend(
            SemSystemSensor(system_manager, *definition)
            for definition in SENSOR_ENTITY_DEFINITIONS
        )

    async_add_entities(entities)


def check_installation(package_path: str) -> dict:
    """Check whether the SEM YAML package exists."""
    path = Path(package_path)
    if not path.is_dir():
        return {
            "status": "Väntar",
            "message": "SEM-paketets katalog saknas.",
            "yaml_files": [],
        }

    yaml_files = sorted(
        str(file.relative_to(path))
        for file in path.rglob("*")
        if file.is_file() and file.suffix.lower() in (".yaml", ".yml")
    )
    if not yaml_files:
        return {
            "status": "Fel",
            "message": "Inga YAML-filer hittades.",
            "yaml_files": [],
        }

    return {
        "status": "OK",
        "message": "SEM-paketets YAML-filer hittades.",
        "yaml_files": yaml_files,
    }


class SemInstallationStatusSensor(SensorEntity):
    """Report the state of the SEM package installation."""

    _attr_has_entity_name = True
    _attr_name = "Installation Status"
    _attr_icon = "mdi:check-circle-outline"

    def __init__(self, info: dict) -> None:
        self._attr_unique_id = "sem_installation_status"
        self._attr_native_value = info["status"]
        self._attr_extra_state_attributes = {
            "message": info["message"],
            "yaml_files": info["yaml_files"],
            "yaml_file_count": len(info["yaml_files"]),
        }


class SemEntityStatusSensor(SensorEntity):
    """Expose resolver status and live multi-device diagnostics."""

    _attr_has_entity_name = True
    _attr_name = "Entity Status"
    _attr_icon = "mdi:database-check"

    def __init__(self, registry: SemEntityRegistry, manager) -> None:
        self._registry = registry
        self._manager = manager
        self._attr_unique_id = "sem_entity_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "huawei_entity_bridge")},
            name="SEM Energy System",
            manufacturer="SEM",
            model="Energy system adapter",
        )
        self._source_entity_ids: list[str] = []
        self._source_unsub = None
        self._last_logged_mismatch_signature = None

    @property
    def native_value(self) -> str:
        setup_errors = self._manager.get_setup_errors()
        diagnostics = self._manager.get_runtime_diagnostics()
        runtime_errors = self._manager.get_runtime_errors(diagnostics)
        if (
            setup_errors
            or runtime_errors
            or self._registry.get_status() != "OK"
        ):
            return "ERROR - Check log"
        return "OK"

    @property
    def extra_state_attributes(self) -> dict:
        attributes = self._registry.as_dict()
        attributes.pop("status", None)

        setup_errors = self._manager.get_setup_errors()
        diagnostics = self._manager.get_runtime_diagnostics()
        runtime_errors = self._manager.get_runtime_errors(diagnostics)
        runtime_warnings = self._manager.get_runtime_warnings(diagnostics)
        attributes["setup_errors"] = setup_errors
        attributes["runtime_diagnostics"] = diagnostics
        attributes["runtime_errors"] = runtime_errors
        attributes["runtime_warnings"] = runtime_warnings

        if setup_errors:
            attributes["status_message"] = setup_errors[0]["message"]
            attributes["action_required"] = setup_errors[0]["action"]
        elif runtime_errors:
            attributes["status_message"] = runtime_errors[0]["message"]
            attributes["action_required"] = runtime_errors[0]["action"]
        elif runtime_warnings:
            attributes["status_message"] = runtime_warnings[0]["message"]
            attributes["action_required"] = runtime_warnings[0]["action"]
        elif self._registry.get_status() == "OK":
            attributes["status_message"] = (
                "Alla obligatoriska entiteter hittades. SEM är redo."
            )
            attributes["action_required"] = "Ingen åtgärd behövs."
        else:
            attributes["status_message"] = (
                "SEM hittar inte alla obligatoriska entiteter."
            )
            attributes["action_required"] = (
                "Kontrollera attributen 'missing_entities' och "
                "'ambiguous_entities' för att se vilka entiteter som behöver "
                "åtgärdas."
            )

        return attributes

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._registry.add_listener(self._handle_registry_update)
        )
        self.async_on_remove(self._remove_source_listener)
        self._refresh_source_listener()
        self._log_runtime_mismatches()

    @callback
    def _log_runtime_mismatches(self) -> None:
        """Log each new or changed mismatch once, avoiding repeated warnings."""
        diagnostics = self._manager.get_runtime_diagnostics()
        errors = self._manager.get_runtime_errors(diagnostics)
        warnings = self._manager.get_runtime_warnings(diagnostics)
        messages = errors + warnings
        if not messages:
            self._last_logged_mismatch_signature = None
            return

        signature = tuple(
            (
                error.get("code"),
                error.get("message"),
                tuple(
                    (source.get("entity_id"), source.get("value"))
                    for source in error.get("sources", [])
                ),
            )
            for error in messages
        )
        if signature == self._last_logged_mismatch_signature:
            return

        self._last_logged_mismatch_signature = signature
        for error in messages:
            _LOGGER.warning(
                "SEM: %s Åtgärd: %s",
                error["message"],
                error["action"],
            )

    @callback
    def _refresh_source_listener(self) -> None:
        """Subscribe to state changes for every resolved physical source."""
        source_ids = []
        for system in self._registry.as_dict().get("systems", {}).values():
            for entity in system.get("entities", {}).values():
                source_ids.extend(
                    source.get("entity_id")
                    for source in entity.get("sources", [])
                    if source.get("entity_id")
                )

        for entity_key in (
            self._manager.capabilities.entities
            if self._manager.capabilities is not None
            else ()
        ):
            source_ids.extend(
                self._manager.get_related_entity_ids(entity_key)
            )

        source_ids = sorted(set(source_ids))
        if source_ids == self._source_entity_ids:
            return

        self._remove_source_listener()
        self._source_entity_ids = source_ids
        if source_ids:
            self._source_unsub = async_track_state_change_event(
                self.hass,
                source_ids,
                self._handle_source_state_change,
            )

    @callback
    def _remove_source_listener(self) -> None:
        """Remove physical source state subscriptions."""
        if self._source_unsub is not None:
            self._source_unsub()
            self._source_unsub = None

    @callback
    def _handle_source_state_change(self, event) -> None:
        """Refresh and log dynamic mismatch diagnostics after a source change."""
        self._log_runtime_mismatches()
        self.async_write_ha_state()

    @callback
    def _handle_registry_update(self) -> None:
        self._refresh_source_listener()
        self._log_runtime_mismatches()
        self.async_write_ha_state()


class SemSystemSensor(SemEntityProxyMixin, SensorEntity):
    """A stable SEM sensor backed by aggregated physical source entities."""

    def __init__(self, manager, entity_key: str, name: str, icon: str) -> None:
        SemEntityProxyMixin.__init__(
            self,
            manager,
            entity_key=entity_key,
            name=name,
            icon=icon,
        )

    @property
    def available(self) -> bool:
        """Require the adapter to declare this sensor as readable."""
        if self._entity_key == "battery_rated_capacity":
            return (
                self._manager.is_entity_readable(self._entity_key)
                and self._manager.get_sensor_value(
                    self._entity_key,
                    self.source_states,
                ) is not None
            )
        return (
            self._manager.is_entity_readable(self._entity_key)
            and super().available
        )

    @property
    def native_value(self):
        """Return the normalized plant-level value."""
        return self._manager.get_sensor_value(
            self._entity_key,
            self.source_states,
        )

    def _update_from_source(self) -> None:
        """Mirror common measurement metadata from all physical sources."""
        attributes = [
            state.attributes
            for state in self.source_states
            if state is not None
        ]
        metadata = self._manager.get_sensor_metadata(
            self._entity_key,
            attributes,
        )

        device_class = metadata.get("device_class")
        state_class = metadata.get("state_class")
        try:
            self._attr_device_class = (
                SensorDeviceClass(device_class) if device_class else None
            )
        except ValueError:
            self._attr_device_class = None

        try:
            self._attr_state_class = (
                SensorStateClass(state_class) if state_class else None
            )
        except ValueError:
            self._attr_state_class = None

        self._attr_native_unit_of_measurement = metadata.get(
            "unit_of_measurement"
        )
        self._attr_suggested_display_precision = metadata.get(
            "suggested_display_precision"
        )

    @property
    def extra_state_attributes(self) -> dict:
        """Expose every physical source entity used by this sensor."""
        return {"source_entity_ids": list(self._source_entity_ids)}
