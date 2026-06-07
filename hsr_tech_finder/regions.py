from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence, Tuple


Bbox = Tuple[float, float, float, float]  # south, west, north, east


@dataclass(frozen=True)
class Region:
    """Search area for one tech hub/city."""

    id: str
    city: str
    state: str
    query: str
    lat: float
    lng: float
    radius_m: int
    bbox: Bbox
    country: str = "India"

    @property
    def label(self) -> str:
        return f"{self.city}, {self.state}" if self.state else self.city

    def with_radius(self, radius_m: int | None) -> "Region":
        if radius_m is None:
            return self
        return replace(self, radius_m=radius_m)

    @classmethod
    def from_dict(cls, payload: dict) -> "Region":
        bbox = payload.get("bbox")
        if not isinstance(bbox, Sequence) or len(bbox) != 4:
            raise ValueError("region bbox must be [south, west, north, east]")
        return cls(
            id=str(payload["id"]),
            city=str(payload["city"]),
            state=str(payload.get("state", "")),
            query=str(payload.get("query") or payload["city"]),
            lat=float(payload["lat"]),
            lng=float(payload["lng"]),
            radius_m=int(payload.get("radius_m", 20_000)),
            bbox=tuple(float(value) for value in bbox),  # type: ignore[arg-type]
            country=str(payload.get("country", "India")),
        )


HSR_REGION = Region(
    id="hsr-layout-bengaluru",
    city="HSR Layout, Bengaluru",
    state="Karnataka",
    query="HSR Layout Bengaluru",
    lat=12.9116,
    lng=77.6389,
    radius_m=3_000,
    bbox=(12.8950, 77.6250, 12.9250, 77.6530),
)

BENGALURU_REGION = Region(
    id="bengaluru",
    city="Bengaluru",
    state="Karnataka",
    query="Bengaluru Karnataka",
    lat=12.9716,
    lng=77.5946,
    radius_m=30_000,
    bbox=(12.80, 77.45, 13.15, 77.80),
)

