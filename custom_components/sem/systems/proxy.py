"""Shared proxy support for SEM system entities."""

from __future__ import annotations

from typing import Any

from homeassistant.core import State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event

from ..const import DOMAIN


SENSOR_ENTITY_DEFINITIONS = (
    ("battery_state_of_capacity", "SEM Battery SOC", "mdi:battery"),
    ("battery_rated_capacity", "SEM Battery Rated Capacity", "mdi:battery-high"),
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
    """Common behaviour for SEM entities backed by physical source entities."""

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
        self._source_entity_ids: list[str] = []
        self._source_unsub = None

        self._attr_has_entity_name = False
        self._attr_name = name
        # Keep the original unique ID namespace to preserve existing HA entity IDs.
        self._attr_unique_id = f"sem_huawei_{entity_key}"
        self._attr_icon = icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "huawei_entity_bridge")},
            name="SEM Energy System",
            manufacturer="SEM",
            model="Energy system adapter",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to registry and every active source entity."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._registry.add_listener(self._handle_registry_update)
        )
        self.async_on_remove(self._remove_source_listener)
        self._refresh_source_listener()
        self._update_from_source()

    @callback
    def _handle_registry_update(self) -> None:
        """Refresh source subscriptions when the registry changes."""
        self._refresh_source_listener()
        self._update_from_source()
        self.async_write_ha_state()

    @callback
    def _handle_source_state_change(self, event) -> None:
        """Update the proxy when any physical source changes."""
        self._update_from_source()
        self.async_write_ha_state()

    def _update_from_source(self) -> None:
        """Update platform-specific values from the adapter."""

    @callback
    def _refresh_source_listener(self) -> None:
        """Follow all source entities resolved by the SEM registry."""
        entity_ids = self._manager.get_entity_ids(self._entity_key)
        if entity_ids == self._source_entity_ids:
            return

        self._remove_source_listener()
        self._source_entity_ids = entity_ids
        if entity_ids:
            self._source_unsub = async_track_state_change_event(
                self.hass,
                entity_ids,
                self._handle_source_state_change,
            )

    @callback
    def _remove_source_listener(self) -> None:
        """Remove source entity subscriptions."""
        if self._source_unsub is not None:
            self._source_unsub()
            self._source_unsub = None

    @property
    def source_states(self) -> list[State | None]:
        """Return current states for every resolved source entity."""
        return [
            self.hass.states.get(entity_id)
            for entity_id in self._source_entity_ids
        ]

    @property
    def source_state(self) -> State | None:
        """Return the first source state for backwards compatibility."""
        states = self.source_states
        return states[0] if states else None

    @property
    def available(self) -> bool:
        """Require every resolved physical source to be available."""
        states = self.source_states
        return bool(states) and all(
            state is not None and state.state not in ("unknown", "unavailable")
            for state in states
        )

    async def async_call_sources(
        self,
        service: str,
        service_data: dict[str, Any],
        *,
        entity_ids: list[str] | None = None,
    ) -> None:
        """Forward one service call to all resolved physical source entities."""
        targets = entity_ids if entity_ids is not None else self._source_entity_ids
        if not targets:
            raise HomeAssistantError(
                f"No source entities resolved for SEM key {self._entity_key}"
            )
        domains = {entity_id.split(".", 1)[0] for entity_id in targets}
        if len(domains) != 1:
            raise HomeAssistantError(
                f"Source domains differ for SEM key {self._entity_key}"
            )
        await self.hass.services.async_call(
            next(iter(domains)),
            service,
            {
                **service_data,
                "entity_id": targets,
            },
            blocking=True,
        )

    async def async_call_source(
        self,
        service: str,
        service_data: dict[str, Any],
    ) -> None:
        """Compatibility wrapper for callers expecting a single source."""
        await self.async_call_sources(service, service_data)
