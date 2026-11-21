"""Factory for the system adapters supported by SEM."""

from .adapter import SemSystemAdapter
from .huawei import HuaweiSolarAdapter
from .solax import SolaXAdapter


def create_system_adapters() -> list[SemSystemAdapter]:
    """Create the configured set of system adapter implementations."""
    return [
        HuaweiSolarAdapter(),
        SolaXAdapter(),
    ]
