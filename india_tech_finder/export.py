from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable

from .models import Company


CSV_FIELDS = [
    "name",
    "tech_score",
    "confidence",
    "city",
    "region",
    "country",
    "address",
    "lat",
    "lng",
    "website",
    "phone",
    "categories",
    "sources",
    "source_ids",
    "notes",
]


def ensure_parent(path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def write_csv(companies: Iterable[Company], path: str | Path) -> Path:
    output = ensure_parent(path)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for company in companies:
            row = company.to_dict()
            row["categories"] = " | ".join(company.categories)
            row["sources"] = " | ".join(company.sources)
            row["source_ids"] = json.dumps(company.source_ids, ensure_ascii=False, sort_keys=True)
            writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})
    return output


def read_json(path: str | Path) -> list[Company]:
    input_path = Path(path)
    if not input_path.exists():
        return []
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    companies: list[Company] = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        allowed = Company.__dataclass_fields__.keys()
        values = {key: value for key, value in item.items() if key in allowed}
        companies.append(Company(**values))
    return companies


def write_json(companies: Iterable[Company], path: str | Path) -> Path:
    output = ensure_parent(path)
    with output.open("w", encoding="utf-8") as handle:
        json.dump([company.to_dict() for company in companies], handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return output
