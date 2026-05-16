# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

"""
Schema proposal and persistence for the schema-aware initialize_graph flow.

A "schema proposal" is the user-supplied (or LLM-derived) **domain** schema
the graph should adopt at init time, expressed as a small Python dict:

    {
        "vertices": [
            {"name": "Company",  "description": "..."},
            {"name": "Report",   "description": "..."},
            ...
        ],
        "edges": [
            {"name": "PUBLISHES",
             "description": "...",
             "pairs": [("Company", "Report"), ("Company", "Filing")]},
            ...
        ],
        "domain_label": "Corporate Governance",   # optional
    }

This module provides:

* :data:`GRAPHRAG_STRUCTURAL_VERTEX_TYPES` / :data:`GRAPHRAG_STRUCTURAL_EDGE_TYPES`
  — the GraphRAG-internal types that the user must not redefine.
* :func:`parse_gsql_schema` — permissive scanner that turns pasted GSQL
  (``ADD VERTEX/EDGE`` statements *or* ``gsql ls`` output) into a proposal.
* :func:`emit_add_statements` — produce a list of ``ADD VERTEX/EDGE`` /
  ``ALTER EDGE … ADD PAIR`` statements that bring an existing graph schema
  up to the proposal (compare-and-add only; never drop).
* :func:`emit_preview_gsql` — render the proposal as a self-contained GSQL
  block for the UI's "Preview as GSQL" tab.

The module is intentionally dependency-light (regex, dataclasses, stdlib
only) so it's unit-testable without spinning up TigerGraph or the LLM.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Sequence, Set, Tuple


# -----------------------------------------------------------------------------
# Structural type registry
# -----------------------------------------------------------------------------

#: GraphRAG-internal vertex types. The user must not propose these as domain
#: types; the permissive parser silently drops any line that names one of
#: these (case-insensitive match).
GRAPHRAG_STRUCTURAL_VERTEX_TYPES: frozenset = frozenset({
    "Document",
    "DocumentChunk",
    "Entity",
    "EntityType",
    "RelationshipType",
    "Content",
    "Community",
    "Image",
})


#: GraphRAG-internal edge types. The user must not propose these as domain
#: types either. ``reverse_*`` companions are derived from ``WITH
#: REVERSE_EDGE=…`` declarations and shouldn't be hand-written.
GRAPHRAG_STRUCTURAL_EDGE_TYPES: frozenset = frozenset({
    "HAS_CONTENT",
    "IS_HEAD_OF",
    "HAS_TAIL",
    "CONTAINS_ENTITY",
    "MENTIONS_RELATIONSHIP",
    "MENTIONS_ENTITY_TYPE",
    "IS_AFTER",
    "HAS_CHILD",
    "ENTITY_HAS_TYPE",
    "RELATIONSHIP",
    "ENTITY_LINKS_TO",
    "IN_COMMUNITY",
    "LINKS_TO",
    "HAS_PARENT",
    "HAS_IMAGE",
    "REFERENCES_IMAGE",
})


# TigerGraph identifier pattern (graphs, jobs, vertex/edge types). Must
# match the route-level ``ValidGraphName`` regex so direct callers of
# the helpers below get the same protection the API layer enforces.
_GSQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


_GSQL_RESERVED_CACHE: Optional[frozenset] = None


def get_gsql_reserved_words() -> frozenset:
    """Return the GSQL reserved-keyword set sourced from
    ``pyTigerGraph.TigerGraphConnection.getReservedKeywords()``.

    Memoized at first call. ``pyTigerGraph`` is a hard dependency of
    this codebase, so an import failure here is a real configuration
    error and we let it propagate.
    """
    global _GSQL_RESERVED_CACHE
    if _GSQL_RESERVED_CACHE is None:
        from pyTigerGraph import TigerGraphConnection

        words = TigerGraphConnection.getReservedKeywords()
        _GSQL_RESERVED_CACHE = (
            words if isinstance(words, frozenset) else frozenset(words)
        )
    return _GSQL_RESERVED_CACHE


def is_reserved_word(name: str) -> bool:
    """Return True if *name* (case-insensitive) collides with a GSQL
    reserved word per pyTigerGraph. Used by the permissive parser to
    drop names that would error at schema-change time anyway.
    """
    if not name:
        return False
    return name.upper() in get_gsql_reserved_words()


#: Network / transport-level failure markers that ``conn.gsql()``
#: surfaces as a string return rather than an exception.
_GSQL_TRANSPORT_FAILURE_MARKERS: tuple = (
    "Response ended prematurely",
    "Connection refused",
    "Connection reset",
    "Read timed out",
    "Internal Server Error",
)


#: Server-reported failure markers that ``conn.gsql()`` includes in
#: its string output without raising. Maintained locally — the
#: pyTigerGraph private helper ``_wrap_gsql_result`` is documented as
#: in flux upstream, so we don't depend on it. Keep this list aligned
#: with upstream's ``_GSQL_ERROR_PATTERNS`` when it stabilizes.
_GSQL_SERVER_ERROR_MARKERS: tuple = (
    'Encountered "',
    "SEMANTIC ERROR",
    "Syntax Error",
    "Failed to create",
    "does not exist",
    "is not a valid",
    "already exists",
    "Invalid syntax",
)


def gsql_output_error(output: str) -> Optional[str]:
    """Return a short error description if *output* (the string returned
    by ``pyTigerGraph.TigerGraphConnection.gsql()``) indicates failure,
    else ``None``.

    Two layers, both checked locally so we don't depend on
    pyTigerGraph private helpers:

    1. Transport-level errors (``Response ended prematurely``,
       ``Connection refused``, etc.) — pyTigerGraph surfaces these as
       a string return rather than an exception.
    2. Server-reported errors (``SEMANTIC ERROR``, ``Failed to
       create``, ``Invalid syntax``, etc.) — string markers in the
       gsql output.

    Used by :func:`apply_proposal` to flip an "applied" return into
    an error when the server reported a problem but pyTigerGraph
    didn't raise.
    """
    if not output:
        return None

    folded = output.casefold()
    for marker in _GSQL_TRANSPORT_FAILURE_MARKERS:
        if marker.casefold() in folded:
            idx = output.lower().find(marker.lower())
            snippet = output[max(0, idx - 40): idx + len(marker) + 200]
            return f"GSQL transport error: {marker!r}. Excerpt: {snippet!r}"

    for marker in _GSQL_SERVER_ERROR_MARKERS:
        if marker in output:
            idx = output.find(marker)
            snippet = output[max(0, idx - 40): idx + len(marker) + 200]
            return f"GSQL server error: {snippet!r}"

    return None


def is_structural_type(name: str) -> bool:
    """Return True if *name* (case-insensitive) is a GraphRAG structural
    vertex or edge type, OR a ``reverse_*`` companion of one, OR a GSQL
    reserved word that would fail at schema-change time.
    """
    if not name:
        return False
    folded = name.casefold()
    if folded.startswith("reverse_"):
        return True
    structural = {t.casefold() for t in GRAPHRAG_STRUCTURAL_VERTEX_TYPES}
    structural |= {t.casefold() for t in GRAPHRAG_STRUCTURAL_EDGE_TYPES}
    if folded in structural:
        return True
    return is_reserved_word(name)


# -----------------------------------------------------------------------------
# Canonical proposal dataclass
# -----------------------------------------------------------------------------


#: TigerGraph GSQL primitive attribute types we accept on proposals.
#: Anything else is dropped at parse time so the schema-change job
#: never receives a non-primitive type.
GSQL_PRIMITIVE_TYPES: frozenset = frozenset({
    "STRING", "INT", "UINT", "DOUBLE", "FLOAT", "BOOL", "DATETIME",
})


@dataclass
class AttributeProposal:
    """One ``(name, type)`` pair on a vertex or edge type."""

    name: str
    type: str = "STRING"

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.type}


@dataclass
class VertexProposal:
    name: str
    description: str = ""
    attributes: List[AttributeProposal] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "attributes": [a.to_dict() for a in self.attributes],
        }


@dataclass
class EdgeProposal:
    name: str
    pairs: List[Tuple[str, str]] = field(default_factory=list)
    description: str = ""
    attributes: List[AttributeProposal] = field(default_factory=list)
    # ``True`` for ``DIRECTED EDGE`` (default), ``False`` for
    # ``UNDIRECTED EDGE``. Captured from the parser; propagated to the
    # emitter so the schema-change job uses the right keyword and
    # WITH-clause shape (undirected edges have no REVERSE_EDGE).
    directed: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "pairs": [list(p) for p in self.pairs],
            "attributes": [a.to_dict() for a in self.attributes],
            "directed": self.directed,
        }


@dataclass
class SchemaProposal:
    """Canonical in-memory representation of a domain schema proposal."""

    vertices: List[VertexProposal] = field(default_factory=list)
    edges: List[EdgeProposal] = field(default_factory=list)
    domain_label: Optional[str] = None

    # --- Construction helpers -----------------------------------------------

    def add_vertex(
        self,
        name: str,
        description: str = "",
        attributes: Optional[Iterable[Tuple[str, str]]] = None,
    ) -> VertexProposal:
        existing = self.find_vertex(name)
        if existing is not None:
            if description and not existing.description:
                existing.description = description
            if attributes:
                self._merge_attrs(existing.attributes, attributes)
            return existing
        v = VertexProposal(name=name, description=description)
        if attributes:
            self._merge_attrs(v.attributes, attributes)
        self.vertices.append(v)
        return v

    def add_edge_pair(
        self,
        name: str,
        from_vt: str,
        to_vt: str,
        description: str = "",
        attributes: Optional[Iterable[Tuple[str, str]]] = None,
        directed: bool = True,
    ) -> EdgeProposal:
        existing = self.find_edge(name)
        pair = (from_vt, to_vt)
        if existing is None:
            existing = EdgeProposal(
                name=name,
                pairs=[pair],
                description=description,
                directed=directed,
            )
            if attributes:
                self._merge_attrs(existing.attributes, attributes)
            self.edges.append(existing)
        else:
            if pair not in existing.pairs:
                existing.pairs.append(pair)
            if description and not existing.description:
                existing.description = description
            if attributes:
                self._merge_attrs(existing.attributes, attributes)
            # If the same edge name appears twice with mismatched
            # direction, prefer the first declaration's choice and
            # log nothing — schema-change time will reject anyway.
        return existing

    @staticmethod
    def _merge_attrs(
        target: List[AttributeProposal],
        new_attrs: Iterable[Tuple[str, str]],
    ) -> None:
        """Merge new ``(name, type)`` tuples into *target*. Ignores
        attributes whose name is already present (case-insensitive),
        so the first declared type wins. Filters out attributes whose
        type isn't a recognized GSQL primitive — those would error at
        schema-change time, and we drop silently to keep the parser
        permissive.
        """
        existing_names = {a.name.casefold() for a in target}
        for name, type_str in new_attrs:
            if not name:
                continue
            if name.casefold() in existing_names:
                continue
            if type_str.upper() not in GSQL_PRIMITIVE_TYPES:
                continue
            target.append(AttributeProposal(name=name, type=type_str.upper()))
            existing_names.add(name.casefold())

    # --- Lookup helpers -----------------------------------------------------

    def find_vertex(self, name: str) -> Optional[VertexProposal]:
        folded = name.casefold()
        return next(
            (v for v in self.vertices if v.name.casefold() == folded), None
        )

    def find_edge(self, name: str) -> Optional[EdgeProposal]:
        folded = name.casefold()
        return next(
            (e for e in self.edges if e.name.casefold() == folded), None
        )

    def vertex_names(self) -> Set[str]:
        return {v.name for v in self.vertices}

    # --- Cleanup ------------------------------------------------------------

    def drop_dangling_pairs(self) -> int:
        """Remove ``(FROM, TO)`` pairs whose endpoints aren't in the
        proposal's vertex set. Returns the number of pairs dropped.
        Edges whose pair list becomes empty are removed entirely.
        """
        names = {v.name for v in self.vertices}
        names_folded = {n.casefold() for n in names}
        dropped = 0
        kept_edges: List[EdgeProposal] = []
        for edge in self.edges:
            kept_pairs: List[Tuple[str, str]] = []
            for src, tgt in edge.pairs:
                if (
                    src.casefold() in names_folded
                    and tgt.casefold() in names_folded
                ):
                    kept_pairs.append((src, tgt))
                else:
                    dropped += 1
            if kept_pairs:
                edge.pairs = kept_pairs
                kept_edges.append(edge)
            else:
                dropped += 0  # whole edge dropped, not counted as a pair-drop
        self.edges = kept_edges
        return dropped

    # --- Serialization ------------------------------------------------------

    def to_dict(self) -> dict:
        out: dict = {
            "vertices": [v.to_dict() for v in self.vertices],
            "edges": [e.to_dict() for e in self.edges],
        }
        if self.domain_label:
            out["domain_label"] = self.domain_label
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "SchemaProposal":
        prop = cls(domain_label=data.get("domain_label"))
        for v in data.get("vertices", []) or []:
            attrs = [
                (a.get("name", ""), a.get("type", "STRING"))
                for a in v.get("attributes", []) or []
            ]
            prop.add_vertex(
                name=v["name"],
                description=v.get("description", ""),
                attributes=attrs,
            )
        for e in data.get("edges", []) or []:
            attrs = [
                (a.get("name", ""), a.get("type", "STRING"))
                for a in e.get("attributes", []) or []
            ]
            edge_directed = bool(e.get("directed", True))
            for pair in e.get("pairs", []) or []:
                prop.add_edge_pair(
                    name=e["name"],
                    from_vt=pair[0],
                    to_vt=pair[1],
                    description=e.get("description", ""),
                    attributes=attrs,
                    directed=edge_directed,
                )
        return prop


# -----------------------------------------------------------------------------
# Permissive GSQL parser
# -----------------------------------------------------------------------------


# A line that contains "VERTEX <Name>(...)" anywhere on it.
# Captures the name and (optionally) the parenthesized attribute list.
# Allows leading whitespace, optional dash, optional ADD prefix.
_VERTEX_LINE_RE = re.compile(
    r"""
    ^                                    # start of line (re.MULTILINE)
    [\s\-]*                              # leading whitespace, optional dash
    (?:add\s+)?                          # optional ADD
    vertex                               # VERTEX
    \s+
    (?P<name>[A-Za-z_][A-Za-z0-9_]*)     # type name
    \s*
    \(                                   # opening paren of attribute list
    (?P<body>[^()]*)                     # attribute body (no nested parens)
    \)                                   # closing paren
    """,
    re.IGNORECASE | re.VERBOSE | re.MULTILINE | re.DOTALL,
)


# A line that contains "DIRECTED EDGE <Name>(...)" or
# "UNDIRECTED EDGE <Name>(...)". Captures the direction keyword (so
# the parser can preserve it on the proposal), the edge name, and the
# FROM/TO body. Attribute / WITH-clause text after the closing paren
# is intentionally not captured.
_EDGE_LINE_RE = re.compile(
    r"""
    ^                                    # start of line
    [\s\-]*                              # leading whitespace, optional dash
    (?:add\s+)?                          # optional ADD
    (?P<dir>directed|undirected)         # DIRECTED or UNDIRECTED
    \s+edge
    \s+
    (?P<name>[A-Za-z_][A-Za-z0-9_]*)     # edge name
    \s*
    \(                                   # opening paren
    (?P<body>.*?)                        # FROM/TO body (non-greedy)
    \)                                   # closing paren
    """,
    re.IGNORECASE | re.VERBOSE | re.MULTILINE | re.DOTALL,
)


# Within an edge body, a single (FROM <V1>, TO <V2>) clause. Multi-pair
# bodies are separated by `|`.
_EDGE_PAIR_RE = re.compile(
    r"""
    \bfrom\s+
    (?P<from>[A-Za-z_][A-Za-z0-9_]*)
    \s*,\s*
    \bto\s+
    (?P<to>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.IGNORECASE | re.VERBOSE,
)


