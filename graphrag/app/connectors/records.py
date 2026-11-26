"""Graph records shared by data-source connectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VertexRecord:
    vertex_type: str
    vertex_id: str
    attributes: dict[str, Any]


@dataclass(frozen=True)
class EdgeRecord:
    source_type: str
    source_id: str
    edge_type: str
    target_type: str
    target_id: str
    attributes: dict[str, Any] = field(default_factory=dict)
