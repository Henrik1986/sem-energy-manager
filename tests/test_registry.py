"""Tests for required and optional SEM entity resolution."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "sem"
    / "systems"
    / "registry.py"
)
SPEC = importlib.util.spec_from_file_location("sem_test_registry", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
registry_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = registry_module
SPEC.loader.exec_module(registry_module)

SemEntityRegistry = registry_module.SemEntityRegistry


def test_missing_optional_entity_does_not_block_system_status() -> None:
    registry = SemEntityRegistry()
    registry.register_system(
        "solax",
        configured=True,
        required_entities=["battery_soc", "battery_power"],
        optional_entities=["battery_capacity"],
    )
    registry.set_entity_result(
        "solax",
        "battery_soc",
        status="found",
        entity_id="sensor.solax_soc",
    )
    registry.set_entity_result(
        "solax",
        "battery_power",
        status="found",
        entity_id="sensor.solax_battery_power",
    )
    registry.set_entity_result(
        "solax",
        "battery_capacity",
        status="disabled",
    )
    registry.set_capabilities(
        "solax",
        {
            "read.battery_soc": "found",
            "read.battery_capacity": "disabled",
            "write.battery_mode": "not_implemented",
        },
    )

    snapshot = registry.as_dict()

    assert registry.get_status() == "OK"
    assert snapshot["missing_entities"] == []
    assert snapshot["optional_missing_entities"] == [
        {
            "system": "solax",
            "entity": "battery_capacity",
            "status": "disabled",
            "required": False,
        }
    ]
    assert snapshot["systems"]["solax"]["entities"]["battery_capacity"][
        "required"
    ] is False
    assert snapshot["systems"]["solax"]["capabilities"][
        "write.battery_mode"
    ] == "not_implemented"


def test_missing_required_entity_still_blocks_system_status() -> None:
    registry = SemEntityRegistry()
    registry.register_system(
        "solax",
        configured=True,
        required_entities=["battery_soc"],
        optional_entities=["battery_capacity"],
    )
    registry.set_entity_result(
        "solax",
        "battery_soc",
        status="missing",
    )
    registry.set_entity_result(
        "solax",
        "battery_capacity",
        status="found",
        entity_id="sensor.solax_capacity",
    )

    snapshot = registry.as_dict()

    assert registry.get_status() == "FEL"
    assert snapshot["missing_entities"] == [
        {
            "system": "solax",
            "entity": "battery_soc",
            "status": "missing",
            "required": True,
        }
    ]
    assert snapshot["optional_missing_entities"] == []


def test_optional_entity_results_are_preserved_when_system_is_reregistered() -> None:
    registry = SemEntityRegistry()
    registry.register_system(
        "solax",
        configured=True,
        required_entities=["battery_soc"],
        optional_entities=["battery_capacity"],
    )
    registry.set_entity_result(
        "solax",
        "battery_capacity",
        status="disabled",
    )

    registry.register_system(
        "solax",
        configured=True,
        required_entities=["battery_soc"],
        optional_entities=["battery_capacity"],
    )

    assert registry.get_entity_status("solax", "battery_capacity") == "disabled"
