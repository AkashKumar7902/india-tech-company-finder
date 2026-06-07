from __future__ import annotations

import csv
from pathlib import Path

from ..models import Company


def fetch_csv_seed(path: str | Path) -> list[Company]:
    """Load extra candidates from a user-maintained CSV seed file.

    Supported headers: name, address, lat, lng, website, phone, categories.
    Categories can be separated by | or comma.
    """
    companies: list[Company] = []
    with Path(path).open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            categories_text = row.get("categories") or ""
            delimiter = "|" if "|" in categories_text else ","
            categories = [part.strip() for part in categories_text.split(delimiter) if part.strip()]
            try:
                lat = float(row["lat"]) if row.get("lat") else None
                lng = float(row["lng"]) if row.get("lng") else None
            except ValueError:
                lat = None
                lng = None
            companies.append(
                Company(
                    name=name,
                    address=(row.get("address") or "").strip(),
                    lat=lat,
                    lng=lng,
                    website=(row.get("website") or "").strip(),
                    phone=(row.get("phone") or "").strip(),
                    categories=categories,
                    sources=["seed_csv"],
                    source_ids={"seed_csv": name},
                )
            )
    return companies
