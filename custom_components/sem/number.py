"""SEM number entities mirrored to all physical Huawei devices."""

import asyncio

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .systems.aggregation import value_fits_number_metadata
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
    """A stable SEM number mirrored to every physical battery system."""

    def __init__(self, manager, entity_key: str, name: str, icon: str) -> None:
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
        """Mirror a shared value and the range common to all source entities."""
        states = self.source_states
        attributes = [
            state.attributes for state in states if state is not None
        ]
        metadata = self._manager.get_number_metadata(
            self._entity_key,
            attributes,
        )
        self._attr_native_value = self._manager.get_number_value(
            self._entity_key,
            states,
        )
        self._attr_native_min_value = self._number_attribute(
            metadata, "min_value", 0
        )
        self._attr_native_max_value = self._number_attribute(
            metadata, "max_value", 100
        )
        self._attr_native_step = self._number_attribute(
            metadata, "step", 1
        )
        self._attr_native_unit_of_measurement = metadata.get(
            "unit_of_measurement"
        )
        try:
            self._attr_mode = NumberMode(
                metadata.get("mode", NumberMode.AUTO)
            )
        except ValueError:
            self._attr_mode = NumberMode.AUTO

    @staticmethod
    def _number_attribute(attributes: dict, key: str, default: float) -> float:
        """Read a numeric capability from shared metadata."""
        try:
            return float(attributes.get(key, default))
        except (TypeError, ValueError):
            return default

    async def async_set_native_value(self, value: float) -> None:
        """Validate the requested value against every physical number entity."""
        states = self.source_states
        if not states or any(state is None for state in states):
            raise HomeAssistantError(
                f"Not all source entities are available for {self._entity_key}"
            )

        if len(states) != len(self._source_entity_ids):
            raise HomeAssistantError(
                f"Source list changed while setting {self._entity_key}"
            )

        for entity_id, state in zip(self._source_entity_ids, states):
            assert state is not None
            if not value_fits_number_metadata(value, state.attributes):
                raise HomeAssistantError(
                    f"Value {value} is not supported by source {entity_id}"
                )

        results = await asyncio.gather(
            *(
                self.hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": entity_id, "value": value},
                    blocking=True,
                )
                for entity_id in self._source_entity_ids
            ),
            return_exceptions=True,
        )
        failures = [
            f"{entity_id}: {result}"
            for entity_id, result in zip(self._source_entity_ids, results)
            if isinstance(result, Exception)
        ]
        if failures:
            raise HomeAssistantError(
                "The number could not be applied to every battery: "
                + "; ".join(failures)
            )

    @property
    def available(self) -> bool:
        """Require valid common limits for every physical number entity."""
        states = self.source_states
        attributes = [
            state.attributes for state in states if state is not None
        ]
        return (
            super().available
            and len(attributes) == len(states)
            and bool(self._manager.get_number_metadata(
                self._entity_key, attributes
            ))
        )

    @property
    def extra_state_attributes(self) -> dict:
        """Expose all physical number entities."""
        return {"source_entity_ids": list(self._source_entity_ids)}
