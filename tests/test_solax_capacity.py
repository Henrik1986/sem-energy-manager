"""Tests for SolaX installed-capacity normalization and fallback."""

from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "sem"
    / "systems"
    / "capacity_aggregation.py"
)
SPEC = importlib.util.spec_from_file_location("capacity_aggregation", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
capacity_aggregation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capacity_aggregation)

complete_capacity_map = capacity_aggregation.complete_capacity_map
normalize_capacity_kwh = capacity_aggregation.normalize_capacity_kwh
total_capacity_kwh = capacity_aggregation.total_capacity_kwh


def test_gen3_capacity_in_kwh_is_not_rescaled() -> None:
    assert normalize_capacity_kwh(
        13.8,
        "kWh",
        "battery_install_capacity",
    ) == 13.8


def test_gen4_to_gen6_capacity_in_wh_is_converted_to_kwh() -> None:
    assert normalize_capacity_kwh(
        13_800,
        "Wh",
        "battery_system_installed_capacity",
    ) == 13.8


def test_source_key_is_used_when_unit_attribute_is_missing() -> None:
    assert normalize_capacity_kwh(
        13.8,
        None,
        "battery_install_capacity",
    ) == 13.8
    assert normalize_capacity_kwh(
        13_800,
        None,
        "battery_system_installed_capacity",
    ) == 13.8


def test_complete_physical_capacities_take_priority_over_helper() -> None:
    assert complete_capacity_map(
        {"a": 10, "b": 20},
        ["a", "b"],
        99,
    ) == {"a": 10, "b": 20}
    assert total_capacity_kwh(
        {"a": 10, "b": 20},
        ["a", "b"],
        99,
    ) == 30


def test_single_missing_capacity_is_inferred_from_configured_total() -> None:
    assert complete_capacity_map(
        {"a": 10},
        ["a", "b"],
        25,
    ) == {"a": 10, "b": 15}


def test_single_battery_can_use_configured_total_as_fallback() -> None:
    assert complete_capacity_map(
        {},
        ["a"],
        13.8,
    ) == {"a": 13.8}


def test_multiple_missing_capacities_are_not_distributed() -> None:
    assert complete_capacity_map(
        {},
        ["a", "b"],
        27.6,
    ) is None
    assert total_capacity_kwh(
        {},
        ["a", "b"],
        27.6,
    ) == 27.6


def test_capacity_is_not_reported_without_discovered_batteries() -> None:
    assert total_capacity_kwh({}, [], 13.8) is None


def test_invalid_or_non_positive_helper_values_are_rejected() -> None:
    for value in (None, "unknown", "unavailable", 0, -1, float("nan")):
        assert complete_capacity_map({}, ["a"], value) is None
        assert total_capacity_kwh({}, ["a"], value) is None


def test_fallback_is_rejected_when_no_positive_capacity_remains() -> None:
    assert complete_capacity_map(
        {"a": 30},
        ["a", "b"],
        20,
    ) is None
