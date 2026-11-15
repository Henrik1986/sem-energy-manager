"""SEM select entities backed by resolved Huawei entities."""

from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .systems.proxy import SELECT_ENTITY_DEFINITIONS, SemHuaweiProxyMixin
from .systems.registry import SemEntityRegistry


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up SEM select entities."""
    registry = hass.data[DOMAIN]["entity_registries"].get(entry.entry_id)
    if registry is None:
        return

    async_add_entities(
        SemHuaweiSelect(registry, *definition)
        for definition in SELECT_ENTITY_DEFINITIONS
    )


class SemHuaweiSelect(SemHuaweiProxyMixin, SelectEntity):
    """A stable SEM select backed by a resolved Huawei select."""

    def __init__(
        self,
        registry: SemEntityRegistry,
        entity_key: str,
        name: str,
        icon: str,
    ) -> None:
        SemHuaweiProxyMixin.__init__(
            self,
            registry,
            entity_key=entity_key,
            name=name,
            icon=icon,
        )
        self._attr_options = []
        self._attr_current_option = None

    def _update_from_source(self) -> None:
        """Mirror available options and current selection."""
        state = self.source_state
        if state is None:
            self._attr_options = []
            self._attr_current_option = None
            return

        self._attr_options = list(state.attributes.get("options", []))
        self._attr_current_option = state.state

    async def async_select_option(self, option: str) -> None:
        """Forward an option selection to the Huawei source."""
        if not self.available:
            raise HomeAssistantError(
                "The Huawei battery working mode entity is unavailable"
            )

        await self.async_call_source(
            "select_option",
            {"option": option},
        )

    @property
    def extra_state_attributes(self) -> dict:
        """Expose the resolved source entity."""
        return {"source_entity_id": self._source_entity_id}
