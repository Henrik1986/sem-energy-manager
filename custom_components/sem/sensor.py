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
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN
from .systems.proxy import SENSOR_ENTITY_DEFINITIONS, SemEntityProxyMixin
from .systems.registry import SemEntityRegistry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up SEM sensors."""
    package_path = hass.config.path("sem")
    info = await hass.async_add_executor_job(check_installation, package_path)

    entities = [SemInstallationStatusSensor(info)]
    entity_registry = hass.data[DOMAIN]["entity_registries"].get(entry.entry_id)
    system_manager = hass.data[DOMAIN]["system_managers"].get(entry.entry_id)

    if entity_registry is not None and system_manager is not None:
        entities.append(SemEntityStatusSensor(entity_registry, system_manager))
        entities.extend(
            SemSystemSensor(system_manager, *definition)
            for definition in SENSOR_ENTITY_DEFINITIONS
        )
    else:
        _LOGGER.error("SEM entity registry not found")

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
        self._source_entity_ids: list[str] = []
        self._source_unsub = None

    @property
    def native_value(self) -> str:
        diagnostics = self._manager.get_runtime_diagnostics()
        if diagnostics.get("setting_mismatches"):
            return "FEL"
        return self._registry.get_status()

    @property
    def extra_state_attributes(self) -> dict:
        attributes = self._registry.as_dict()
        attributes.pop("status", None)

        setup_errors = self._get_setup_errors(attributes)
        diagnostics = self._manager.get_runtime_diagnostics()
        attributes["setup_errors"] = setup_errors
        attributes["runtime_diagnostics"] = diagnostics

        if setup_errors:
            attributes["status_message"] = setup_errors[0]["message"]
            attributes["action_required"] = setup_errors[0]["action"]
        elif diagnostics.get("setting_mismatches"):
            attributes["status_message"] = (
                "En eller flera inställningar skiljer sig mellan "
                "batterienheterna. SEM kan därför inte visa ett gemensamt värde."
            )
            attributes["action_required"] = (
                "Ändra inställningarna via SEM eller kontrollera i Huawei Solar "
                "att samtliga batterienheter har samma värde."
            )
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

    def _get_setup_errors(self, attributes: dict) -> list[dict]:
        """Build actionable setup errors for required system entities."""
        errors = []
        huawei = attributes.get("systems", {}).get("huawei", {})
        entities = huawei.get("entities", {})
        capacity = entities.get("battery_rated_capacity", {})
        status = capacity.get("status")

        if status not in {"disabled", "missing"}:
            return errors

        entity_registry = er.async_get(self.hass)
        device_registry = dr.async_get(self.hass)
        capacity_sources = capacity.get("sources", [])
        soc_sources = entities.get("battery_state_of_capacity", {}).get(
            "sources", []
        )

        def describe_source(source: dict, issue: str) -> dict:
            entity_id = source.get("entity_id")
            device_id = source.get("device_id")
            entity_entry = (
                entity_registry.async_get(entity_id) if entity_id else None
            )
            device_entry = (
                device_registry.async_get(device_id) if device_id else None
            )
            return {
                "device_name": (
                    device_entry.name_by_user or device_entry.name
                    if device_entry else "Okänd batterienhet"
                ),
                "device_id": device_id,
                "entity_id": entity_id,
                "entity_name": (
                    entity_entry.name or entity_entry.original_name
                    if entity_entry else "Rated Capacity"
                ),
                "issue": issue,
            }

        affected_devices = []
        if status == "disabled":
            affected_devices = [
                describe_source(source, "Entiteten är inaktiverad")
                for source in capacity_sources
            ]
            message = (
                "SEM kan inte beräkna batteriernas totala kapacitet eller "
                "kapacitetsviktade SOC eftersom Huawei-entiteten "
                "'Rated Capacity' är inaktiverad på en eller flera batterienheter."
            )
        else:
            capacity_device_ids = {
                source.get("device_id")
                for source in capacity_sources
                if source.get("device_id")
            }
            affected_devices = [
                describe_source(source, "Rated Capacity hittades inte")
                for source in soc_sources
                if source.get("device_id") not in capacity_device_ids
            ]
            message = (
                "SEM hittar inte Huawei-entiteten 'Rated Capacity' för alla "
                "identifierade batterienheter. Total batterikapacitet och "
                "kapacitetsviktad SOC kan därför inte beräknas korrekt."
            )

        action = (
            "Aktivera 'Rated Capacity' i Huawei Solar för varje batterienhet "
            "och ladda därefter om SEM-integrationen. Home Assistant behöver "
            "inte startas om."
        )

        return [{
            "code": (
                "huawei_battery_capacity_disabled"
                if status == "disabled"
                else "huawei_battery_capacity_missing"
            ),
            "message": message,
            "action": action,
            "steps": [
                "Öppna Inställningar → Enheter och tjänster → Enheter.",
                "Öppna varje Huawei-batterienhet (Connected Energy Storage).",
                "Under Entiteter aktiverar du 'Rated Capacity'. Upprepa för "
                "samtliga batterienheter i anläggningen.",
                "Öppna SEM-integrationen och välj Ladda om.",
            ],
            "affected_devices": affected_devices,
            "entities": capacity.get("matches", []),
        }]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._registry.add_listener(self._handle_registry_update)
        )
        self.async_on_remove(self._remove_source_listener)
        self._refresh_source_listener()

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
        """Refresh dynamic mismatch diagnostics after a physical change."""
        self.async_write_ha_state()

    @callback
    def _handle_registry_update(self) -> None:
        self._refresh_source_listener()
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
