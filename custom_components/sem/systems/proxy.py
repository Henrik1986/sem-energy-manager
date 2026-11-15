"""Shared proxy support for SEM system entities."""

from __future__ import annotations

from typing import Any

from homeassistant.core import State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event

from ..const import DOMAIN
from .registry import SemEntityRegistry


SENSOR_ENTITY_DEFINITIONS = (
    ("battery_state_of_capacity", "SEM Battery SOC", "mdi:battery"),
    ("battery_charge_discharge_power", "SEM Battery Power", "mdi:lightning-bolt"),
    ("battery_day_charge", "SEM Battery Day Charge", "mdi:battery-arrow-up"),
    ("battery_day_discharge", "SEM Battery Day Discharge", "mdi:battery-arrow-down"),
    ("inverter_input_power", "SEM Inverter Input Power", "mdi:solar-power"),
    (
        "power_meter_active_power",
        "SEM Power Meter Active Power",
        "mdi:transmission-tower",
    ),
)

SELECT_ENTITY_DEFINITIONS = (
    ("battery_working_mode", "SEM Battery Working Mode", "mdi:battery-cog"),
)

NUMBER_ENTITY_DEFINITIONS = (
    ("grid_charge_maximum_power", "SEM Grid Charge Maximum Power", "mdi:flash"),
    ("maximum_discharging_power", "SEM Maximum Discharging Power", "mdi:flash-outline"),
    ("end_of_discharge_soc", "SEM End of Discharge SOC", "mdi:battery-low"),
)


class SemEntityProxyMixin:
    """Common behaviour for SEM entities backed by an active adapter."""

    def __init__(
        self,
        manager,
        *,
        entity_key: str,
        name: str,
        icon: str,
    ) -> None:
        self._manager = manager
        self._registry = manager.registry
        self._entity_key = entity_key
        self._source_entity_id: str | None = None
        self._source_unsub = None

        self._attr_has_entity_name = False
        self._attr_name = name
        self._attr_unique_id = f"sem_{entity_key}"
        self._attr_icon = icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "system_adapter_bridge")},
            name="SEM Energy System",
            manufacturer="SEM",
            model="Energy system adapter",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to registry and source entity updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._registry.add_listener(self._handle_registry_update)
        )
        self.async_on_remove(self._remove_source_listener)
        self._refresh_source_listener()
        self._update_from_source()

    @callback
    def _handle_registry_update(self) -> None:
        """Refresh the source entity when the registry changes."""
        self._refresh_source_listener()
        self._update_from_source()
        self.async_write_ha_state()

    @callback
    def _handle_source_state_change(self, event) -> None:
        """Update the proxy when its source entity changes."""
        self._update_from_source()
        self.async_write_ha_state()

    def _update_from_source(self) -> None:
        """Update platform-specific values from the current source."""

    @callback
    def _refresh_source_listener(self) -> None:
        """Follow the entity currently resolved by the SEM registry."""
        entity_id = self._manager.get_entity_id(self._entity_key)

        if entity_id == self._source_entity_id:
            return

        self._remove_source_listener()
        self._source_entity_id = entity_id

        if entity_id is not None:
            self._source_unsub = async_track_state_change_event(
                self.hass,
                [entity_id],
                self._handle_source_state_change,
            )

    @callback
    def _remove_source_listener(self) -> None:
        """Remove the current source entity subscription."""
        if self._source_unsub is not None:
            self._source_unsub()
            self._source_unsub = None

    @property
    def source_state(self) -> State | None:
        """Return the current state of the resolved source entity."""
        if self._source_entity_id is None:
            return None
        return self.hass.states.get(self._source_entity_id)

    @property
    def available(self) -> bool:
        """Only report available while the resolved source is available."""
        state = self.source_state
        return state is not None and state.state not in (
            "unknown",
            "unavailable",
        )

    async def async_call_source(
        self,
        service: str,
        service_data: dict[str, Any],
    ) -> None:
        """Forward a standard Home Assistant service to the active source."""
        if self._source_entity_id is None:
            raise HomeAssistantError(
                f"No source entity resolved for SEM key {self._entity_key}"
            )

        domain = self._source_entity_id.split(".", 1)[0]
        await self.hass.services.async_call(
            domain,
            service,
            {
                **service_data,
                "entity_id": self._source_entity_id,
            },
            blocking=True,
        )
