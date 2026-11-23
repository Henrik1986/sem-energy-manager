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

SOLIS_MODE_TO_SOURCE_OPTION = solis_helpers.SOLIS_MODE_TO_SOURCE_OPTION
SOLIS_TOU_NUMBER_ENTITY_KEYS = solis_helpers.SOLIS_TOU_NUMBER_ENTITY_KEYS
build_solis_time_values = solis_helpers.build_solis_time_values
parse_solis_tou_periods = solis_helpers.parse_solis_tou_periods


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
