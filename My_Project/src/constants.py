"""
PayrollX — Shared Domain Constants
"""

MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def get_month_name(month: int) -> str:
    """Return the full English month name for a 1-based month number (1-12)."""
    return MONTH_NAMES[month - 1] if 1 <= month <= 12 else str(month)
