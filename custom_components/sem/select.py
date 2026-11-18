"""SEM select entities backed by all physical source devices."""

import asyncio

from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .systems.proxy import SELECT_ENTITY_DEFINITIONS, SemEntityProxyMixin


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up SEM select entities."""
    manager = hass.data[DOMAIN]["system_managers"].get(entry.entry_id)
    if manager is None:
        return
    async_add_entities(
        SemSystemSelect(manager, *definition)
        for definition in SELECT_ENTITY_DEFINITIONS
    )


class SemSystemSelect(SemEntityProxyMixin, SelectEntity):
    """A stable SEM select mirrored to every physical battery."""

    def __init__(self, manager, entity_key: str, name: str, icon: str) -> None:
        SemEntityProxyMixin.__init__(
            self,
            manager,
            entity_key=entity_key,
            name=name,
            icon=icon,
        )
        self._attr_options = []
        self._attr_current_option = None

    def _update_from_source(self) -> None:
        """Expose only shared options and a shared current value."""
        states = self.source_states
        if not states or any(state is None for state in states):
            self._attr_options = []
            self._attr_current_option = None
            return

        source_options = [
            list(state.attributes.get("options", []))
            for state in states
            if state is not None
        ]
        self._attr_options = self._manager.get_select_options(
            self._entity_key,
            source_options,
        )
        source_values = [
            state.state if state is not None else None
            for state in states
        ]
        self._attr_current_option = self._manager.to_sem_option(
            self._entity_key,
            source_values,
        )

    @property
    def available(self) -> bool:
        """Require all source devices and a common selectable option."""
        return super().available and bool(self._attr_options)

    async def async_select_option(self, option: str) -> None:
        """Validate and mirror a selected option to every battery."""
        if not self.available:
            raise HomeAssistantError(
                f"The source entities for {self._entity_key} are unavailable"
            )

        states = self.source_states
        if not states or any(state is None for state in states):
            raise HomeAssistantError(
                f"Not all source entities are available for {self._entity_key}"
            )

        source_options = [
            list(state.attributes.get("options", []))
            for state in states
            if state is not None
        ]
        try:
            translated_options = self._manager.to_source_options(
                self._entity_key,
                option,
                source_options,
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

        entity_ids = list(self._source_entity_ids)
        if len(entity_ids) != len(translated_options):
            raise HomeAssistantError(
                f"Source list changed while setting {self._entity_key}"
            )

        results = await asyncio.gather(
            *(
                self.hass.services.async_call(
                    "select",
                    "select_option",
                    {"entity_id": entity_id, "option": source_option},
                    blocking=True,
                )
                for entity_id, source_option in zip(
                    entity_ids, translated_options
                )
            ),
            return_exceptions=True,
        )
        failures = [
            f"{entity_id}: {result}"
            for entity_id, result in zip(entity_ids, results)
            if isinstance(result, Exception)
        ]
        if failures:
            raise HomeAssistantError(
                "The selection could not be applied to every battery: "
                + "; ".join(failures)
            )

    @property
    def extra_state_attributes(self) -> dict:
        """Expose every physical select entity."""
        return {"source_entity_ids": list(self._source_entity_ids)}