INDIA_TECH_CITY_REGIONS: list[Region] = [
    BENGALURU_REGION,
    Region("hyderabad", "Hyderabad", "Telangana", "Hyderabad Telangana", 17.3850, 78.4867, 30_000, (17.20, 78.20, 17.65, 78.65)),
    Region("pune", "Pune", "Maharashtra", "Pune Maharashtra", 18.5204, 73.8567, 25_000, (18.40, 73.70, 18.70, 74.05)),
    Region("mumbai", "Mumbai", "Maharashtra", "Mumbai Maharashtra", 19.0760, 72.8777, 25_000, (18.85, 72.75, 19.30, 73.10)),
    Region("navi-mumbai", "Navi Mumbai", "Maharashtra", "Navi Mumbai Maharashtra", 19.0330, 73.0297, 18_000, (18.90, 72.95, 19.25, 73.20)),
    Region("chennai", "Chennai", "Tamil Nadu", "Chennai Tamil Nadu", 13.0827, 80.2707, 25_000, (12.85, 80.10, 13.25, 80.35)),
    Region("gurugram", "Gurugram", "Haryana", "Gurugram Haryana", 28.4595, 77.0266, 18_000, (28.35, 76.90, 28.55, 77.15)),
    Region("noida", "Noida", "Uttar Pradesh", "Noida Uttar Pradesh", 28.5355, 77.3910, 18_000, (28.45, 77.25, 28.65, 77.50)),
    Region("new-delhi", "New Delhi", "Delhi", "New Delhi Delhi", 28.6139, 77.2090, 22_000, (28.50, 77.05, 28.75, 77.35)),
    Region("kolkata", "Kolkata", "West Bengal", "Kolkata West Bengal", 22.5726, 88.3639, 22_000, (22.45, 88.20, 22.70, 88.50)),
    Region("ahmedabad", "Ahmedabad", "Gujarat", "Ahmedabad Gujarat", 23.0225, 72.5714, 20_000, (22.90, 72.45, 23.15, 72.75)),
    Region("gandhinagar", "Gandhinagar", "Gujarat", "Gandhinagar Gujarat", 23.2156, 72.6369, 12_000, (23.10, 72.55, 23.30, 72.75)),
    Region("kochi", "Kochi", "Kerala", "Kochi Kerala", 9.9312, 76.2673, 18_000, (9.85, 76.20, 10.10, 76.40)),
    Region("thiruvananthapuram", "Thiruvananthapuram", "Kerala", "Thiruvananthapuram Kerala", 8.5241, 76.9366, 15_000, (8.45, 76.85, 8.60, 77.05)),
    Region("chandigarh-mohali", "Chandigarh-Mohali", "Chandigarh/Punjab", "Chandigarh Mohali", 30.7333, 76.7794, 20_000, (30.62, 76.62, 30.85, 76.90)),
    Region("jaipur", "Jaipur", "Rajasthan", "Jaipur Rajasthan", 26.9124, 75.7873, 22_000, (26.75, 75.65, 27.05, 76.00)),
    Region("indore", "Indore", "Madhya Pradesh", "Indore Madhya Pradesh", 22.7196, 75.8577, 18_000, (22.60, 75.75, 22.85, 76.00)),
    Region("coimbatore", "Coimbatore", "Tamil Nadu", "Coimbatore Tamil Nadu", 11.0168, 76.9558, 18_000, (10.90, 76.85, 11.15, 77.10)),
    Region("bhubaneswar", "Bhubaneswar", "Odisha", "Bhubaneswar Odisha", 20.2961, 85.8245, 15_000, (20.20, 85.70, 20.40, 85.95)),
    Region("mysuru", "Mysuru", "Karnataka", "Mysuru Karnataka", 12.2958, 76.6394, 15_000, (12.20, 76.55, 12.40, 76.75)),
    Region("mangaluru", "Mangaluru", "Karnataka", "Mangaluru Karnataka", 12.9141, 74.8560, 15_000, (12.80, 74.75, 13.05, 74.95)),
    Region("nagpur", "Nagpur", "Maharashtra", "Nagpur Maharashtra", 21.1458, 79.0882, 18_000, (21.05, 78.95, 21.25, 79.20)),
    Region("visakhapatnam", "Visakhapatnam", "Andhra Pradesh", "Visakhapatnam Andhra Pradesh", 17.6868, 83.2185, 18_000, (17.60, 83.10, 17.85, 83.40)),
    Region("lucknow", "Lucknow", "Uttar Pradesh", "Lucknow Uttar Pradesh", 26.8467, 80.9462, 18_000, (26.75, 80.85, 27.00, 81.05)),
]

PRESETS: dict[str, list[Region]] = {
    "hsr": [HSR_REGION],
    "bengaluru": [BENGALURU_REGION],
    "india-tech-cities": INDIA_TECH_CITY_REGIONS,
    "india": INDIA_TECH_CITY_REGIONS,
}


def load_regions_file(path: str | Path) -> list[Region]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("regions file must be a JSON array")
    return [Region.from_dict(item) for item in payload]


def filter_regions(regions: Iterable[Region], filters: Iterable[str] | None) -> list[Region]:
    selected = list(regions)
    if not filters:
        return selected

    wanted = {value.lower().strip() for value in filters if value.strip()}
    output = []
    for region in selected:
        aliases = {
            region.id.lower(),
            region.city.lower(),
            region.label.lower(),
            region.query.lower(),
        }
        if aliases & wanted:
            output.append(region)

    missing = sorted(wanted - {alias for region in output for alias in {region.id.lower(), region.city.lower(), region.label.lower(), region.query.lower()}})
    if missing:
        available = ", ".join(region.id for region in selected)
        raise ValueError(f"unknown region(s): {', '.join(missing)}. Available: {available}")
    return output
