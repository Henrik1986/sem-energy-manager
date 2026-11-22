"""Pure capacity normalization and fallback helpers for SEM systems."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


def _positive_number(value: Any) -> float | None:
    """Return a finite positive number, rejecting unavailable and invalid values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def normalize_capacity_kwh(
    value: Any,
    unit: str | None,
    source_key: str | None = None,
) -> float | None:
    """Normalize a supported SolaX installed-capacity source to kWh."""
    number = _positive_number(value)
    if number is None:
        return None

    if unit:
        normalized_unit = unit.strip().lower()
        if normalized_unit in {"wh", "watt-hour", "watt-hours"}:
            return number / 1000
        if normalized_unit in {"kwh", "kilowatt-hour", "kilowatt-hours"}:
            return number
        return None

    # The Gen3 register is in kWh; the Gen4-6 register is in Wh.
    if source_key == "battery_install_capacity":
        return number
    if source_key == "battery_system_installed_capacity":
        return number / 1000
    return None


def complete_capacity_map(
    physical_capacities_kwh: Mapping[str, Any],
    battery_device_ids: Sequence[str],
    configured_total_kwh: Any,
) -> dict[str, float] | None:
    """Complete per-device capacities only when at most one is missing."""
    expected_ids = set(battery_device_ids)
    if not expected_ids:
        return None

    capacities: dict[str, float] = {}
    for device_id in expected_ids:
        value = _positive_number(physical_capacities_kwh.get(device_id))
        if value is not None:
            capacities[device_id] = value

    missing_ids = expected_ids - set(capacities)
    if not missing_ids:
        return capacities

    configured_total = _positive_number(configured_total_kwh)
    if configured_total is None or len(missing_ids) != 1:
        return None

    remaining_capacity = configured_total - sum(capacities.values())
    if not math.isfinite(remaining_capacity) or remaining_capacity <= 0:
        return None

    missing_id = next(iter(missing_ids))
    capacities[missing_id] = remaining_capacity
    return capacities


def total_capacity_kwh(
    physical_capacities_kwh: Mapping[str, Any],
    battery_device_ids: Sequence[str],
    configured_total_kwh: Any,
) -> float | None:
    """Prefer a complete physical total; otherwise use the configured plant total."""
    complete = complete_capacity_map(
        physical_capacities_kwh,
        battery_device_ids,
        configured_total_kwh,
    )
    if complete is not None:
        return sum(complete.values())

    return _positive_number(configured_total_kwh)
