from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .regions import Region, SearchPoint


DEFAULT_TECH_ZONES_PATH = Path(__file__).parent / "data" / "tech_zones.json"


@dataclass(frozen=True)
class TechZone:
    """Curated high-priority tech area inside a city/region."""

    region_id: str
    name: str
    query: str
    lat: float
    lng: float
    radius_m: int = 4000

    @classmethod
    def from_dict(cls, payload: dict) -> "TechZone":
        return cls(
            region_id=str(payload["region_id"]),
            name=str(payload["name"]),
            query=str(payload.get("query") or payload["name"]),
            lat=float(payload["lat"]),
            lng=float(payload["lng"]),
            radius_m=int(payload.get("radius_m", 4000)),
        )

    def search_point(self) -> SearchPoint:
        # The google_places source recognizes the zone: prefix and appends the
        # query text to each search query, e.g. "software company Whitefield".
        return (self.lat, self.lng, f"zone:{self.query}")


def load_tech_zones(path: str | Path | None = None) -> list[TechZone]:
    zones_path = Path(path) if path else DEFAULT_TECH_ZONES_PATH
    payload = json.loads(zones_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("tech zones file must be a JSON array")
    return [TechZone.from_dict(item) for item in payload]


def zones_by_region(zones: Iterable[TechZone]) -> dict[str, list[TechZone]]:
    grouped: dict[str, list[TechZone]] = {}
    for zone in zones:
        grouped.setdefault(zone.region_id, []).append(zone)
    return grouped


def tech_zone_points_for_region(
    region: Region,
    grouped_zones: dict[str, list[TechZone]],
) -> list[SearchPoint]:
    return [zone.search_point() for zone in grouped_zones.get(region.id, [])]
