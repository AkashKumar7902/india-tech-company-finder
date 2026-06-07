from __future__ import annotations

import math
from typing import Optional


HSR_CENTER_LAT = 12.9116
HSR_CENTER_LNG = 77.6389
# Approximate bounding box covering HSR Layout sectors and immediate edge roads.
# Format: south, west, north, east.
HSR_BBOX = (12.8950, 77.6250, 12.9250, 77.6530)


def distance_m(
    lat1: Optional[float],
    lng1: Optional[float],
    lat2: Optional[float],
    lng2: Optional[float],
) -> Optional[float]:
    if None in (lat1, lng1, lat2, lng2):
        return None

    radius_m = 6_371_000
    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    d_phi = math.radians(float(lat2) - float(lat1))
    d_lambda = math.radians(float(lng2) - float(lng1))

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_m * c
