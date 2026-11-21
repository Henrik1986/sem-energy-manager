"""Unit tests for provider-independent SEM aggregation helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "sem"
    / "systems"
    / "aggregation.py"
)
SPEC = importlib.util.spec_from_file_location("aggregation", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
aggregation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(aggregation)

capacity_weighted_soc = aggregation.capacity_weighted_soc
common_number_metadata = aggregation.common_number_metadata
common_sem_options = aggregation.common_sem_options
group_sources_by_owner = aggregation.group_sources_by_owner
plant_meter_value = aggregation.plant_meter_value
sum_numeric_values = aggregation.sum_numeric_values
value_fits_number_metadata = aggregation.value_fits_number_metadata


def test_single_battery_soc_is_unchanged() -> None:
    assert capacity_weighted_soc(
        {"battery_1": 63},
        {"battery_1": 13_800},
    ) == 63


def test_multiple_batteries_use_capacity_weighted_soc() -> None:
    assert capacity_weighted_soc(
        {"battery_1": 50, "battery_2": 80},
        {"battery_1": 10_000, "battery_2": 30_000},
    ) == 72.5


def test_soc_is_unavailable_when_capacity_is_missing_or_invalid() -> None:
    assert capacity_weighted_soc(
        {"battery_1": 50, "battery_2": 80},
        {"battery_1": 10_000},
    ) is None
    assert capacity_weighted_soc(
        {"battery_1": 50},
        {"battery_1": "unavailable"},
    ) is None
    assert capacity_weighted_soc(
        {"battery_1": 101},
        {"battery_1": 10_000},
    ) is None


def test_soc_requires_matching_physical_battery_ids() -> None:
    assert capacity_weighted_soc(
        {"battery_1": 50},
        {"battery_2": 10_000},
    ) is None


def test_single_and_multiple_source_values_are_summed() -> None:
    assert sum_numeric_values([1250]) == 1250
    assert sum_numeric_values([1250, 875]) == 2125


def test_sum_does_not_partially_aggregate_unavailable_sources() -> None:
    assert sum_numeric_values([1250, "unavailable"]) is None
    assert sum_numeric_values([]) is None


def test_plant_meter_duplicates_are_compared_not_summed() -> None:
    assert plant_meter_value([1000]) == 1000
    assert plant_meter_value([1000, 1050]) == 1025
    assert plant_meter_value([1000, 1800]) is None
    assert plant_meter_value([1000, "unavailable"]) is None


def test_number_metadata_uses_intersection_of_physical_ranges() -> None:
    metadata = common_number_metadata([
        {
            "min": 0,
            "max": 5000,
            "step": 100,
            "unit_of_measurement": "W",
            "mode": "box",
        },
        {
            "min": 500,
            "max": 4000,
            "step": 100,
            "unit_of_measurement": "W",
            "mode": "box",
        },
    ])
    assert metadata == {
        "min_value": 500,
        "max_value": 4000,
        "step": 100,
        "unit_of_measurement": "W",
        "mode": "box",
    }


def test_number_metadata_rejects_incompatible_sources() -> None:
    assert common_number_metadata([
        {"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"},
        {"min": 110, "max": 200, "step": 1, "unit_of_measurement": "%"},
    ]) is None


def test_number_value_is_checked_against_each_physical_range() -> None:
    common_value = 1500
    assert value_fits_number_metadata(
        common_value,
        {"min": 0, "max": 5000, "step": 100},
    )
    assert not value_fits_number_metadata(
        common_value,
        {"min": 0, "max": 1000, "step": 100},
    )
    assert not value_fits_number_metadata(
        1550,
        {"min": 0, "max": 5000, "step": 100},
    )


def test_select_options_are_intersected_and_translated() -> None:
    option_map = {
        "maximise_self_consumption": "self_consumption",
        "time_of_use_luna2000": "time_of_use",
        "fully_fed_to_grid": "feed_in_priority",
    }
    result = common_sem_options(
        [
            ["maximise_self_consumption", "time_of_use_luna2000"],
            ["maximise_self_consumption", "fully_fed_to_grid"],
        ],
        option_map,
    )
    assert result == ["self_consumption"]


def test_sources_group_by_owner_not_root_device() -> None:
    sources = [
        SimpleNamespace(
            owner_device_id="master",
            device_id="master_battery",
            root_device_id="master",
        ),
        SimpleNamespace(
            owner_device_id="slave",
            device_id="slave_battery",
            root_device_id="master",
        ),
        SimpleNamespace(
            owner_device_id="separate",
            device_id="separate_battery",
            root_device_id="separate",
        ),
    ]
    groups = group_sources_by_owner(sources)
    assert set(groups) == {"master", "slave", "separate"}
    assert len(groups["master"]) == 1
    assert len(groups["slave"]) == 1


def test_number_range_requires_a_common_step_grid() -> None:
    assert common_number_metadata([
        {"min": 0, "max": 10, "step": 1, "unit_of_measurement": "W"},
        {"min": 0.5, "max": 10, "step": 1, "unit_of_measurement": "W"},
    ]) is None
