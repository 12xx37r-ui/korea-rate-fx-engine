from __future__ import annotations
from dataclasses import dataclass, asdict, field
from typing import Any

@dataclass
class SourceResult:
    source: str
    status: str
    message: str = ""
    rows: int = 0
    latest_observation: str | None = None
    payload_path: str | None = None
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)
