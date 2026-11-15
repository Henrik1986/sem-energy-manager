"""SEM number entities backed by the active system adapter."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .systems.proxy import NUMBER_ENTITY_DEFINITIONS, SemEntityProxyMixin


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up SEM number entities."""
    manager = hass.data[DOMAIN]["system_managers"].get(entry.entry_id)
    if manager is None:
        return

    async_add_entities(
        SemSystemNumber(manager, *definition)
        for definition in NUMBER_ENTITY_DEFINITIONS
    )


class SemSystemNumber(SemEntityProxyMixin, NumberEntity):
    """A stable SEM number backed by the active system adapter."""

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
        self._attr_native_value = None
        self._attr_native_min_value = 0
        self._attr_native_max_value = 100
        self._attr_native_step = 1
        self._attr_native_unit_of_measurement = None
        self._attr_mode = NumberMode.AUTO

    def _update_from_source(self) -> None:
        """Mirror the value and limits from the active source."""
        state = self.source_state
        if state is None:
            self._attr_native_value = None
            return

        try:
            self._attr_native_value = float(state.state)
        except (TypeError, ValueError):
            self._attr_native_value = None

        attributes = state.attributes
        self._attr_native_min_value = self._number_attribute(
            attributes, "min", 0
        )
        self._attr_native_max_value = self._number_attribute(
            attributes, "max", 100
        )
        self._attr_native_step = self._number_attribute(
            attributes, "step", 1
        )
        self._attr_native_unit_of_measurement = attributes.get(
            "unit_of_measurement"
        )

        try:
            self._attr_mode = NumberMode(
                attributes.get("mode", NumberMode.AUTO)
            )
        except ValueError:
            self._attr_mode = NumberMode.AUTO

    @staticmethod
    def _number_attribute(
        attributes: dict,
        key: str,
        default: float,
    ) -> float:
        """Read a numeric capability from the source state."""
        try:
            return float(attributes.get(key, default))
        except (TypeError, ValueError):
            return default

    async def async_set_native_value(self, value: float) -> None:
        """Forward a value change to the active source."""
        if not self.available:
            raise HomeAssistantError(
                f"The source entity for {self._entity_key} is unavailable"
            )

        await self.async_call_source(
            "set_value",
            {"value": value},
        )

    @property
    def extra_state_attributes(self) -> dict:
        """Expose the resolved source entity."""
        return {"source_entity_id": self._source_entity_id}
