"""SEM select entities backed by the active system adapter."""

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
    """A stable SEM select backed by the active system adapter."""

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
        self._attr_options = []
        self._attr_current_option = None

    def _update_from_source(self) -> None:
        """Mirror generic options and current selection."""
        state = self.source_state
        if state is None:
            self._attr_options = []
            self._attr_current_option = None
            return

        source_options = list(state.attributes.get("options", []))
        self._attr_options = self._manager.get_select_options(
            self._entity_key,
            source_options,
        )
        self._attr_current_option = self._manager.to_sem_option(
            self._entity_key,
            state.state,
        )

    async def async_select_option(self, option: str) -> None:
        """Translate a SEM option and forward it to the active source."""
        if not self.available:
            raise HomeAssistantError(
                f"The source entity for {self._entity_key} is unavailable"
            )

        state = self.source_state
        source_options = (
            list(state.attributes.get("options", []))
            if state is not None
            else []
        )
        try:
            source_option = self._manager.to_source_option(
                self._entity_key,
                option,
                source_options,
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

        await self.async_call_source(
            "select_option",
            {"option": source_option},
        )

    @property
    def extra_state_attributes(self) -> dict:
        """Expose the resolved source entity."""
        return {"source_entity_id": self._source_entity_id}
