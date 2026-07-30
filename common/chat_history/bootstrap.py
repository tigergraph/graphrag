"""Idempotent schema/query bootstrap for ``GraphRAGChatHistory``."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Any

from pyTigerGraph import TigerGraphConnection

from common.db.query_install import install_query_set
from common.db.query_sets import CHAT_HISTORY_QUERIES, with_gsql
from common.db.connection_utils import normalize_restpp_url
from common.db.schema_utils import gsql_output_error

from .settings import HistorySettings, load_database_config

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_PATH = (
    _REPO_ROOT / "common" / "gsql" / "chat_history" / "ChatHistory_Schema.gsql"
)
_RUNTIME_ROLE = "GraphRAGChatHistoryRuntime"
_RUNTIME_DATA_PRIVILEGES = (
    "GRANT READ, CREATE, UPDATE, DELETE ON ALL DATA "
    "IN GRAPH {graph} TO {role}",
)


def _admin_connection(settings: HistorySettings) -> TigerGraphConnection:
    username = os.getenv("CHAT_HISTORY_BOOTSTRAP_USERNAME", "")
    password = os.getenv("CHAT_HISTORY_BOOTSTRAP_PASSWORD", "")
    if not (username and password):
        raise RuntimeError(
            "Set CHAT_HISTORY_BOOTSTRAP_USERNAME and "
            "CHAT_HISTORY_BOOTSTRAP_PASSWORD; GSQL schema and query "
            "installation requires an administrative user/password session"
        )

    db_config = load_database_config()
    kwargs: dict[str, Any] = {
        "host": db_config["hostname"],
        "graphname": settings.graph_name,
        "restppPort": db_config.get("restppPort", "9000"),
        "gsPort": db_config.get("gsPort", "14240"),
    }
    kwargs["username"] = username
    kwargs["password"] = password
    conn = normalize_restpp_url(TigerGraphConnection(**kwargs))
    conn.customizeHeader(
        timeout=max(settings.timeout_seconds, 60) * 1000,
        responseSize=5_000_000,
    )
    return conn


def _render_for_graph(text: str, graph_name: str) -> str:
    return text.replace("GraphRAGChatHistory", graph_name)


def _graph_names(conn: TigerGraphConnection) -> set[str]:
    return {
        str(item.get("graphName"))
        for item in conn.listGraphs()
        if isinstance(item, dict) and item.get("graphName")
    }


def _gsql(conn: TigerGraphConnection, command: str) -> str:
    output = conn.gsql(command)
    error = gsql_output_error(output)
    if error:
        raise RuntimeError(error)
    return output


def _schema_change_only(schema: str) -> str:
    marker = "CREATE SCHEMA_CHANGE JOB"
    index = schema.find(marker)
    if index < 0:
        raise RuntimeError("Chat history schema file has no schema-change job")
    return schema[index:]


def _ensure_schema(
    conn: TigerGraphConnection, settings: HistorySettings
) -> None:
    schema = _render_for_graph(
        _SCHEMA_PATH.read_text(encoding="utf-8"), settings.graph_name
    )
    if settings.graph_name not in _graph_names(conn):
        logger.info("Creating operational graph %s", settings.graph_name)
        _gsql(conn, schema)
        return

    listing = _gsql(conn, f"USE GRAPH {settings.graph_name}\nls")
    if "- VERTEX ChatUser" not in listing:
        logger.info(
            "Adding chat history schema to existing graph %s",
            settings.graph_name,
        )
        try:
            _gsql(
                conn,
                f"USE GRAPH {settings.graph_name}\n"
                "DROP JOB add_graphrag_chat_history_schema",
            )
        except RuntimeError:
            pass
        _gsql(
            conn,
            f"USE GRAPH {settings.graph_name}\n"
            + _schema_change_only(schema)
        )


def _create_queries(
    conn: TigerGraphConnection, settings: HistorySettings
) -> list[str]:
    names: list[str] = []
    for query_path in with_gsql(CHAT_HISTORY_QUERIES):
        path = _REPO_ROOT / query_path
        body = _render_for_graph(
            path.read_text(encoding="utf-8"), settings.graph_name
        )
        query_name = path.stem
        logger.info("Creating history query %s", query_name)
        _gsql(
            conn,
            f"USE GRAPH {settings.graph_name}\nBEGIN\n{body}\nEND"
        )
        names.append(query_name)
    return names


def _ensure_runtime_role(
    conn: TigerGraphConnection,
    settings: HistorySettings,
    query_names: list[str],
) -> None:
    runtime_user = os.getenv("CHAT_HISTORY_RUNTIME_USERNAME", "").strip()
    listing = _gsql(
        conn, f"USE GRAPH {settings.graph_name}\nSHOW ROLE"
    )
    if _RUNTIME_ROLE not in listing:
        _gsql(
            conn,
            f"CREATE ROLE {_RUNTIME_ROLE} ON GRAPH {settings.graph_name}"
        )
    _gsql(
        conn,
        "GRANT EXECUTE ON QUERY "
        + ", ".join(query_names)
        + f" IN GRAPH {settings.graph_name} TO {_RUNTIME_ROLE}"
    )
    # TigerGraph 4.2 evaluates the data operations performed by installed
    # queries in addition to their object-level EXECUTE grants. Keep these
    # privileges isolated to the operational graph and its dedicated runtime
    # role; the runtime account receives no access to knowledge graphs.
    for grant in _RUNTIME_DATA_PRIVILEGES:
        _gsql(
            conn,
            grant.format(
                graph=settings.graph_name,
                role=_RUNTIME_ROLE,
            ),
        )
    if runtime_user:
        _gsql(
            conn,
            f"GRANT ROLE {_RUNTIME_ROLE} ON GRAPH {settings.graph_name} "
            f"TO {runtime_user}"
        )
    else:
        logger.warning(
            "CHAT_HISTORY_RUNTIME_USERNAME is not set; the runtime role was "
            "created but not assigned"
        )


def bootstrap_history_graph(
    *,
    connection: TigerGraphConnection | None = None,
    settings: HistorySettings | None = None,
    configure_role: bool = True,
) -> dict[str, Any]:
    settings = settings or HistorySettings.from_env()
    conn = connection or _admin_connection(settings)
    conn.graphname = settings.graph_name
    _ensure_schema(conn, settings)
    query_names = _create_queries(conn, settings)
    install_query_set(conn, query_names, force=True)
    if configure_role:
        _ensure_runtime_role(conn, settings, query_names)
    return {
        "graph": settings.graph_name,
        "queries": query_names,
        "runtime_role": _RUNTIME_ROLE if configure_role else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bootstrap TigerGraph chat history storage"
    )
    parser.add_argument(
        "--skip-role",
        action="store_true",
        help="Create/install schema and queries without managing a runtime role",
    )
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOGLEVEL", "INFO"))
    result = bootstrap_history_graph(configure_role=not args.skip_role)
    print(result)


if __name__ == "__main__":
    main()
