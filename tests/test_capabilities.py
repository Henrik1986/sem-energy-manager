"""Unit tests for the provider-independent SEM capability model."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "sem"
    / "systems"
    / "capabilities.py"
)
SPEC = importlib.util.spec_from_file_location("capabilities", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
capabilities_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = capabilities_module
SPEC.loader.exec_module(capabilities_module)

EntityCapability = capabilities_module.EntityCapability
SystemCapabilities = capabilities_module.SystemCapabilities


def test_required_and_optional_entities_are_distinguished() -> None:
    capabilities = SystemCapabilities(
        entities={
            "battery_soc": EntityCapability(required=True, readable=True),
            "battery_day_charge": EntityCapability(required=False, readable=True),
        }
    )

    assert capabilities.required_entity_keys == ("battery_soc",)
    assert capabilities.has_entity("battery_soc")
    assert capabilities.has_entity("battery_day_charge")
    assert not capabilities.has_entity("battery_unknown")


def test_read_and_write_capabilities_are_independent() -> None:
    capabilities = SystemCapabilities(
        entities={
            "battery_soc": EntityCapability(
                required=True,
                readable=True,
                writable=False,
            ),
            "battery_mode": EntityCapability(
                required=False,
                readable=True,
                writable=True,
            ),
            "write_only_control": EntityCapability(
                required=False,
                readable=False,
                writable=True,
            ),
        }
    )

    assert capabilities.can_read_entity("battery_soc")
    assert not capabilities.can_write_entity("battery_soc")
    assert capabilities.can_read_entity("battery_mode")
    assert capabilities.can_write_entity("battery_mode")
    assert not capabilities.can_read_entity("write_only_control")
    assert capabilities.can_write_entity("write_only_control")


def test_supported_commands_are_explicit() -> None:
    capabilities = SystemCapabilities(
        commands=frozenset({"set_grid_export_power_limit"})
    )

    assert capabilities.supports_command("set_grid_export_power_limit")
    assert not capabilities.supports_command("set_battery_time_of_use_periods")


def test_capabilities_are_serializable_for_status_sensor() -> None:
    capabilities = SystemCapabilities(
        entities={
            "battery_soc": EntityCapability(required=True, readable=True),
        },
        commands=frozenset({"reset_grid_export_power_limit"}),
    )

    assert capabilities.as_dict() == {
        "entities": {
            "battery_soc": {
                "required": True,
                "readable": True,
                "writable": False,
            }
        },
        "commands": ["reset_grid_export_power_limit"],
    }
