from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Company:
    """Normalized company candidate returned by one or more sources."""

    name: str
    address: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None
    website: str = ""
    phone: str = ""
    categories: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    source_ids: Dict[str, str] = field(default_factory=dict)
    tech_score: float = 0.0
    confidence: str = ""
    notes: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
