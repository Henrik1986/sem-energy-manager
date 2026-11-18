"""Pure aggregation helpers for Huawei multi-inverter systems."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def parse_numeric(value: Any) -> float | None:
    """Convert a state-like value to a finite float."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and value in {"unknown", "unavailable", ""}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def sum_numeric_values(values: Iterable[Any]) -> float | None:
    """Sum values only when every expected source has a valid numeric value."""
    parsed = [parse_numeric(value) for value in values]
    if not parsed or any(value is None for value in parsed):
        return None
    return sum(value for value in parsed if value is not None)


def capacity_weighted_soc(
    soc_by_device: Mapping[str, Any],
    capacity_by_device: Mapping[str, Any],
) -> float | None:
    """Calculate SOC weighted by rated capacity (both device maps must match)."""
    if not soc_by_device or set(soc_by_device) != set(capacity_by_device):
        return None

    weighted_sum = 0.0
    total_capacity = 0.0

    for device_id, soc_state in soc_by_device.items():
        soc = parse_numeric(soc_state)
        capacity = parse_numeric(capacity_by_device[device_id])
        if (
            soc is None
            or capacity is None
            or not 0 <= soc <= 100
            or capacity <= 0
        ):
            return None

        weighted_sum += soc * capacity
        total_capacity += capacity

    if total_capacity <= 0:
        return None

    return weighted_sum / total_capacity


def group_sources_by_inverter(
    sources: Sequence[Any],
) -> dict[str, list[Any]]:
    """Group physical sources by their owning inverter, not by daisy-chain root."""
    grouped: dict[str, list[Any]] = defaultdict(list)
    for source in sources:
        inverter_id = (
            getattr(source, "owner_device_id", None)
            or getattr(source, "device_id", None)
        )
        if inverter_id is not None:
            grouped[inverter_id].append(source)
    return dict(sorted(grouped.items()))


def plant_meter_value(
    values: Sequence[Any],
    *,
    absolute_tolerance: float = 100.0,
    relative_tolerance: float = 0.02,
) -> float | None:
    """Return one plant meter value without adding duplicate meter readings."""
    parsed = [parse_numeric(value) for value in values]
    if not parsed or any(value is None for value in parsed):
        return None

    valid = [value for value in parsed if value is not None]
    if len(valid) == 1:
        return valid[0]

    tolerance = max(
        absolute_tolerance,
        max(abs(value) for value in valid) * relative_tolerance,
    )
    if max(valid) - min(valid) > tolerance:
        return None

    return sum(valid) / len(valid)


def common_number_metadata(
    attributes: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Return the shared valid number range across all physical sources."""
    if not attributes:
        return None

    units = {item.get("unit_of_measurement") for item in attributes}
    modes = {item.get("mode", "auto") for item in attributes}
    mins = [parse_numeric(item.get("min", 0)) for item in attributes]
    maxes = [parse_numeric(item.get("max", 100)) for item in attributes]
    steps = [parse_numeric(item.get("step", 1)) for item in attributes]

    if (
        len(units) != 1
        or len(modes) != 1
        or any(value is None for value in mins + maxes + steps)
    ):
        return None

    step_values = [value for value in steps if value is not None]
    if any(
        not math.isclose(value, step_values[0], rel_tol=0, abs_tol=1e-9)
        for value in step_values[1:]
    ):
        return None

    valid_mins = [value for value in mins if value is not None]
    minimum = max(valid_mins)
    maximum = min(value for value in maxes if value is not None)
    step = step_values[0]

    # All devices must share the same step grid, not just the same step size.
    base_minimum = min(valid_mins)
    if not math.isclose(
        (minimum - base_minimum) / step,
        round((minimum - base_minimum) / step),
        rel_tol=0,
        abs_tol=1e-6,
    ):
        return None

    if minimum > maximum:
        return None

    # Round the displayed maximum down to a value every device can accept.
    maximum = minimum + math.floor((maximum - minimum) / step) * step
    if maximum < minimum:
        return None

    return {
        "min_value": minimum,
        "max_value": maximum,
        "step": step,
        "unit_of_measurement": next(iter(units)),
        "mode": next(iter(modes)),
    }


def value_fits_number_metadata(
    value: float,
    attributes: Mapping[str, Any],
) -> bool:
    """Check that a value satisfies one physical number entity's limits."""
    minimum = parse_numeric(attributes.get("min", 0))
    maximum = parse_numeric(attributes.get("max", 100))
    step = parse_numeric(attributes.get("step", 1))

    if minimum is None or maximum is None or step is None or step <= 0:
        return False
    if value < minimum - 1e-6 or value > maximum + 1e-6:
        return False

    steps_from_min = (value - minimum) / step
    return math.isclose(
        steps_from_min,
        round(steps_from_min),
        rel_tol=0,
        abs_tol=1e-6,
    )


def common_sem_options(
    source_options: Sequence[Sequence[str]],
    source_to_sem: Mapping[str, str],
) -> list[str]:
    """Return SEM options supported by every physical source."""
    if not source_options:
        return []

    translated = [
        {
            source_to_sem[option]
            for option in options
            if option in source_to_sem
        }
        for options in source_options
    ]
    common = set.intersection(*translated) if translated else set()

    return [
        source_to_sem[option]
        for option in source_options[0]
        if option in source_to_sem and source_to_sem[option] in common
    ]
