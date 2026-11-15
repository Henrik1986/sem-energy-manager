import logging
from pathlib import Path

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
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

    if entity_registry is not None:
        entities.append(
            SemEntityStatusSensor(entity_registry)
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
        return attributes

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