# A single ``name <PRIMITIVE_TYPE>`` token in an attribute body.
_ATTR_TOKEN_RE = re.compile(
    r"""
    \b
    (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    \s+
    (?P<type>STRING|INT|UINT|DOUBLE|FLOAT|BOOL|DATETIME)
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)


# Strip ``PRIMARY_ID <id_name> <PRIMITIVE_TYPE>`` so the attribute
# scanner doesn't collect the id field. The system always auto-adds
# ``PRIMARY_ID id STRING``; user-supplied values are honored only if
# they appear as the literal PRIMARY_ID — otherwise they're treated
# as plain attributes.
_PRIMARY_ID_RE = re.compile(
    r"\bPRIMARY_ID\b\s+[A-Za-z_][A-Za-z0-9_]*\s+(?:STRING|INT|UINT|DOUBLE|FLOAT|BOOL|DATETIME)",
    re.IGNORECASE,
)


# Strip a ``FROM <V>, TO <V>`` clause so the attribute scanner doesn't
# accidentally pick up "FROM" / "TO" tokens or their vertex-type
# placeholders. Used when scanning edge attribute bodies.
_FROM_TO_CLAUSE_RE = re.compile(
    r"\bfrom\s+[A-Za-z_][A-Za-z0-9_]*\s*,\s*to\s+[A-Za-z_][A-Za-z0-9_]*",
    re.IGNORECASE,
)


def _extract_attributes(body: str, *, is_edge_body: bool) -> List[Tuple[str, str]]:
    """Scan an attribute body and return ``(name, type)`` pairs that
    look like primitive attribute declarations. Skips ``PRIMARY_ID``
    entries (the system auto-adds those) and, for edge bodies, FROM/TO
    pair clauses.
    """
    if not body:
        return []
    cleaned = _PRIMARY_ID_RE.sub("", body)
    if is_edge_body:
        cleaned = _FROM_TO_CLAUSE_RE.sub("", cleaned)
    seen: Set[str] = set()
    out: List[Tuple[str, str]] = []
    for m in _ATTR_TOKEN_RE.finditer(cleaned):
        name = m.group("name")
        type_str = m.group("type").upper()
        folded = name.casefold()
        if folded in seen:
            continue
        # Skip GSQL keywords that may slip through (FROM/TO already
        # stripped, but be defensive against other reserved tokens).
        if folded in {"from", "to", "primary_id"}:
            continue
        # Drop attribute names that collide with GSQL reserved words
        # (e.g. ``count``, ``min``, ``max``). The schema-change job
        # would otherwise fail with "Encountered ',' ..." when TG's
        # parser reads the keyword in attribute position.
        if is_reserved_word(name):
            continue
        seen.add(folded)
        out.append((name, type_str))
    return out


# Matches a comment block:
#   * one or more `// ...` lines, or
#   * a single `/* ... */` block.
# Used to find descriptions immediately preceding a VERTEX/EDGE line.
_COMMENT_BLOCK_RE = re.compile(
    r"""
    (?:
        (?:^[ \t]*//[ \t]?(?P<line>.*)$\n?)+   # one+ // lines
        |
        ^[ \t]*/\*(?P<block>.*?)\*/[ \t]*\n    # /* … */ block
    )
    """,
    re.MULTILINE | re.DOTALL | re.VERBOSE,
)


def _extract_description_for(text: str, decl_start: int) -> str:
    """Return the comment block's text immediately preceding *decl_start*
    in *text*, or an empty string if none is present.

    A comment block is one or more consecutive ``//`` line comments, or a
    single ``/* … */`` block, separated from the declaration by at most
    blank lines.
    """
    # Walk backwards from decl_start over blank/whitespace-only lines.
    cursor = decl_start
    # Skip any whitespace immediately before the decl
    while cursor > 0 and text[cursor - 1] in (" ", "\t"):
        cursor -= 1
    # Walk back one or more blank lines
    while cursor > 0 and text[cursor - 1] == "\n":
        # Look at the line before this newline
        prev_line_end = cursor - 1
        prev_line_start = text.rfind("\n", 0, prev_line_end) + 1
        prev_line = text[prev_line_start:prev_line_end]
        if prev_line.strip() == "":
            cursor = prev_line_start
            continue
        break

    # Now cursor points at the start of the line that's potentially a comment.
    # Walk back over consecutive `//` lines collecting their bodies.
    comment_lines: List[str] = []
    while cursor > 0:
        line_end = cursor - 1  # newline before cursor
        line_start = text.rfind("\n", 0, line_end) + 1
        line = text[line_start:line_end]
        stripped = line.lstrip()
        if stripped.startswith("//"):
            comment_lines.insert(0, stripped[2:].lstrip())
            cursor = line_start
        elif stripped.startswith("/*") or stripped.endswith("*/"):
            # Try a /* … */ block ending on this line
            block_end = text.rfind("*/", 0, cursor)
            block_start = text.rfind("/*", 0, block_end)
            if block_start == -1 or block_end == -1:
                break
            body = text[block_start + 2:block_end]
            # Strip leading * on each line (typical /* * … */ style)
            cleaned = re.sub(r"^\s*\*?\s?", "", body, flags=re.MULTILINE)
            comment_lines.insert(0, cleaned.strip())
            break
        else:
            break

    return " ".join(s.strip() for s in comment_lines if s.strip())


def parse_gsql_schema(text: str) -> SchemaProposal:
    """Permissively scan *text* for ``VERTEX`` / ``DIRECTED EDGE``
    declarations and return a :class:`SchemaProposal`.

    The scanner ignores everything that doesn't match the two declaration
    patterns: section headers (``Vertex Types:``, ``Edge Types:``),
    ``Indexes:``, ``Queries:`` blocks, ``CREATE GRAPH`` /
    ``INSTALL QUERY`` / ``ALTER`` lines, blank lines, etc. ``ADD``
    prefix and the ``- `` bullet from ``gsql ls`` output are both
    accepted; ``;`` terminators are tolerated.

    Lines naming a structural type (case-insensitive) are silently dropped.
    ``reverse_*`` edges (auto-generated by ``WITH REVERSE_EDGE=…``) are
    silently dropped. ``(FROM, TO)`` pairs whose endpoints don't resolve
    to a vertex extracted from the same payload are dropped after parsing
    (see :meth:`SchemaProposal.drop_dangling_pairs`).
    """
    proposal = SchemaProposal()

    # Pass 1: vertices
    for m in _VERTEX_LINE_RE.finditer(text):
        name = m.group("name")
        if is_structural_type(name):
            continue
        desc = _extract_description_for(text, m.start())
        attrs = _extract_attributes(m.group("body") or "", is_edge_body=False)
        proposal.add_vertex(name=name, description=desc, attributes=attrs)

    # Pass 2: edges
    for m in _EDGE_LINE_RE.finditer(text):
        name = m.group("name")
        if is_structural_type(name):
            continue
        if name.lower().startswith("reverse_"):
            continue
        body = m.group("body") or ""
        desc = _extract_description_for(text, m.start())
        attrs = _extract_attributes(body, is_edge_body=True)
        directed = (m.group("dir") or "directed").lower() == "directed"
        for pm in _EDGE_PAIR_RE.finditer(body):
            from_vt = pm.group("from")
            to_vt = pm.group("to")
            if is_structural_type(from_vt) or is_structural_type(to_vt):
                # Either endpoint is a structural type, a reverse_*
                # auto-generated companion, or a GSQL reserved word —
                # the pair would be invalid as a user-declared domain
                # edge. ``drop_dangling_pairs`` would catch it later
                # anyway; rejecting here keeps the proposal free of
                # transient invalid state.
                continue
            proposal.add_edge_pair(
                name=name,
                from_vt=from_vt,
                to_vt=to_vt,
                description=desc,
                attributes=attrs,
                directed=directed,
            )

    # Filter dangling pairs (FROM/TO that don't resolve to a vertex we
    # actually extracted from the same payload).
    proposal.drop_dangling_pairs()
    return proposal


# -----------------------------------------------------------------------------
# GSQL emission
# -----------------------------------------------------------------------------


@dataclass
class ExistingSchema:
    """Snapshot of what's already on the graph, used by the diff emitter.

    ``vertex_types`` — vertex-type names currently on the graph.
    ``edge_pairs`` — edge-type name → set of ``(FROM, TO)`` pairs.
    ``directed_edges`` — subset of edge-type names with
    ``IsDirected=True`` (consumed by the retriever renderer).
    """

    vertex_types: Set[str] = field(default_factory=set)
    edge_pairs: dict = field(default_factory=dict)
    directed_edges: Set[str] = field(default_factory=set)

    def has_vertex(self, name: str) -> bool:
        folded = name.casefold()
        return any(v.casefold() == folded for v in self.vertex_types)

    def has_edge(self, name: str) -> bool:
        return name in self.edge_pairs or any(
            k.casefold() == name.casefold() for k in self.edge_pairs
        )

    def has_edge_pair(self, name: str, from_vt: str, to_vt: str) -> bool:
        # Edge name lookup is case-insensitive
        edge_key = next(
            (k for k in self.edge_pairs if k.casefold() == name.casefold()),
            None,
        )
        if edge_key is None:
            return False
        for src, tgt in self.edge_pairs.get(edge_key, set()):
            if src.casefold() == from_vt.casefold() and tgt.casefold() == to_vt.casefold():
                return True
        return False


@dataclass
class AllowedSchema:
    """Domain-schema bundle handed to the LLM entity/relationship
    extractor. Carries one text rendering for the LLM prompt and the
    structured maps the worker layer uses for runtime coercion and
    endpoint validation.

    All fields exclude GraphRAG structural types — only user-declared
    domain types reach the extractor.

    Fields:
        schema_rep — rendered schema text suitable for an LLM prompt
            (vertex types with attributes, edge types with endpoints,
            inline definitions). Reuses the same shape that
            ``render_schema_rep`` produces for query-side tools.
        vertex_types / edge_types — name lists for fast allow-checks.
        vertex_attributes / edge_attributes — ``{type: {attr: tg_type}}``
            for typed-attribute coercion at upsert time.
        vertex_definitions / edge_definitions — ``{type: description}``
            from EntityType / RelationshipType meta-vertices.
        edge_endpoints — ``{edge: [(from_vt, to_vt), ...]}`` for the
            worker's endpoint-pair validation.
    """

    schema_rep: str = ""
    schema_version: Optional[int] = None
    vertex_types: List[str] = field(default_factory=list)
    edge_types: List[str] = field(default_factory=list)
    vertex_attributes: dict = field(default_factory=dict)
    edge_attributes: dict = field(default_factory=dict)
    vertex_definitions: dict = field(default_factory=dict)
    edge_definitions: dict = field(default_factory=dict)
    edge_endpoints: dict = field(default_factory=dict)


# TG accepts only these types inside a DISCRIMINATOR(...) clause.
# DOUBLE / FLOAT / BOOL are rejected at schema-change time.
_DISCRIMINATOR_TYPES = frozenset({"INT", "UINT", "STRING", "DATETIME"})


def _default_literal(tg_type: str) -> str:
    """Return the GSQL literal for the per-type default — used inside
    ``DISCRIMINATOR(... DEFAULT <literal>)`` clauses so the column is
    non-nullable but per-instance upserts that omit the value still
    succeed (the omitted attribute falls to the default).
    """
    t = (tg_type or "").upper()
    if t in ("INT", "UINT"):
        return "0"
    if t in ("DOUBLE", "FLOAT"):
        return "0.0"
    if t == "BOOL":
        return "false"
    if t == "DATETIME":
        return '"1970-01-01 00:00:00"'
    return '""'


def emit_add_statements(
    proposal: SchemaProposal,
    existing: Optional[ExistingSchema] = None,
) -> List[str]:
    """Diff *proposal* against *existing* and return a list of GSQL
    statements (sans trailing ``;``) that, when run inside a
    ``SCHEMA_CHANGE JOB`` against a graph in the *existing* state, bring
    the graph up to the proposal.

    Order is deterministic and dependency-safe:

    1. ``ADD VERTEX <name> (PRIMARY_ID id STRING) WITH PRIMARY_ID_AS_ATTRIBUTE="true"``
       for every domain vertex type that doesn't already exist.
    2. ``ADD DIRECTED EDGE <name> (FROM <V1>, TO <V2> [| FROM …]) WITH REVERSE_EDGE="reverse_<name>"``
       for every domain edge type that doesn't exist on the graph at all.
    3. ``ALTER EDGE <name> ADD PAIR (FROM <V1>, TO <V2>)`` for every
       ``(FROM, TO)`` pair on an existing edge type that's missing.

    No ``DROP``s are ever emitted — the diff is strictly additive.
    """
    if existing is None:
        existing = ExistingSchema()

    stmts: List[str] = []

    # 1. New vertex types
    for v in proposal.vertices:
        if existing.has_vertex(v.name):
            continue
        attrs_part = ""
        if v.attributes:
            attrs_part = ", " + ", ".join(
                f"{a.name} {a.type}" for a in v.attributes
            )
        stmts.append(
            f'ADD VERTEX {v.name} (PRIMARY_ID id STRING{attrs_part}) '
            f'WITH PRIMARY_ID_AS_ATTRIBUTE="true"'
        )

    # 2 + 3. Edges: fully new, or new pairs on an existing edge
    for e in proposal.edges:
        if not e.pairs:
            continue
        if not existing.has_edge(e.name):
            pairs_str = " | ".join(
                f"FROM {src}, TO {tgt}" for src, tgt in e.pairs
            )
            # Promote discriminator-eligible attributes (per TG: INT,
            # UINT, STRING, DATETIME) into a ``DISCRIMINATOR(...)``
            # clause with type defaults. Other attribute types stay as
            # regular nullable columns outside the clause.
            disc_attrs = [a for a in e.attributes if a.type.upper() in _DISCRIMINATOR_TYPES]
            plain_attrs = [a for a in e.attributes if a.type.upper() not in _DISCRIMINATOR_TYPES]
            parts: List[str] = []
            if disc_attrs:
                parts.append("DISCRIMINATOR(" + ", ".join(
                    f"{a.name} {a.type} DEFAULT {_default_literal(a.type)}"
                    for a in disc_attrs
                ) + ")")
            parts.extend(f"{a.name} {a.type}" for a in plain_attrs)
            attrs_part = (", " + ", ".join(parts)) if parts else ""
            edge_kw = "DIRECTED EDGE" if e.directed else "UNDIRECTED EDGE"
            # Undirected edges have no reverse companion, so omit the
            # WITH REVERSE_EDGE clause.
            with_clause = (
                f' WITH REVERSE_EDGE="reverse_{e.name}"' if e.directed else ""
            )
            stmts.append(
                f'ADD {edge_kw} {e.name} ({pairs_str}{attrs_part}){with_clause}'
            )
        else:
            # Existing edge: only ALTER ADD PAIR is supported here.
            # Adding attributes on an existing edge needs a separate
            # ALTER ATTRIBUTE statement and is out of scope for this
            # additive diff.
            for src, tgt in e.pairs:
                if existing.has_edge_pair(e.name, src, tgt):
                    continue
                stmts.append(
                    f"ALTER EDGE {e.name} ADD PAIR (FROM {src}, TO {tgt})"
                )

    return stmts


def emit_structural_link_alters(
    proposal: SchemaProposal,
    existing: Optional[ExistingSchema] = None,
) -> List[str]:
    """For every domain vertex in *proposal*, emit ``ALTER EDGE … ADD
    PAIR`` statements that connect it to the GraphRAG core via the
    structural edges:

    * ``CONTAINS_ENTITY`` — ``Document`` / ``DocumentChunk`` → domain vertex
    * ``IN_COMMUNITY`` — domain vertex → ``Community`` (so the
      post-Louvain mirror step can attach domain-VT instances to the
      community their twin Entity belongs to, and retrievers walking
      domain VTs can reach community memberships directly)

    The typed-relationship pattern (``IS_HEAD_OF`` / ``HAS_TAIL``) lives
    at the meta-schema layer (``EntityType`` ↔ ``RelationshipType``) and
    does NOT need per-domain-vertex pairs. The original schema
    declaration covers the only pairs we ever traverse.

    Pairs already on the graph (per *existing*) are skipped. The
    statements are returned in a deterministic order so the schema
    diff is reproducible.
    """
    if existing is None:
        existing = ExistingSchema()

    # Skip the structural-link emit entirely when the GraphRAG core
    # types aren't on the graph — without them the ALTER would
    # reference an undeclared endpoint and fail. In production these
    # are always present by the time apply_proposal runs (init_supportai
    # creates the structural schema first), but unit tests and
    # bare-graph fixtures may not have them.
    has_doc = existing.has_vertex("Document")
    has_chunk = existing.has_vertex("DocumentChunk")
    has_community = existing.has_vertex("Community")

    stmts: List[str] = []
    for v in proposal.vertices:
        # CONTAINS_ENTITY: Document / DocumentChunk → <vt>
        if has_doc and not existing.has_edge_pair("CONTAINS_ENTITY", "Document", v.name):
            stmts.append(
                f"ALTER EDGE CONTAINS_ENTITY ADD PAIR (FROM Document, TO {v.name})"
            )
        if has_chunk and not existing.has_edge_pair("CONTAINS_ENTITY", "DocumentChunk", v.name):
            stmts.append(
                f"ALTER EDGE CONTAINS_ENTITY ADD PAIR (FROM DocumentChunk, TO {v.name})"
            )
        # IN_COMMUNITY: <vt> → Community
        if has_community and not existing.has_edge_pair("IN_COMMUNITY", v.name, "Community"):
            stmts.append(
                f"ALTER EDGE IN_COMMUNITY ADD PAIR (FROM {v.name}, TO Community)"
            )
    return stmts


def emit_preview_gsql(proposal: SchemaProposal) -> str:
    """Render *proposal* as a self-contained GSQL block suitable for the
    UI's "Preview as GSQL" tab. Comments above each declaration carry
    the description, when set.
    """
    lines: List[str] = []
    if proposal.domain_label:
        lines.append(f"// Domain: {proposal.domain_label}")
        lines.append("")

    for v in proposal.vertices:
        if v.description:
            lines.append(f"// {v.description}")
        attrs_part = ""
        if v.attributes:
            attrs_part = ", " + ", ".join(
                f"{a.name} {a.type}" for a in v.attributes
            )
        lines.append(
            f'ADD VERTEX {v.name} (PRIMARY_ID id STRING{attrs_part}) '
            f'WITH PRIMARY_ID_AS_ATTRIBUTE="true";'
        )
        lines.append("")

    for e in proposal.edges:
        if not e.pairs:
            continue
        if e.description:
            lines.append(f"// {e.description}")
        pairs_str = " | ".join(f"FROM {src}, TO {tgt}" for src, tgt in e.pairs)
        attrs_part = ""
        if e.attributes:
            attrs_part = ", " + ", ".join(
                f"{a.name} {a.type}" for a in e.attributes
            )
        edge_kw = "DIRECTED EDGE" if e.directed else "UNDIRECTED EDGE"
        with_clause = (
            f' WITH REVERSE_EDGE="reverse_{e.name}"' if e.directed else ""
        )
        lines.append(
            f'ADD {edge_kw} {e.name} ({pairs_str}{attrs_part}){with_clause};'
        )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# -----------------------------------------------------------------------------
# TigerGraph-side schema reader
# -----------------------------------------------------------------------------


def read_existing_schema(conn) -> ExistingSchema:
    """Read the current vertex / edge schema from a TigerGraph
    connection and return an :class:`ExistingSchema` snapshot suitable
    for :func:`emit_add_statements`.

    Works with both ``pyTigerGraph.TigerGraphConnection`` and our
    ``TigerGraphConnectionProxy`` wrapper. Only the synchronous
    ``getVertexTypes`` / ``getEdgeTypes`` / ``getEdgeType`` API is used.

    Edge pairs are extracted from the edge-type metadata returned by
    pyTigerGraph. For single-pair edges the metadata exposes
    ``FromVertexTypeName`` / ``ToVertexTypeName`` directly. For
    multi-pair edges (where those fields are ``"*"``) the metadata
    contains an ``EdgePairs`` list of ``{"From": ..., "To": ...}``
    dicts. We accept both shapes.

    Errors during schema introspection are not swallowed — the caller
    needs to know if the snapshot is incomplete before diffing. If the
    graph hasn't been initialized at all (no vertex types yet),
    pyTigerGraph returns an empty list, which produces an
    ``ExistingSchema`` with empty ``vertex_types`` / ``edge_pairs``
    (the diff emitter then emits a full ``ADD`` for everything in the
    proposal — which is the desired behavior on a fresh graph).
    """
    snapshot = ExistingSchema()

    vertex_types = conn.getVertexTypes() or []
    snapshot.vertex_types = set(vertex_types)

    for et_name in conn.getEdgeTypes() or []:
        meta = conn.getEdgeType(et_name) or {}
        pairs: Set[Tuple[str, str]] = set()

        from_v = meta.get("FromVertexTypeName")
        to_v = meta.get("ToVertexTypeName")
        if from_v and to_v and from_v != "*" and to_v != "*":
            pairs.add((from_v, to_v))

        # Multi-pair edges: an EdgePairs list either always (some TG
        # versions) or only when From/To are "*" (other versions).
        for ep in meta.get("EdgePairs", []) or []:
            f = ep.get("From")
            t = ep.get("To")
            if f and t:
                pairs.add((f, t))

        if pairs:
            snapshot.edge_pairs[et_name] = pairs
        if meta.get("IsDirected"):
            snapshot.directed_edges.add(et_name)

    return snapshot


# -----------------------------------------------------------------------------
# Atomic apply
# -----------------------------------------------------------------------------


def build_schema_change_job(
    graphname: str,
    statements: Sequence[str],
    job_name: Optional[str] = None,
) -> Tuple[str, str]:
    """Wrap *statements* into a single ``CREATE SCHEMA_CHANGE JOB`` /
    ``RUN`` / ``DROP`` GSQL block for *graphname*.

    Returns ``(gsql_block, job_name)``. The job name is generated with a
    short uuid suffix so re-runs against the same graph don't collide
    with a previously-created (but never dropped) job.

    The returned block is intended to be passed verbatim to
    ``conn.gsql(...)``; running every ``ADD`` / ``ALTER`` inside one job
    is what makes the application atomic.
    """
    if not statements:
        raise ValueError("build_schema_change_job: statements is empty")
    if not _GSQL_IDENT_RE.fullmatch(graphname):
        raise ValueError(f"Invalid graph name: {graphname!r}")
    if job_name is None:
        job_name = f"add_domain_schema_{uuid.uuid4().hex[:8]}"
    elif not _GSQL_IDENT_RE.fullmatch(job_name):
        raise ValueError(f"Invalid job name: {job_name!r}")

    body = ";\n  ".join(s.rstrip(";") for s in statements) + ";"
    block = (
        f"USE GRAPH {graphname}\n"
        f"CREATE SCHEMA_CHANGE JOB {job_name} FOR GRAPH {graphname} {{\n"
        f"  {body}\n"
        f"}}\n"
        f"RUN SCHEMA_CHANGE JOB {job_name}\n"
        f"DROP JOB {job_name}"
    )
    return block, job_name


def read_type_metadata(conn) -> Tuple[dict, dict]:
    """Read every ``EntityType`` / ``RelationshipType`` vertex from
    *conn* and return two dicts:

        (
            {entity_type_id: description},
            {relationship_type_id: definition},
        )

    Empty / missing values are dropped so callers can ``.get(name, "")``
    without distinguishing "no row" from "row with empty description".
    Errors propagate — callers needing best-effort behavior should wrap
    in their own try/except.
    """
    entity_descs: dict = {}
    rel_defs: dict = {}

    try:
        rows = conn.getVertices("EntityType") or []
    except Exception:
        rows = []
    for row in rows:
        attrs = row.get("attributes", row)
        v_id = row.get("v_id") or attrs.get("id")
        desc = (attrs.get("description") or "").strip()
        if v_id and desc:
            entity_descs[v_id] = desc

    try:
        rows = conn.getVertices("RelationshipType") or []
    except Exception:
        rows = []
    for row in rows:
        attrs = row.get("attributes", row)
        v_id = row.get("v_id") or attrs.get("id")
        defn = (attrs.get("definition") or "").strip()
        if v_id and defn:
            rel_defs[v_id] = defn

    return entity_descs, rel_defs


async def read_existing_schema_async(conn) -> "ExistingSchema":
    """Async counterpart to :func:`read_existing_schema` — used by the
    ECC pipeline where ``conn`` is an ``AsyncTigerGraphConnection``.

    Returns a raw :class:`ExistingSchema` snapshot. Callers that want
    to filter structural types (e.g. for domain-only consumers like
    the extractor builder) should use :func:`is_structural_type` on
    the returned vertex / edge names — this helper deliberately does
    not couple the live-schema read to the proposal-time concept of
    "domain vs structural", so live-schema consumers stay independent
    of the proposal lifecycle.
    """
    snapshot = ExistingSchema()
    snapshot.vertex_types = set(await conn.getVertexTypes() or [])
    for et_name in await conn.getEdgeTypes() or []:
        meta = await conn.getEdgeType(et_name) or {}
        pairs: Set[Tuple[str, str]] = set()
        from_v = meta.get("FromVertexTypeName")
        to_v = meta.get("ToVertexTypeName")
        if from_v and to_v and from_v != "*" and to_v != "*":
            pairs.add((from_v, to_v))
        for ep in meta.get("EdgePairs", []) or []:
            f = ep.get("From")
            t = ep.get("To")
            if f and t:
                pairs.add((f, t))
        if pairs:
            snapshot.edge_pairs[et_name] = pairs
        if meta.get("IsDirected"):
            snapshot.directed_edges.add(et_name)
    return snapshot


def _assemble_schema_rep(
    *,
    graphname: str,
    schema_ver: Optional[int],
    vertex_blocks: List[str],
    edge_blocks: List[str],
    exclude_structural: bool,
    domain_verts: List[str],
    domain_edge_types: List[str],
    vertex_attributes: dict,
    edge_attributes: dict,
    entity_descs: dict,
    rel_defs: dict,
    edge_endpoints: dict,
) -> AllowedSchema:
    """Bundle pre-computed blocks into an ``AllowedSchema``. Shared by
    the sync and async builders so both paths produce identical output.
    """
    if exclude_structural and not domain_verts and not domain_edge_types:
        return AllowedSchema(schema_version=schema_ver)
    graph_label = f" {graphname}" if graphname else ""
    qualifier = "domain " if exclude_structural else ""
    text = (
        f"The {qualifier}schema of the graph{graph_label} is as follows:\n"
        f"Vertex Types:\n{chr(10).join(vertex_blocks) if vertex_blocks else '(none)'}"
        f"\n\nEdge Types:\n{chr(10).join(edge_blocks) if edge_blocks else '(none)'}\n"
    )
    domain_entity_defs = {v: entity_descs[v] for v in domain_verts if entity_descs.get(v)}
    domain_rel_defs = {e: rel_defs[e] for e in domain_edge_types if rel_defs.get(e)}
    return AllowedSchema(
        schema_rep=text,
        schema_version=schema_ver,
        vertex_types=domain_verts,
        edge_types=domain_edge_types,
        vertex_attributes=vertex_attributes,
        edge_attributes=edge_attributes,
        vertex_definitions=domain_entity_defs,
        edge_definitions=domain_rel_defs,
        edge_endpoints=edge_endpoints,
    )


def render_schema_rep(conn, exclude_structural: bool = False) -> AllowedSchema:
    """Read the live schema and return a full :class:`AllowedSchema`
    bundle (rendered text + structured maps + version).

    Used by both query-side tools (``generate_cypher`` / ``generate_gsql``
    / ``map_question_to_schema``) and the ECC entity extractor. Pass
    ``exclude_structural=True`` to drop GraphRAG structural types
    (Entity, Document, Community, structural edges, etc.) — the
    extractor uses this mode; query-side tools use the default so the
    LLM sees the full graph including bookkeeping types.

    Returns an :class:`AllowedSchema` with at least ``schema_version``
    populated; when the graph has no types yet, the other fields stay
    empty.
    """
    from common.db.connections import get_schema_ver as _get_schema_ver
    schema_ver = _get_schema_ver(conn)

    try:
        entity_descs, rel_defs = read_type_metadata(conn)
    except Exception:
        # Older / unmigrated graphs may lack the EntityType /
        # RelationshipType meta-schema; render without definitions
        # rather than failing.
        entity_descs, rel_defs = {}, {}

    try:
        all_verts = conn.getVertexTypes() or []
    except Exception:
        all_verts = []
    domain_verts = (
        [v for v in all_verts if not is_structural_type(v)]
        if exclude_structural else list(all_verts)
    )

    vertex_attributes: dict = {}
    vertex_blocks: List[str] = []
    for vert in sorted(domain_verts):
        try:
            vinfo = conn.getVertexType(vert) or {}
        except Exception:
            continue
        primary_id_name = (vinfo.get("PrimaryId") or {}).get("AttributeName", "")
        attrs_map, attr_lines = _collect_attrs(vinfo.get("Attributes"), primary_id_name)
        vertex_attributes[vert] = attrs_map
        defn_line = (
            f"\n\tDefinition: {entity_descs[vert]}" if entity_descs.get(vert) else ""
        )
        attrs_block = "\n\t\t".join(attr_lines) or "No attributes"
        vertex_blocks.append(
            f"{vert}{defn_line}\n\tPrimary Id Attribute: {primary_id_name}"
            f"\n\tAttributes: \n\t\t{attrs_block}"
        )

    try:
        all_edges = conn.getEdgeTypes() or []
    except Exception:
        all_edges = []
    edge_attributes: dict = {}
    edge_endpoints: dict = {}
    edge_blocks: List[str] = []
    domain_edge_types: List[str] = []
    for edge in sorted(all_edges):
        if exclude_structural and is_structural_type(edge):
            continue
        try:
            einfo = conn.getEdgeType(edge) or {}
        except Exception:
            continue
        pairs = _collect_edge_pairs(einfo, exclude_structural)
        if exclude_structural and not pairs:
            continue
        domain_edge_types.append(edge)
        edge_endpoints[edge] = pairs
        attrs_map, attr_lines = _collect_attrs(einfo.get("Attributes"), "")
        edge_attributes[edge] = attrs_map
        direction = "Directed" if einfo.get("IsDirected") else "Undirected"
        defn_line = (
            f"\n\tDefinition: {rel_defs[edge]}" if rel_defs.get(edge) else ""
        )
        attrs_block = "\n\t\t".join(attr_lines) or "No attributes"
        # Emit one block per (FROM, TO) pair — keeps the rendered
        # text single-pair-per-block.
        for src, tgt in pairs:
            pair_info = f"From Vertex: {src}\n\tTo Vertex: {tgt}"
            edge_blocks.append(
                f"{edge}{defn_line}\n\t{pair_info}"
                f"\n\tEdge direction: {direction}"
                f"\n\tAttributes: \n\t\t{attrs_block}"
            )

    return _assemble_schema_rep(
        graphname=getattr(conn, "graphname", "") or "",
        schema_ver=schema_ver,
        vertex_blocks=vertex_blocks,
        edge_blocks=edge_blocks,
        exclude_structural=exclude_structural,
        domain_verts=sorted(domain_verts) if exclude_structural else list(all_verts),
        domain_edge_types=domain_edge_types,
        vertex_attributes=vertex_attributes,
        edge_attributes=edge_attributes,
        entity_descs=entity_descs,
        rel_defs=rel_defs,
        edge_endpoints=edge_endpoints,
    )


def _collect_attrs(attr_list, skip_name: str) -> Tuple[dict, List[str]]:
    """Walk an ``Attributes`` array from ``getVertexType`` /
    ``getEdgeType`` and return ``({attr_name: tg_type}, ["name of type
    type", ...])``. ``skip_name`` is the primary-id attribute that
    shouldn't appear in the user-facing schema rep.
    """
    attrs_map: dict = {}
    lines: List[str] = []
    for a in attr_list or []:
        a_name = a.get("AttributeName")
        a_type = ((a.get("AttributeType") or {}).get("Name")) or "STRING"
        if not a_name or a_name == skip_name:
            continue
        attrs_map[a_name] = a_type
        lines.append(f"{a_name} of type {a_type}")
    return attrs_map, lines


def _collect_edge_pairs(einfo: dict, exclude_structural: bool) -> List[Tuple[str, str]]:
    """Build the (FROM, TO) pair list for an edge, filtering out pairs
    whose endpoint is a structural type when ``exclude_structural`` is
    set. Used by both schema-rep paths.
    """
    pairs: List[Tuple[str, str]] = []
    from_v = einfo.get("FromVertexTypeName")
    to_v = einfo.get("ToVertexTypeName")
    if from_v and to_v and from_v != "*" and to_v != "*":
        if not (exclude_structural and (is_structural_type(from_v) or is_structural_type(to_v))):
            pairs.append((from_v, to_v))
    for ep in einfo.get("EdgePairs", []) or []:
        f, t = ep.get("From"), ep.get("To")
        if not (f and t):
            continue
        if exclude_structural and (is_structural_type(f) or is_structural_type(t)):
            continue
        pairs.append((f, t))
    return pairs


# Backwards-compatible alias for callers that still want the old name.
# ``render_schema_rep(conn, exclude_structural=True)`` is the canonical
# spelling; keep this until call sites migrate.
def build_allowed_schema(conn) -> AllowedSchema:
    """Back-compat alias for ``render_schema_rep(conn, exclude_structural=True)``."""
    return render_schema_rep(conn, exclude_structural=True)


async def render_schema_rep_async(
    conn, exclude_structural: bool = False,
) -> AllowedSchema:
    """Async counterpart to :func:`render_schema_rep`. Used by the ECC
    pipeline where ``conn`` is an ``AsyncTigerGraphConnection`` (whose
    ``getVertexType`` / ``getEdgeType`` are coroutines).

    Same semantics as the sync version — see :func:`render_schema_rep`.
    """
    from common.db.connections import get_schema_ver as _get_schema_ver

    try:
        schema_ver = _get_schema_ver(conn)
    except Exception:
        schema_ver = None

    try:
        entity_descs, rel_defs = await read_type_metadata_async(conn)
    except Exception:
        entity_descs, rel_defs = {}, {}

    try:
        all_verts = await conn.getVertexTypes() or []
    except Exception:
        all_verts = []
    domain_verts = (
        [v for v in all_verts if not is_structural_type(v)]
        if exclude_structural else list(all_verts)
    )

    vertex_attributes: dict = {}
    vertex_blocks: List[str] = []
    for vert in sorted(domain_verts):
        try:
            vinfo = await conn.getVertexType(vert) or {}
        except Exception:
            continue
        primary_id_name = (vinfo.get("PrimaryId") or {}).get("AttributeName", "")
        attrs_map, attr_lines = _collect_attrs(vinfo.get("Attributes"), primary_id_name)
        vertex_attributes[vert] = attrs_map
        defn_line = (
            f"\n\tDefinition: {entity_descs[vert]}" if entity_descs.get(vert) else ""
        )
        attrs_block = "\n\t\t".join(attr_lines) or "No attributes"
        vertex_blocks.append(
            f"{vert}{defn_line}\n\tPrimary Id Attribute: {primary_id_name}"
            f"\n\tAttributes: \n\t\t{attrs_block}"
        )

    try:
        all_edges = await conn.getEdgeTypes() or []
    except Exception:
        all_edges = []
    edge_attributes: dict = {}
    edge_endpoints: dict = {}
    edge_blocks: List[str] = []
    domain_edge_types: List[str] = []
    for edge in sorted(all_edges):
        if exclude_structural and is_structural_type(edge):
            continue
        try:
            einfo = await conn.getEdgeType(edge) or {}
        except Exception:
            continue
        pairs = _collect_edge_pairs(einfo, exclude_structural)
        if exclude_structural and not pairs:
            continue
        domain_edge_types.append(edge)
        edge_endpoints[edge] = pairs
        attrs_map, attr_lines = _collect_attrs(einfo.get("Attributes"), "")
        edge_attributes[edge] = attrs_map
        direction = "Directed" if einfo.get("IsDirected") else "Undirected"
        defn_line = (
            f"\n\tDefinition: {rel_defs[edge]}" if rel_defs.get(edge) else ""
        )
        attrs_block = "\n\t\t".join(attr_lines) or "No attributes"
        for src, tgt in pairs:
            pair_info = f"From Vertex: {src}\n\tTo Vertex: {tgt}"
            edge_blocks.append(
                f"{edge}{defn_line}\n\t{pair_info}"
                f"\n\tEdge direction: {direction}"
                f"\n\tAttributes: \n\t\t{attrs_block}"
            )

    return _assemble_schema_rep(
        graphname=getattr(conn, "graphname", "") or "",
        schema_ver=schema_ver,
        vertex_blocks=vertex_blocks,
        edge_blocks=edge_blocks,
        exclude_structural=exclude_structural,
        domain_verts=sorted(domain_verts) if exclude_structural else list(all_verts),
        domain_edge_types=domain_edge_types,
        vertex_attributes=vertex_attributes,
        edge_attributes=edge_attributes,
        entity_descs=entity_descs,
        rel_defs=rel_defs,
        edge_endpoints=edge_endpoints,
    )


# Back-compat alias for the ECC pipeline.
async def build_allowed_schema_async(conn) -> AllowedSchema:
    """Back-compat alias for ``render_schema_rep_async(conn, exclude_structural=True)``."""
    return await render_schema_rep_async(conn, exclude_structural=True)


async def read_type_metadata_async(conn) -> Tuple[dict, dict]:
    """Async counterpart to :func:`read_type_metadata` — used by the
    ECC pipeline where the available connection is
    ``pyTigerGraph.AsyncTigerGraphConnection``.

    Same return shape: ``({entity_id: description}, {rel_id: definition})``.
    Errors propagate to the caller.
    """
    entity_descs: dict = {}
    rel_defs: dict = {}

    try:
        rows = await conn.getVertices("EntityType") or []
    except Exception:
        rows = []
    for row in rows:
        attrs = row.get("attributes", row)
        v_id = row.get("v_id") or attrs.get("id")
        desc = (attrs.get("description") or "").strip()
        if v_id and desc:
            entity_descs[v_id] = desc

    try:
        rows = await conn.getVertices("RelationshipType") or []
    except Exception:
        rows = []
    for row in rows:
        attrs = row.get("attributes", row)
        v_id = row.get("v_id") or attrs.get("id")
        defn = (attrs.get("definition") or "").strip()
        if v_id and defn:
            rel_defs[v_id] = defn

    return entity_descs, rel_defs


def _short_name(name: str) -> str:
    """Lowercase, underscore-separated form of *name* — used as the
    ``short_name`` attribute on ``RelationshipType`` vertices for display.
    Trims to at most ~32 characters (the column has no length but display
    is friendlier when short).
    """
    folded = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    return folded[:32]


def upsert_type_metadata(
    conn,
    proposal: SchemaProposal,
) -> dict:
    """Upsert ``EntityType`` / ``RelationshipType`` vertices with the
    descriptions from *proposal*. Does not touch existing rows whose
    description / definition is already non-empty unless the proposal
    carries a non-empty value of its own (callers may opt to override
    by passing a description; we always pass through what the proposal
    has).

    Returns ``{"entity_types": [...], "relationship_types": [...]}``
    listing the ids upserted.
    """
    now = int(time.time())
    entity_ids: List[str] = []
    relationship_ids: List[str] = []

    for v in proposal.vertices:
        # EntityType schema: (id STRING, description STRING, epoch_added UINT)
        attrs = {"epoch_added": now}
        if v.description:
            attrs["description"] = v.description
        conn.upsertVertex("EntityType", v.name, attributes=attrs)
        entity_ids.append(v.name)

    for e in proposal.edges:
        # RelationshipType schema:
        #   (id STRING, definition STRING, short_name STRING,
        #    epoch_added UINT, epoch_processing UINT, epoch_processed UINT)
        attrs = {
            "epoch_added": now,
            "short_name": _short_name(e.name),
        }
        if e.description:
            attrs["definition"] = e.description
        conn.upsertVertex("RelationshipType", e.name, attributes=attrs)
        relationship_ids.append(e.name)

    return {
        "entity_types": entity_ids,
        "relationship_types": relationship_ids,
    }


def apply_proposal(
    conn,
    graphname: str,
    proposal: SchemaProposal,
    job_name: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Diff *proposal* against the current schema on *conn* and apply the
    additive delta as a single atomic ``SCHEMA_CHANGE JOB``.

    Returns a result dict::

        {
            "status": "applied" | "no-op",
            "statements": [...],   # ADD/ALTER statements that were emitted
            "job_name": "<job name | None if no-op>",
            "gsql_output": "<raw gsql client output | ''>",
            "summary": {...},      # summarize(proposal)
        }

    *progress* is an optional callback invoked at each sub-phase with a
    short status string (e.g. ``"Creating new vertex/edge types"``,
    ``"Installing retriever queries"``); the router uses it to drive
    the init-dialog status line.

    Schema introspection errors propagate; the caller decides whether the
    overall init flow should be marked as failed. The structural GraphRAG
    schema must already exist on the graph (so the diff sees structural
    types and only emits domain-side ADDs).
    """
    def _report(msg: str) -> None:
        if progress is None:
            return
        try:
            progress(msg)
        except Exception:
            pass

    existing = read_existing_schema(conn)
    domain_stmts = emit_add_statements(proposal, existing)
    # Run the structural-link emitter against an *augmented* snapshot
    # so vertices we're about to ADD are treated as present — otherwise
    # has_edge_pair would always say "missing" and we'd over-emit.
    augmented = ExistingSchema(
        vertex_types=set(existing.vertex_types) | {v.name for v in proposal.vertices},
        edge_pairs=dict(existing.edge_pairs),
    )
    structural_stmts = emit_structural_link_alters(proposal, augmented)
    statements = domain_stmts + structural_stmts
    summary = summarize(proposal)

    if not statements:
        # Even on no-op, refresh metadata so descriptions edited in the
        # review panel land on EntityType / RelationshipType vertices.
        # The upsert is fast (<5s) so we don't surface it as its own
        # status — the previous phase's message lingers through it.
        metadata = upsert_type_metadata(conn, proposal)
        retrievers = _install_retrievers_after_apply(
            conn, graphname,
            proposal=proposal, pre_apply_existing=existing,
            progress=progress,
        )
        return {
            "status": "no-op",
            "statements": [],
            "job_name": None,
            "job_names": [],
            "gsql_output": "",
            "summary": summary,
            "metadata": metadata,
            "retrievers": retrievers,
        }

    # Split into two phases so TG's job-validator never sees an ALTER
    # referencing a vertex/edge type created elsewhere in the same
    # job. The ADD phase runs first; the ALTER phase (e.g. ADD PAIR
    # on existing edges) runs only after the ADD phase commits.
    add_stmts = [s for s in statements if s.lstrip().upper().startswith("ADD ")]
    alter_stmts = [s for s in statements if s.lstrip().upper().startswith("ALTER ")]

    def _run_phase(phase_stmts: List[str], phase_job: Optional[str]) -> Tuple[str, str]:
        block, name = build_schema_change_job(graphname, phase_stmts, phase_job)
        try:
            out = conn.gsql(block)
        except Exception:
            try:
                conn.gsql(f"USE GRAPH {graphname}\nDROP JOB {name}")
            except Exception:
                pass
            raise
        return out, name

    # The two-phase split (ADD then ALTER) is internal mechanics; the
    # user just sees a single "Applying domain schema" message that
    # spans both phases plus the brief metadata upsert. The wording
    # matches both schema-source paths (sample extraction and pasted
    # GSQL) — "extracted" would be misleading for the paste mode.
    _report("Applying domain schema")

    phase_outputs: List[str] = []
    phase_jobs: List[str] = []
    first_job_name: Optional[str] = None
    for phase_stmts in (add_stmts, alter_stmts):
        if not phase_stmts:
            continue
        # Only honor the caller-supplied job_name on the first phase that
        # actually runs; subsequent phases get auto-generated names so
        # they don't collide.
        phase_job = job_name if first_job_name is None else None
        output, ran_name = _run_phase(phase_stmts, phase_job)
        phase_outputs.append(output)
        phase_jobs.append(ran_name)
        if first_job_name is None:
            first_job_name = ran_name
        err = gsql_output_error(output)
        if err:
            try:
                conn.gsql(f"USE GRAPH {graphname}\nDROP JOB {ran_name}")
            except Exception:
                pass
            return {
                "status": "error",
                "statements": statements,
                "job_name": first_job_name,
                "job_names": phase_jobs,
                "gsql_output": "\n".join(phase_outputs),
                "error": err,
                "summary": summary,
                "metadata": {"entity_types": [], "relationship_types": []},
                "retrievers": {"status": "skipped", "reason": "schema apply failed"},
            }

    # Metadata upsert is fast (<5s); no separate status message.
    metadata = upsert_type_metadata(conn, proposal)
    retrievers = _install_retrievers_after_apply(
        conn, graphname,
        proposal=proposal, pre_apply_existing=existing,
        progress=progress,
    )
    return {
        "status": "applied",
        "statements": statements,
        "job_name": first_job_name,
        "job_names": phase_jobs,
        "gsql_output": "\n".join(phase_outputs),
        "summary": summary,
        "metadata": metadata,
        "retrievers": retrievers,
    }


def _detect_transitional_state(
    conn,
    proposal: SchemaProposal,
    pre_apply_existing: ExistingSchema,
) -> Optional[dict]:
    """Return a payload when a domain schema is being added to a graph
    that already has Entity-layer data; ``None`` otherwise.
    """
    new_vts = [
        v.name for v in proposal.vertices if not pre_apply_existing.has_vertex(v.name)
    ]
    if not new_vts:
        return None
    try:
        entity_count = conn.getVertexCount("Entity") or 0
    except Exception:
        entity_count = 0
    if entity_count <= 0:
        return None
    return {
        "entity_count": int(entity_count),
        "new_domain_vts": sorted(new_vts),
        "recommendation": (
            "Existing Entity-layer data won't be auto-promoted to the "
            "newly declared domain types. Retrievers will keep walking "
            "the Entity layer (retrieval_include_entity forced to "
            "True) so chat answers stay grounded. For full typed "
            "retrieval, clear derived data (Entity / RELATIONSHIP / "
            "Community) and re-run ECC against the existing chunks."
        ),
    }


def _install_retrievers_after_apply(
    conn,
    graphname: str,
    proposal: Optional[SchemaProposal] = None,
    pre_apply_existing: Optional[ExistingSchema] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Re-render and install the templated retrievers against the live
    domain schema. No-op when no domain types are on the graph.
    """
    try:
        snapshot = read_existing_schema(conn)
    except Exception as exc:
        return {"status": "error", "error": f"read live schema: {exc}"}

    # Union the live-schema view with the proposal so a stale cache
    # right after SCHEMA_CHANGE JOB doesn't miss new types.
    domain_vt_set: Set[str] = {
        v for v in snapshot.vertex_types if not is_structural_type(v)
    }
    domain_edge_set: Set[str] = {
        e for e in snapshot.edge_pairs
        if not is_structural_type(e) and e in snapshot.directed_edges
    }
    if proposal is not None:
        for v in proposal.vertices:
            if not is_structural_type(v.name):
                domain_vt_set.add(v.name)
        for e in proposal.edges:
            if not is_structural_type(e.name) and e.directed:
                domain_edge_set.add(e.name)
    domain_vts = sorted(domain_vt_set)
    domain_edges = sorted(domain_edge_set)

    import logging as _logging
    _logger = _logging.getLogger(__name__)
    _logger.info(
        f"_install_retrievers_after_apply: graph={graphname} "
        f"domain_vts={len(domain_vts)} directed_domain_edges={len(domain_edges)} "
        f"snapshot_edge_pairs={len(snapshot.edge_pairs)}"
    )

    if not domain_vts and not domain_edges:
        return {"status": "skipped", "reason": "no domain types on graph"}

    transitional: Optional[dict] = None
    if proposal is not None and pre_apply_existing is not None:
        transitional = _detect_transitional_state(
            conn, proposal, pre_apply_existing
        )

    try:
        from common.db.retriever_render import (
            install_retrievers,
            resolve_include_entity,
        )
    except Exception as exc:
        return {"status": "error", "error": f"import renderer: {exc}"}

    try:
        from common.config import graphrag_config
        include_entity = resolve_include_entity(
            graphrag_config.get,
            has_domain_schema=bool(domain_vts),
        )
    except Exception:
        include_entity = False if domain_vts else True

    if transitional:
        include_entity = True

    result: dict = {
        "status": "installed",
        "include_entity": include_entity,
        "results": install_retrievers(
            conn,
            graphname,
            domain_vts=domain_vts,
            domain_edges=domain_edges,
            include_entity=include_entity,
            progress=progress,
        ),
    }
    if transitional:
        result["transitional"] = transitional
    return result


# -----------------------------------------------------------------------------
# Validation summary (informational, never blocking)
# -----------------------------------------------------------------------------


def summarize(proposal: SchemaProposal) -> dict:
    """Return a small descriptive payload for logging / API responses
    (counts and lists of names). Never raises.
    """
    return {
        "vertex_count": len(proposal.vertices),
        "edge_count": len(proposal.edges),
        "vertex_names": [v.name for v in proposal.vertices],
        "edge_names": [e.name for e in proposal.edges],
        "edge_pair_count": sum(len(e.pairs) for e in proposal.edges),
        "domain_label": proposal.domain_label,
    }
