"""Explicit resource and numeric limits for external measurement evidence."""

from decimal import Decimal

MAX_CSV_BYTES = 1_048_576
MAX_CSV_ROWS = 4_096
MAX_CSV_ISSUES = 100
MAX_NUMERIC_TEXT_LENGTH = 256
MAX_MEASUREMENT_DIGITS = 128
MAX_MEASUREMENT_EXPONENT = 100


def validate_measurement_decimal(value: Decimal) -> Decimal:
    """Reject representations outside the supported measurement envelope."""

    if (
        not value.is_finite()
        or len(value.as_tuple().digits) > MAX_MEASUREMENT_DIGITS
        or abs(value.adjusted()) > MAX_MEASUREMENT_EXPONENT
    ):
        raise ValueError(
            "measurement numbers require at most 128 digits "
            "and an adjusted exponent between -100 and 100"
        )
    return value
