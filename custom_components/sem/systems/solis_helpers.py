"""Pure helper functions for the Solis SEM adapter."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence


SOLIS_MODE_TO_SOURCE_OPTION = {
    "self_consumption": "Self-Use - No Grid Charging",
    "feed_in_priority": "Feed-in priority - No Grid Charging",
    "time_of_use": "Self-Use",
}
SOLIS_SOURCE_OPTION_TO_MODE = {
    option: mode for mode, option in SOLIS_MODE_TO_SOURCE_OPTION.items()
}

SOLIS_END_OF_DISCHARGE_SOC_ENTITY_KEY = "end_of_discharge_soc"
SOLIS_END_OF_DISCHARGE_SOC_SOURCE_KEY = "battery_minimum_soc"

SOLIS_BATTERY_SOC_ENTITY_KEY = "battery_state_of_capacity"
SOLIS_BATTERY_SOC_SOURCE_KEY = "battery_soc"
SOLIS_BATTERY_RATED_CAPACITY_ENTITY_KEY = "battery_rated_capacity"
SOLIS_TOTAL_CAPACITY_HELPER = "input_number.sem_battery_total_capacity_kwh"

SOLIS_GRID_CHARGE_MAXIMUM_POWER_ENTITY_KEY = "grid_charge_maximum_power"
SOLIS_GRID_CHARGE_NO_GRID_SOURCE_OPTION = "Timed Charge/Discharge - No Grid Charging"

def solis_total_capacity_kwh(value: object) -> float | None:
    """Validate the user's configured total Solis battery capacity in kWh."""
    try:
        capacity = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(capacity) or capacity <= 0:
        return None

    return capacity


def solis_battery_soc_value(values: Sequence[object]) -> float | None:
    """Return SOC only when all available Solis inverter readings agree."""
    if not values:
        return None

    parsed: list[float] = []
    for value in values:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric) or not 0 <= numeric <= 100:
            return None
        parsed.append(numeric)

    reference = parsed[0]
    if any(
        not math.isclose(value, reference, rel_tol=0.0, abs_tol=1e-6)
        for value in parsed[1:]
    ):
        return None

    return reference


def solis_grid_charge_blocked_option(sem_option: str) -> str | None:
    """Return the Solis working-mode option that prevents grid charging."""
    if sem_option == "time_of_use":
        return SOLIS_GRID_CHARGE_NO_GRID_SOURCE_OPTION
    return SOLIS_MODE_TO_SOURCE_OPTION.get(sem_option)

SOLIS_TOU_NUMBER_FIELDS = (
    "timed_charge_start_hours",
    "timed_charge_start_minutes",
    "timed_charge_end_hours",
    "timed_charge_end_minutes",
    "timed_discharge_start_hours",
    "timed_discharge_start_minutes",
    "timed_discharge_end_hours",
    "timed_discharge_end_minutes",
)
SOLIS_TOU_NUMBER_ENTITY_KEYS = tuple(
    f"{field}{'' if slot == 1 else f'_{slot}'}"
    for slot in range(1, 4)
    for field in SOLIS_TOU_NUMBER_FIELDS
)
SOLIS_TOU_BUTTON_ENTITY_KEYS = (
    "update_charge_discharge_times",
    "update_charge_discharge_times_2",
    "update_charge_discharge_times_3",
)

SOLIS_TOU_PERIOD_PATTERN = re.compile(
    r"(?P<start>(?:[01]\d|2[0-3]):[0-5]\d)-"
    r"(?P<end>(?:[01]\d|2[0-3]):[0-5]\d)/"
    r"(?P<days>\d{7})/(?P<flag>[+-])"
)


