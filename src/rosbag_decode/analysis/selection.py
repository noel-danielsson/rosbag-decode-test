"""Inclusive integer-nanosecond selections, independent of display state."""
from decimal import Decimal, ROUND_HALF_EVEN

import numpy as np
import pandas as pd


def interval(start, end):
    start, end = int(start), int(end)
    if start > end:
        raise ValueError("Interval start must not exceed end")
    return start, end


def from_drag(start, end, origin_ns):
    def ns(value):
        return int(origin_ns) + int((Decimal(str(value)) * 1_000_000_000).to_integral_value(
            rounding=ROUND_HALF_EVEN))
    return interval(ns(min(start, end)), ns(max(start, end)))


def parse_utc(value):
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError("Include a timezone, e.g. 2025-11-14T13:24:00Z")
    return stamp.as_unit("ns").value


def utc(value):
    return pd.Timestamp(int(value), unit="ns", tz="UTC").isoformat()


def mask(frame, intervals):
    result = np.zeros(len(frame), dtype=bool)
    stamps = frame.index.as_unit("ns").asi8
    for start, end in intervals:
        start, end = interval(start, end)
        result |= (stamps >= start) & (stamps <= end)
    return result
