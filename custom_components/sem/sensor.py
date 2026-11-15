import logging
from pathlib import Path

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .systems.proxy import SENSOR_ENTITY_DEFINITIONS, SemEntityProxyMixin
from .systems.registry import SemEntityRegistry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:

    package_path = hass.config.path("sem")

    info = await hass.async_add_executor_job(
        check_installation, package_path
    )

    entities = [
        SemInstallationStatusSensor(info)
    ]

    entity_registry = hass.data[DOMAIN]["entity_registries"].get(
        entry.entry_id
    )
    system_manager = hass.data[DOMAIN]["system_managers"].get(
        entry.entry_id
    )

    if entity_registry is not None and system_manager is not None:
        entities.append(
            SemEntityStatusSensor(entity_registry)
        )
        entities.extend(
            SemSystemSensor(system_manager, *definition)
            for definition in SENSOR_ENTITY_DEFINITIONS
        )

    else:
        _LOGGER.error("SEM entity registry not found")

    async_add_entities(entities)


def check_installation(package_path: str) -> dict:
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

    _attr_has_entity_name = True
    _attr_name = "Entity Status"
    _attr_icon = "mdi:database-check"

    def __init__(self, registry: SemEntityRegistry) -> None:
        self._registry = registry
        self._attr_unique_id = "sem_entity_status"

    @property
    def native_value(self) -> str:
        return self._registry.get_status()

    @property
    def extra_state_attributes(self) -> dict:
        attributes = self._registry.as_dict()
        attributes.pop("status", None)
        attributes["setup_errors"] = self._get_setup_errors(attributes)
        return attributes

    @staticmethod
    def _get_setup_errors(attributes: dict) -> list[dict]:
        """Build actionable setup errors for required system entities."""
        errors = []
        huawei = attributes.get("systems", {}).get("huawei", {})
        capacity = huawei.get("entities", {}).get(
            "battery_rated_capacity", {}
        )
        status = capacity.get("status")

        if status == "disabled":
            errors.append({
                "code": "huawei_battery_capacity_disabled",
                "message": (
                    "Huawei: Entiteten för batteriets nominella kapacitet "
                    "är inaktiverad."
                ),
                "action": (
                    "Aktivera entiteten 'Rated Capacity' under varje "
                    "batterienhet i Home Assistant och starta sedan om SEM."
                ),
                "entities": capacity.get("matches", []),
            })
        elif status == "missing":
            errors.append({
                "code": "huawei_battery_capacity_missing",
                "message": (
                    "Huawei: SEM hittar inte kapacitetsentiteten för alla "
                    "identifierade batterisystem."
                ),
                "action": (
                    "Kontrollera att 'Rated Capacity' finns och är aktiverad "
                    "för varje batterienhet i Home Assistant."
                ),
                "entities": capacity.get("matches", []),
            })

        return errors

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        self.async_on_remove(
            self._registry.add_listener(
                self._handle_registry_update
            )
        )

    @callback
    def _handle_registry_update(self) -> None:
        self.async_write_ha_state()



class SemSystemSensor(SemEntityProxyMixin, SensorEntity):
    """A stable SEM sensor backed by the active system adapter."""

    def __init__(
        self,
        manager,
        entity_key: str,
        name: str,
        icon: str,
    ) -> None:
        SemEntityProxyMixin.__init__(
            self,
            manager,
            entity_key=entity_key,
            name=name,
            icon=icon,
        )

    @property
    def native_value(self):
        """Return the normalized value from the active system adapter."""
        return self._manager.get_sensor_value(
            self._entity_key,
            self.source_state,
        )

    def _update_from_source(self) -> None:
        """Mirror normalized measurement metadata from the adapter."""
        state = self.source_state
        attributes = state.attributes if state is not None else {}
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
        """Expose the source entity used for this value."""
        return {"source_entity_id": self._source_entity_id}
