"""TigerGraph upsert payloads shared by data-source connectors."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from connectors.records import EdgeRecord, VertexRecord


def _attrs(attributes: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        key: {"value": value}
        for key, value in attributes.items()
        if value is not None
    }


def upsert_payload(
    vertices: list[VertexRecord],
    edges: list[EdgeRecord],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "vertices": defaultdict(dict),
        "edges": defaultdict(
            lambda: defaultdict(
                lambda: defaultdict(lambda: defaultdict(dict))
            )
        ),
    }
    for vertex in vertices:
        payload["vertices"][vertex.vertex_type][vertex.vertex_id] = _attrs(
            vertex.attributes
        )
    for edge in edges:
        payload["edges"][edge.source_type][edge.source_id][edge.edge_type][
            edge.target_type
        ][edge.target_id] = _attrs(edge.attributes)
    return payload
