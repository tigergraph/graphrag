"""Canonical immutable identity used by every history operation."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping


@dataclass(frozen=True)
class HistoryPrincipal:
    user_id: str
    accessible_graphs: frozenset[str]
    global_roles: frozenset[str]
    graph_roles: Mapping[str, tuple[str, ...]]

    @classmethod
    def create(
        cls,
        *,
        user_id: str,
        accessible_graphs: Iterable[str],
        global_roles: Iterable[str] = (),
        graph_roles: Mapping[str, Iterable[str]] | None = None,
        operational_graph: str = "GraphRAGChatHistory",
    ) -> "HistoryPrincipal":
        canonical = (user_id or "").strip()
        if not canonical:
            raise ValueError("A canonical TigerGraph username is required")

        immutable_graph_roles = {
            str(graph): tuple(sorted({str(role).lower() for role in roles}))
            for graph, roles in (graph_roles or {}).items()
            if graph != operational_graph
        }
        return cls(
            user_id=canonical,
            accessible_graphs=frozenset(
                str(graph)
                for graph in accessible_graphs
                if graph and graph != operational_graph
            ),
            global_roles=frozenset(
                str(role).lower() for role in global_roles if role
            ),
            graph_roles=MappingProxyType(immutable_graph_roles),
        )

    def can_access_graph(self, graph_name: str) -> bool:
        return graph_name in self.accessible_graphs

    def is_history_admin(self) -> bool:
        return bool(self.global_roles & {"superuser", "globaldesigner"})

    def is_trace_reader(self) -> bool:
        return "superuser" in self.global_roles