def _minutes(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute


def parse_solis_tou_periods(value: str) -> dict[str, list[tuple[str, str]]]:
    """Parse SEM's period format into Solis charge and discharge windows."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Solis TOU periods must be a non-empty string")

    result: dict[str, list[tuple[str, str]]] = {
        "charge": [],
        "discharge": [],
    }

    for line_number, raw_line in enumerate(value.strip().splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        match = SOLIS_TOU_PERIOD_PATTERN.fullmatch(line)
        if match is None:
            raise ValueError(
                f"Invalid Solis TOU period on line {line_number}: {line!r}. "
                "Expected HH:MM-HH:MM/1234567/+ or /-."
            )

        if len(match.group("days")) != 7 or set(match.group("days")) != set("1234567"):
            raise ValueError(
                "Solis timed charge/discharge supports all seven days only; "
                "use 1234567."
            )

        start = match.group("start")
        end = match.group("end")
        if _minutes(start) >= _minutes(end):
            raise ValueError(
                f"Invalid Solis TOU period {start}-{end}: "
                "the start must be earlier than the end on the same day."
            )

        target = "charge" if match.group("flag") == "+" else "discharge"
        result[target].append((start, end))
        if len(result[target]) > 3:
            raise ValueError(
                f"Solis supports at most three {target} periods."
            )

    if not result["charge"] and not result["discharge"]:
        raise ValueError("No valid Solis TOU periods were supplied")

    all_windows = sorted(
        (
            (_minutes(start), _minutes(end), kind)
            for kind, windows in result.items()
            for start, end in windows
        ),
        key=lambda item: item[0],
    )
    for previous, current in zip(all_windows, all_windows[1:]):
        if current[0] < previous[1]:
            raise ValueError(
                "Solis TOU charge and discharge periods must not overlap."
            )

    for kind in result:
        result[kind].sort(key=lambda item: _minutes(item[0]))

    return result


def build_solis_time_values(
    periods: dict[str, list[tuple[str, str]]],
) -> dict[str, int]:
    """Build all three Solis time slots, clearing every unused slot."""
    values: dict[str, int] = {}

    for slot in range(1, 4):
        suffix = "" if slot == 1 else f"_{slot}"
        charge = (
            periods["charge"][slot - 1]
            if len(periods["charge"]) >= slot
            else ("00:00", "00:00")
        )
        discharge = (
            periods["discharge"][slot - 1]
            if len(periods["discharge"]) >= slot
            else ("00:00", "00:00")
        )

        for prefix, window in (
            ("timed_charge", charge),
            ("timed_discharge", discharge),
        ):
            start_hour, start_minute = (
                int(part) for part in window[0].split(":")
            )
            end_hour, end_minute = (
                int(part) for part in window[1].split(":")
            )
            values[f"{prefix}_start_hours{suffix}"] = start_hour
            values[f"{prefix}_start_minutes{suffix}"] = start_minute
            values[f"{prefix}_end_hours{suffix}"] = end_hour
            values[f"{prefix}_end_minutes{suffix}"] = end_minute

    return values


def build_solis_discharge_current_values(
    maximum_power_w: float,
    voltage_by_device: Mapping[str, float],
    current_limits_by_device: Mapping[str, Mapping[str, float]],
) -> dict[str, float]:
    """Convert a plant discharge-power ceiling into safe inverter current limits."""
    if (
        not isinstance(maximum_power_w, (int, float))
        or not math.isfinite(maximum_power_w)
        or maximum_power_w < 0
    ):
        raise ValueError(
            "Maximum discharge power must be a finite non-negative number"
        )
    if (
        not voltage_by_device
        or set(voltage_by_device) != set(current_limits_by_device)
    ):
        raise ValueError(
            "Battery voltage and discharge-current sources must match"
        )

    power_per_device = maximum_power_w / len(voltage_by_device)
    result: dict[str, float] = {}
    for device_id, voltage in voltage_by_device.items():
        if (
            not isinstance(voltage, (int, float))
            or not math.isfinite(voltage)
            or voltage <= 0
        ):
            raise ValueError(
                f"Invalid battery voltage for Solis inverter {device_id}"
            )

        limits = current_limits_by_device[device_id]
        try:
            minimum = float(limits["min"])
            maximum = float(limits["max"])
            step = float(limits["step"])
        except (KeyError, TypeError, ValueError) as err:
            raise ValueError(
                f"Invalid discharge-current limits for Solis inverter {device_id}"
            ) from err
        if (
            not all(math.isfinite(item) for item in (minimum, maximum, step))
            or minimum > maximum
            or step <= 0
        ):
            raise ValueError(
                f"Invalid discharge-current range for Solis inverter {device_id}"
            )

        requested_current = power_per_device / float(voltage)
        if requested_current < minimum and minimum > 0:
            raise ValueError(
                f"Requested discharge power is below the minimum current "
                f"setting for Solis inverter {device_id}"
            )
        if maximum_power_w == 0:
            if minimum > 0:
                raise ValueError(
                    f"Solis inverter {device_id} cannot set zero discharge current"
                )
            result[device_id] = 0.0
            continue

        bounded_current = min(requested_current, maximum)
        steps = math.floor((bounded_current - minimum + 1e-9) / step)
        target_current = minimum + max(0, steps) * step
        if target_current > maximum:
            target_current = minimum + math.floor(
                (maximum - minimum + 1e-9) / step
            ) * step
        result[device_id] = round(target_current, 6)

    return result
