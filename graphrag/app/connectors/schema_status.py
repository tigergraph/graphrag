"""Compare a connector schema proposal with the installed graph schema."""

from __future__ import annotations

from typing import Any

from common.db.schema_utils import SchemaProposal, read_existing_schema


def attribute_types(metadata: dict[str, Any]) -> dict[str, str]:
    attributes: dict[str, str] = {}
    primary_id = (metadata.get("PrimaryId") or {}).get("AttributeName")
    for attribute in metadata.get("Attributes") or []:
        name = attribute.get("AttributeName")
        if not name or name == primary_id:
            continue
        attribute_type = (
            (attribute.get("AttributeType") or {}).get("Name") or "STRING"
        )
        attributes[str(name).casefold()] = str(attribute_type).upper()
    return attributes


def edge_pairs(metadata: dict[str, Any]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    source = metadata.get("FromVertexTypeName")
    target = metadata.get("ToVertexTypeName")
    if source and target and source != "*" and target != "*":
        pairs.add((str(source).casefold(), str(target).casefold()))
    for pair in metadata.get("EdgePairs") or []:
        source = pair.get("From")
        target = pair.get("To")
        if source and target:
            pairs.add((str(source).casefold(), str(target).casefold()))
    return pairs


def evaluate_proposal(
    conn,
    proposal: SchemaProposal,
    extra_pairs: tuple[tuple[str, str, str], ...] = (),
) -> dict[str, Any]:
    """Return installed, incomplete, conflict, or not_initialized."""
    existing = read_existing_schema(conn)
    conflicts: list[str] = []
    missing_vertices: list[str] = []
    missing_edges: list[str] = []
    missing_pairs: list[str] = []
    missing_attributes: list[str] = []

    required_core = ("Document", "DocumentChunk", "Content")
    missing_core = [name for name in required_core if not existing.has_vertex(name)]
    required_core_edges = ("CONTAINS_ENTITY", "HAS_CHILD", "HAS_CONTENT")
    missing_core_edges = [
        name for name in required_core_edges if not existing.has_edge(name)
    ]
    if missing_core or missing_core_edges:
        return {
            "status": "not_initialized",
            "missing": {
                "core_vertices": missing_core,
                "core_edges": missing_core_edges,
            },
            "conflicts": [],
        }

    for vertex in proposal.vertices:
        if not existing.has_vertex(vertex.name):
            missing_vertices.append(vertex.name)
            continue
        metadata = conn.getVertexType(vertex.name) or {}
        actual = attribute_types(metadata)
        for attribute in vertex.attributes:
            actual_type = actual.get(attribute.name.casefold())
            if actual_type is None:
                missing_attributes.append(f"{vertex.name}.{attribute.name}")
            elif actual_type != attribute.type.upper():
                conflicts.append(
                    f"{vertex.name}.{attribute.name} must be "
                    f"{attribute.type.upper()}, found {actual_type}"
                )

    for edge in proposal.edges:
        if not existing.has_edge(edge.name):
            missing_edges.append(edge.name)
            continue
        metadata = conn.getEdgeType(edge.name) or {}
        if bool(metadata.get("IsDirected")) != edge.directed:
            expected = "directed" if edge.directed else "undirected"
            conflicts.append(f"{edge.name} must be {expected}")
        actual_attributes = attribute_types(metadata)
        for attribute in edge.attributes:
            actual_type = actual_attributes.get(attribute.name.casefold())
            if actual_type is None:
                missing_attributes.append(f"{edge.name}.{attribute.name}")
            elif actual_type != attribute.type.upper():
                conflicts.append(
                    f"{edge.name}.{attribute.name} must be "
                    f"{attribute.type.upper()}, found {actual_type}"
                )
        actual_pairs = edge_pairs(metadata)
        for source, target in edge.pairs:
            if (source.casefold(), target.casefold()) not in actual_pairs:
                missing_pairs.append(f"{edge.name}: {source} -> {target}")

    for edge_name, source, target in extra_pairs:
        if not existing.has_edge_pair(edge_name, source, target):
            missing_pairs.append(f"{edge_name}: {source} -> {target}")

    missing = {
        "vertices": missing_vertices,
        "edges": missing_edges,
        "pairs": missing_pairs,
        "attributes": missing_attributes,
    }
    if conflicts:
        status = "conflict"
    elif not any(missing.values()):
        status = "installed"
    elif any(existing.has_vertex(vertex.name) for vertex in proposal.vertices):
        status = "incomplete"
    else:
        status = "not_installed"
    return {"status": status, "missing": missing, "conflicts": conflicts}
