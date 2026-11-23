"""Unit tests for Solis mode and time-of-use translation helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "sem"
    / "systems"
    / "solis_helpers.py"
)
SPEC = importlib.util.spec_from_file_location("solis_helpers", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
solis_helpers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(solis_helpers)

SOLIS_BATTERY_SOC_ENTITY_KEY = solis_helpers.SOLIS_BATTERY_SOC_ENTITY_KEY
SOLIS_INVERTER_INPUT_POWER_ENTITY_KEY = solis_helpers.SOLIS_INVERTER_INPUT_POWER_ENTITY_KEY
SOLIS_POWER_METER_ENTITY_KEY = solis_helpers.SOLIS_POWER_METER_ENTITY_KEY
SOLIS_POWER_METER_SOURCE_KEY = solis_helpers.SOLIS_POWER_METER_SOURCE_KEY
SOLIS_INVERTER_INPUT_POWER_SOURCE_KEY = solis_helpers.SOLIS_INVERTER_INPUT_POWER_SOURCE_KEY
sum_solis_pv_power = solis_helpers.sum_solis_pv_power
SOLIS_BATTERY_RATED_CAPACITY_ENTITY_KEY = (
    solis_helpers.SOLIS_BATTERY_RATED_CAPACITY_ENTITY_KEY
)
SOLIS_TOTAL_CAPACITY_HELPER = solis_helpers.SOLIS_TOTAL_CAPACITY_HELPER
solis_total_capacity_kwh = solis_helpers.solis_total_capacity_kwh
SOLIS_BATTERY_SOC_SOURCE_KEY = solis_helpers.SOLIS_BATTERY_SOC_SOURCE_KEY
solis_battery_soc_value = solis_helpers.solis_battery_soc_value
SOLIS_END_OF_DISCHARGE_SOC_ENTITY_KEY = (
    solis_helpers.SOLIS_END_OF_DISCHARGE_SOC_ENTITY_KEY
)
SOLIS_END_OF_DISCHARGE_SOC_SOURCE_KEY = (
    solis_helpers.SOLIS_END_OF_DISCHARGE_SOC_SOURCE_KEY
)
SOLIS_GRID_CHARGE_MAXIMUM_POWER_ENTITY_KEY = (
    solis_helpers.SOLIS_GRID_CHARGE_MAXIMUM_POWER_ENTITY_KEY
)
SOLIS_GRID_CHARGE_NO_GRID_SOURCE_OPTION = (
    solis_helpers.SOLIS_GRID_CHARGE_NO_GRID_SOURCE_OPTION
)
solis_grid_charge_blocked_option = solis_helpers.solis_grid_charge_blocked_option
SOLIS_MODE_TO_SOURCE_OPTION = solis_helpers.SOLIS_MODE_TO_SOURCE_OPTION
SOLIS_TOU_NUMBER_ENTITY_KEYS = solis_helpers.SOLIS_TOU_NUMBER_ENTITY_KEYS
build_solis_time_values = solis_helpers.build_solis_time_values
build_solis_discharge_current_values = (
    solis_helpers.build_solis_discharge_current_values
)
parse_solis_tou_periods = solis_helpers.parse_solis_tou_periods


def test_power_meter_maps_to_standard_solis_meter_sensor() -> None:
    assert SOLIS_POWER_METER_ENTITY_KEY == "power_meter_active_power"
    assert SOLIS_POWER_METER_SOURCE_KEY == "meter_active_power"


def test_inverter_input_power_maps_to_pv_total_power_per_inverter() -> None:
    assert SOLIS_INVERTER_INPUT_POWER_ENTITY_KEY == "inverter_input_power"
    assert SOLIS_INVERTER_INPUT_POWER_SOURCE_KEY == "pv_total_power"


def test_solis_pv_power_sums_all_inverter_readings() -> None:
    assert sum_solis_pv_power(["1200", 2300, 0]) == 3500


def test_solis_pv_power_rejects_incomplete_or_invalid_readings() -> None:
    for values in ([], ["1000", "unavailable"], ["nan"], ["inf"], [-1], [True]):
        assert sum_solis_pv_power(values) is None


def test_battery_rated_capacity_maps_to_sem_helper() -> None:
    assert SOLIS_BATTERY_RATED_CAPACITY_ENTITY_KEY == "battery_rated_capacity"
    assert SOLIS_TOTAL_CAPACITY_HELPER == "input_number.sem_battery_total_capacity_kwh"


def test_total_capacity_helper_accepts_positive_kwh_values() -> None:
    assert solis_total_capacity_kwh("13.8") == 13.8
    assert solis_total_capacity_kwh(20) == 20


def test_total_capacity_helper_rejects_invalid_values() -> None:
    for value in ("unknown", "unavailable", "nan", 0, -1, "inf"):
        assert solis_total_capacity_kwh(value) is None


def test_battery_soc_maps_to_standard_solis_sensor() -> None:
    assert SOLIS_BATTERY_SOC_ENTITY_KEY == "battery_state_of_capacity"
    assert SOLIS_BATTERY_SOC_SOURCE_KEY == "battery_soc"


def test_single_and_matching_solis_soc_readings_are_preserved() -> None:
    assert solis_battery_soc_value(["72"]) == 72
    assert solis_battery_soc_value(["72", 72.0]) == 72


def test_different_solis_soc_readings_are_not_averaged() -> None:
    assert solis_battery_soc_value(["70", "60"]) is None


def test_invalid_solis_soc_readings_are_rejected() -> None:
    for values in ([], ["unknown"], ["101"], ["-1"], ["nan"], ["50", "unavailable"]):
        assert solis_battery_soc_value(values) is None


def test_end_of_discharge_soc_maps_to_battery_minimum_soc() -> None:
    assert SOLIS_END_OF_DISCHARGE_SOC_ENTITY_KEY == "end_of_discharge_soc"
    assert SOLIS_END_OF_DISCHARGE_SOC_SOURCE_KEY == "battery_minimum_soc"


def test_grid_charge_limit_maps_to_existing_sem_number() -> None:
    assert SOLIS_GRID_CHARGE_MAXIMUM_POWER_ENTITY_KEY == "grid_charge_maximum_power"


def test_grid_charge_block_uses_timed_no_grid_mode_only_for_tou() -> None:
    assert solis_grid_charge_blocked_option("time_of_use") == (
        SOLIS_GRID_CHARGE_NO_GRID_SOURCE_OPTION
    )
    assert solis_grid_charge_blocked_option("self_consumption") == (
        "Self-Use - No Grid Charging"
    )
    assert solis_grid_charge_blocked_option("feed_in_priority") == (
        "Feed-in priority - No Grid Charging"
    )
    assert solis_grid_charge_blocked_option("unsupported") is None


def test_sem_modes_map_to_solis_options() -> None:
    assert SOLIS_MODE_TO_SOURCE_OPTION == {
        "self_consumption": "Self-Use - No Grid Charging",
        "feed_in_priority": "Feed-in priority - No Grid Charging",
        "time_of_use": "Self-Use",
    }


def test_all_day_charge_period_accepts_trailing_whitespace() -> None:
    periods = parse_solis_tou_periods(
        "00:00-23:59/1234567/+ "
    )

    assert periods == {
        "charge": [("00:00", "23:59")],
        "discharge": [],
    }

    values = build_solis_time_values(periods)
    assert len(values) == 24
    assert values["timed_charge_start_hours"] == 0
    assert values["timed_charge_start_minutes"] == 0
    assert values["timed_charge_end_hours"] == 23
    assert values["timed_charge_end_minutes"] == 59
    assert values["timed_discharge_start_hours"] == 0
    assert values["timed_discharge_end_hours"] == 0


def test_charge_and_discharge_periods_fill_separate_slots() -> None:
    periods = parse_solis_tou_periods(
        "01:00-03:30/1234567/+\n"
        "06:15-07:00/1234567/-\n"
        "12:00-13:45/1234567/+\n"
        "18:00-19:30/1234567/-"
    )

    values = build_solis_time_values(periods)
    assert values["timed_charge_start_hours"] == 1
    assert values["timed_charge_start_minutes"] == 0
    assert values["timed_charge_end_hours"] == 3
    assert values["timed_charge_end_minutes"] == 30
    assert values["timed_discharge_start_hours"] == 6
    assert values["timed_discharge_start_minutes"] == 15
    assert values["timed_charge_start_hours_2"] == 12
    assert values["timed_discharge_start_hours_2"] == 18

    # Unused third slot is explicitly cleared.
    assert values["timed_charge_start_hours_3"] == 0
    assert values["timed_charge_end_hours_3"] == 0
    assert values["timed_discharge_start_hours_3"] == 0
    assert values["timed_discharge_end_hours_3"] == 0


def test_three_charge_and_discharge_periods_are_supported() -> None:
    periods = parse_solis_tou_periods(
        "00:00-01:00/1234567/+\n"
        "02:00-03:00/1234567/-\n"
        "04:00-05:00/1234567/+\n"
        "06:00-07:00/1234567/-\n"
        "08:00-09:00/1234567/+\n"
        "10:00-11:00/1234567/-"
    )

    values = build_solis_time_values(periods)
    assert values["timed_charge_start_hours_3"] == 8
    assert values["timed_discharge_start_hours_3"] == 10


def test_non_daily_weekday_mask_is_rejected() -> None:
    try:
        parse_solis_tou_periods("00:00-01:00/1234560/+")
    except ValueError as err:
        assert "all seven days" in str(err)
    else:
        raise AssertionError("Expected a ValueError")


def test_invalid_and_overnight_periods_are_rejected() -> None:
    invalid_values = (
        "24:00-01:00/1234567/+",
        "23:00-01:00/1234567/+",
        "10:00-10:00/1234567/+",
        "invalid",
    )
    for value in invalid_values:
        try:
            parse_solis_tou_periods(value)
        except ValueError:
            continue
        raise AssertionError(f"Expected a ValueError for {value!r}")


def test_overlapping_charge_and_discharge_periods_are_rejected() -> None:
    try:
        parse_solis_tou_periods(
            "00:00-02:00/1234567/+\n"
            "01:00-03:00/1234567/-"
        )
    except ValueError as err:
        assert "must not overlap" in str(err)
    else:
        raise AssertionError("Expected a ValueError")


def test_more_than_three_periods_of_one_type_are_rejected() -> None:
    try:
        parse_solis_tou_periods(
            "00:00-01:00/1234567/+\n"
            "02:00-03:00/1234567/+\n"
            "04:00-05:00/1234567/+\n"
            "06:00-07:00/1234567/+"
        )
    except ValueError as err:
        assert "at most three charge periods" in str(err)
    else:
        raise AssertionError("Expected a ValueError")


def test_all_local_number_keys_match_three_eight_value_slots() -> None:
    assert len(SOLIS_TOU_NUMBER_ENTITY_KEYS) == 24
    assert "timed_charge_start_hours" in SOLIS_TOU_NUMBER_ENTITY_KEYS
    assert "timed_charge_start_hours_2" in SOLIS_TOU_NUMBER_ENTITY_KEYS
    assert "timed_charge_start_hours_3" in SOLIS_TOU_NUMBER_ENTITY_KEYS


def test_discharge_power_is_split_and_converted_to_current() -> None:
    values = build_solis_discharge_current_values(
        6_000,
        {"inv-1": 400, "inv-2": 500},
        {
            "inv-1": {"min": 0, "max": 100, "step": 1},
            "inv-2": {"min": 0, "max": 100, "step": 1},
        },
    )
    assert values == {"inv-1": 7.0, "inv-2": 6.0}
    assert (
        values["inv-1"] * 400 + values["inv-2"] * 500
        <= 6_000
    )


def test_zero_discharge_power_sets_zero_current() -> None:
    values = build_solis_discharge_current_values(
        0,
        {"inv-1": 400},
        {"inv-1": {"min": 0, "max": 100, "step": 1}},
    )
    assert values == {"inv-1": 0.0}


def test_discharge_current_is_limited_to_source_maximum() -> None:
    values = build_solis_discharge_current_values(
        20_000,
        {"inv-1": 400},
        {"inv-1": {"min": 0, "max": 20, "step": 1}},
    )
    assert values == {"inv-1": 20.0}


def test_discharge_power_rejects_invalid_voltage_or_limits() -> None:
    invalid_cases = (
        (0, {"inv-1": 0}, {"inv-1": {"min": 0, "max": 100, "step": 1}}),
        (1_000, {"inv-1": 400}, {"inv-1": {"min": 0, "max": 100, "step": 0}}),
        (1_000, {"inv-1": 400}, {"inv-2": {"min": 0, "max": 100, "step": 1}}),
    )
    for power, voltages, limits in invalid_cases:
        try:
            build_solis_discharge_current_values(power, voltages, limits)
        except ValueError:
            continue
        raise AssertionError("Expected invalid discharge-power inputs to fail")
