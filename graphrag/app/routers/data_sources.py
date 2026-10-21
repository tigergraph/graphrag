"""Graph-scoped enterprise data-source management."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.security import HTTPBasicCredentials

from common.config import validate_graphname
from common.db.schema_utils import apply_proposal
from common.utils.graph_locks import (
    acquire_graph_lock,
    get_current_operation,
    release_graph_lock,
)
from connectors.jira.client import JiraAPIError, JiraCloudClient
from connectors.jira.config import PROJECT_KEY_RE, JiraDataSource, JiraScope
from connectors.jira.schema import (
    jira_schema_proposal,
    jira_schema_status,
)
from connectors.jira.state import JiraSourceStore
from connectors.jira.sync import JiraSyncService

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Data Sources"])
route_prefix = "/ui"
store = JiraSourceStore()

_sync_state: dict[str, dict[str, Any]] = {}
_sync_state_lock = threading.Lock()
_jira_tokens: dict[tuple[str, str], str] = {}
_jira_tokens_lock = threading.Lock()


def _ui_basic_auth():
    from routers.ui import ui_basic_auth

    return ui_basic_auth


def _require_access(
    graphname: str,
    auth: tuple[list[str], HTTPBasicCredentials],
) -> HTTPBasicCredentials:
    validate_graphname(graphname)
    graphs, credentials = auth
    if graphname not in graphs:
        raise HTTPException(status_code=403, detail="Graph access is required.")
    from routers.ui import _require_roles

    _require_roles(credentials, {"superuser", "globaldesigner"})
    return credentials


def _source_or_404(graphname: str, source_id: str) -> JiraDataSource:
    try:
        return store.get(graphname, source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Data source not found.")


def _source_with_credentials(
    graphname: str,
    source_id: str,
) -> JiraDataSource:
    source = _source_or_404(graphname, source_id)
    with _jira_tokens_lock:
        token = _jira_tokens.get((graphname, source_id))
    token = token or source.connection.api_token
    if not token:
        raise HTTPException(
            status_code=409,
            detail=(
                "Jira API token is not available. Test the connection again "
                "and provide the token."
            ),
        )
    resolved = source.model_copy(deep=True)
    resolved.connection.api_token = token
    return resolved


def _jira_error(exc: JiraAPIError) -> HTTPException:
    if exc.status_code == 404:
        detail = (
            "Jira REST API was not found at the configured Site URL. "
            "Use the Jira tenant base URL, for example "
            "https://your-company.atlassian.net."
        )
    elif exc.status_code == 401:
        detail = (
            "Jira rejected the credentials. Use the Atlassian account email "
            "that owns the API token and a valid Jira API token."
        )
    elif exc.status_code == 403:
        detail = (
            "Jira accepted the credentials but denied access. Verify that the "
            "account can access Jira and browse the required projects."
        )
    else:
        detail = str(exc)
    status_code = 400 if 400 <= exc.status_code < 500 else 502
    return HTTPException(status_code=status_code, detail=detail)


def _connection(request: Request, graphname: str):
    authorization = request.headers.get("Authorization")
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header.")
    from routers.ui import ws_basic_auth

    _, conn = ws_basic_auth(authorization, graphname)
    return conn


def _set_sync_state(sync_run_id: str, **updates: Any) -> None:
    with _sync_state_lock:
        current = dict(_sync_state.get(sync_run_id) or {})
        current.update(updates)
        _sync_state[sync_run_id] = current


@router.get(f"{route_prefix}/{{graphname}}/data-sources")
def list_data_sources(
    graphname: str,
    auth: Annotated[
        tuple[list[str], HTTPBasicCredentials],
        Depends(_ui_basic_auth()),
    ],
):
    _require_access(graphname, auth)
    return {"sources": store.list(graphname)}


@router.put(f"{route_prefix}/{{graphname}}/data-sources/{{source_id}}")
def save_data_source(
    graphname: str,
    source_id: str,
    source: JiraDataSource,
    auth: Annotated[
        tuple[list[str], HTTPBasicCredentials],
        Depends(_ui_basic_auth()),
    ],
):
    _require_access(graphname, auth)
    if source.id != source_id:
        raise HTTPException(
            status_code=400,
            detail="Path source id must match the request body.",
        )
    try:
        saved = store.upsert(graphname, source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"source": store.redact(saved)}


@router.delete(f"{route_prefix}/{{graphname}}/data-sources/{{source_id}}")
def delete_data_source(
    graphname: str,
    source_id: str,
    auth: Annotated[
        tuple[list[str], HTTPBasicCredentials],
        Depends(_ui_basic_auth()),
    ],
):
    _require_access(graphname, auth)
    try:
        store.delete(graphname, source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Data source not found.")
    with _jira_tokens_lock:
        _jira_tokens.pop((graphname, source_id), None)
    return {"status": "deleted"}


@router.post(f"{route_prefix}/{{graphname}}/data-sources/{{source_id}}/test")
def test_data_source(
    graphname: str,
    source_id: str,
    auth: Annotated[
        tuple[list[str], HTTPBasicCredentials],
        Depends(_ui_basic_auth()),
    ],
    candidate: JiraDataSource | None = None,
):
    _require_access(graphname, auth)
    if candidate is None or not candidate.connection.api_token.strip():
        raise HTTPException(
            status_code=400,
            detail="Enter the Jira API token before testing the connection.",
        )
    if candidate.id != source_id:
        raise HTTPException(
            status_code=400,
            detail="Path source id must match the request body.",
        )
    token = candidate.connection.api_token.strip()
    try:
        with JiraCloudClient(candidate) as client:
            account = client.myself()
            cloud_id = candidate.connection.cloud_id or client.cloud_id()
    except JiraAPIError as exc:
        raise _jira_error(exc)
    try:
        source = store.upsert(graphname, candidate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    with _jira_tokens_lock:
        _jira_tokens[(graphname, source_id)] = token
    source.connection.cloud_id = cloud_id
    source.sync.last_tested_at = datetime.now(timezone.utc)
    store.update_runtime_state(graphname, source)
    return {
        "status": "connected",
        "account": {
            "account_id": account.get("accountId"),
            "display_name": account.get("displayName"),
        },
        "cloud_id": cloud_id,
        "tested_at": source.sync.last_tested_at.isoformat(),
        "source": store.redact(source),
    }


@router.get(f"{route_prefix}/{{graphname}}/data-sources/{{source_id}}/projects")
def list_jira_projects(
    graphname: str,
    source_id: str,
    auth: Annotated[
        tuple[list[str], HTTPBasicCredentials],
        Depends(_ui_basic_auth()),
    ],
    project_key: str | None = None,
):
    _require_access(graphname, auth)
    source = _source_with_credentials(graphname, source_id)
    if source.sync.last_tested_at is None:
        raise HTTPException(
            status_code=409,
            detail="Test this Jira connection before loading projects.",
        )
    normalized_project_key = project_key.strip().upper() if project_key else None
    if normalized_project_key and not PROJECT_KEY_RE.fullmatch(
        normalized_project_key
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Project key must start with a letter and contain only letters, "
                "numbers, or underscores."
            ),
        )
    try:
        with JiraCloudClient(source) as client:
            projects = (
                [client.project(normalized_project_key)]
                if normalized_project_key
                else client.projects()
            )
    except JiraAPIError as exc:
        if normalized_project_key and exc.status_code == 404:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Jira project {normalized_project_key} was not found or "
                    "is not visible to this account."
                ),
            )
        raise _jira_error(exc)
    return {
        "projects": [
            {
                "id": project.get("id"),
                "key": project.get("key"),
                "name": project.get("name"),
            }
            for project in projects
        ]
    }


@router.post(
    f"{route_prefix}/{{graphname}}/data-sources/{{source_id}}/issues/count"
)
def preview_jira_issue_count(
    graphname: str,
    source_id: str,
    scope: JiraScope,
    auth: Annotated[
        tuple[list[str], HTTPBasicCredentials],
        Depends(_ui_basic_auth()),
    ],
):
    """Estimate how many Jira issues match the unsaved project scope."""
    _require_access(graphname, auth)
    if not scope.project_keys:
        raise HTTPException(
            status_code=400,
            detail="Select at least one Jira project before previewing tickets.",
        )
    source = _source_with_credentials(graphname, source_id)
    if source.sync.last_tested_at is None:
        raise HTTPException(
            status_code=409,
            detail="Test this Jira connection before previewing tickets.",
        )
    preview_source = source.model_copy(deep=True)
    preview_source.scope = scope
    try:
        with JiraCloudClient(preview_source) as client:
            count = client.approximate_issue_count()
    except JiraAPIError as exc:
        raise _jira_error(exc)
    return {"count": count, "approximate": True}


@router.get(f"{route_prefix}/{{graphname}}/data-sources/jira/schema")
def get_jira_schema_status(
    graphname: str,
    request: Request,
    auth: Annotated[
        tuple[list[str], HTTPBasicCredentials],
        Depends(_ui_basic_auth()),
    ],
):
    _require_access(graphname, auth)
    conn = _connection(request, graphname)
    return jira_schema_status(conn)


@router.post(f"{route_prefix}/{{graphname}}/data-sources/jira/schema/install")
def install_jira_schema(
    graphname: str,
    request: Request,
    auth: Annotated[
        tuple[list[str], HTTPBasicCredentials],
        Depends(_ui_basic_auth()),
    ],
):
    _require_access(graphname, auth)
    sources = store.load(graphname).sources
    if not sources:
        raise HTTPException(
            status_code=409,
            detail="Save a Jira connection before installing its schema.",
        )
    if not any(source.sync.last_tested_at for source in sources):
        raise HTTPException(
            status_code=409,
            detail="Test a Jira connection before installing its schema.",
        )
    if not any(
        source.sync.last_tested_at and source.scope.project_keys
        for source in sources
    ):
        raise HTTPException(
            status_code=409,
            detail="Select and save at least one Jira project before installing its schema.",
        )
    operation = "install_jira_schema"
    if not acquire_graph_lock(graphname, operation):
        current = get_current_operation(graphname) or "another operation"
        raise HTTPException(
            status_code=409,
            detail=f"Graph '{graphname}' is busy with '{current}'.",
        )
    try:
        conn = _connection(request, graphname)
        before = jira_schema_status(conn)
        if before["status"] == "not_initialized":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Initialize the knowledge graph before installing "
                    "the Jira connector schema."
                ),
            )
        if before["status"] == "conflict":
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Existing graph schema conflicts with Jira schema.",
                    "conflicts": before["conflicts"],
                },
            )

        result = apply_proposal(conn, graphname, jira_schema_proposal())
        if result.get("status") == "error":
            raise HTTPException(
                status_code=500,
                detail=result.get("error") or "Jira schema installation failed.",
            )
        retrievers = result.get("retrievers") or {}
        if retrievers.get("status") == "error":
            raise HTTPException(
                status_code=500,
                detail=(
                    "Jira schema was applied, but retriever installation failed: "
                    f"{retrievers.get('error', 'unknown error')}"
                ),
            )

        # The connection used to apply the SCHEMA_CHANGE JOB can retain the
        # pre-migration schema snapshot. Verify through a fresh connection so
        # a successful migration is not reported as a false 500.
        verification_conn = _connection(request, graphname)
        after = jira_schema_status(verification_conn)
        if after["status"] != "installed":
            logger.error(
                "Jira schema verification failed for graph %s: %s",
                graphname,
                after,
            )
            raise HTTPException(
                status_code=500,
                detail={
                    "message": (
                        "Jira schema installation completed, but verification "
                        "did not find a compatible schema."
                    ),
                    "missing": after.get("missing"),
                    "conflicts": after.get("conflicts"),
                },
            )
        return {
            "status": "installed",
            "schema": after,
            "migration": {
                "status": result.get("status"),
                "statements": result.get("statements") or [],
                "summary": result.get("summary") or {},
                "retrievers": retrievers,
            },
        }
    finally:
        release_graph_lock(graphname, operation)


@router.post(f"{route_prefix}/{{graphname}}/data-sources/{{source_id}}/sync")
def start_sync(
    graphname: str,
    source_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    auth: Annotated[
        tuple[list[str], HTTPBasicCredentials],
        Depends(_ui_basic_auth()),
    ],
):
    _require_access(graphname, auth)
    source = _source_with_credentials(graphname, source_id)
    if not source.enabled:
        raise HTTPException(status_code=409, detail="Jira data source is disabled.")
    if source.sync.last_tested_at is None:
        raise HTTPException(
            status_code=409,
            detail="Test this Jira connection before synchronization.",
        )
    if not source.scope.project_keys:
        raise HTTPException(
            status_code=409,
            detail="Select at least one Jira project before synchronization.",
        )
    authorization = request.headers.get("Authorization")
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header.")
    conn = _connection(request, graphname)
    schema = jira_schema_status(conn)
    if schema["status"] != "installed":
        raise HTTPException(
            status_code=409,
            detail=(
                "Jira schema is not installed for this graph. "
                "Install it from Data Sources before synchronization."
            ),
        )

    operation = f"jira_sync:{source_id}"
    if not acquire_graph_lock(graphname, operation):
        current = get_current_operation(graphname) or "another operation"
        raise HTTPException(
            status_code=409,
            detail=f"Graph '{graphname}' is busy with '{current}'.",
        )

    run_id = uuid.uuid4().hex
    try:
        _set_sync_state(
            run_id,
            run_id=run_id,
            graphname=graphname,
            source_id=source_id,
            status="queued",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception:
        release_graph_lock(graphname, operation)
        raise

    def run() -> None:
        result: dict[str, Any] | None = None
        error: Exception | None = None
        try:
            _set_sync_state(run_id, status="running")
            from routers.ui import ws_basic_auth

            _, conn = ws_basic_auth(authorization, graphname)
            source = _source_with_credentials(graphname, source_id)
            result = JiraSyncService(
                graphname,
                source,
                conn,
                store=store,
            ).run()
        except Exception as exc:
            error = exc
            logger.exception(
                "Jira data-source sync failed graph=%s source=%s",
                graphname,
                source_id,
            )
        finally:
            # Release before publishing a terminal state. The UI starts the
            # GraphRAG rebuild as soon as it observes "completed".
            release_graph_lock(graphname, operation)

        if error is not None:
            _set_sync_state(
                run_id,
                status="failed",
                completed_at=datetime.now(timezone.utc).isoformat(),
                error=str(error)[:1000],
            )
        else:
            _set_sync_state(
                run_id,
                status="completed",
                completed_at=datetime.now(timezone.utc).isoformat(),
                result=result,
            )

    try:
        background_tasks.add_task(run)
    except Exception:
        release_graph_lock(graphname, operation)
        raise
    return {"status": "submitted", "run_id": run_id}


@router.get(
    f"{route_prefix}/{{graphname}}/data-sources/{{source_id}}/sync/{{run_id}}"
)
def get_sync_status(
    graphname: str,
    source_id: str,
    run_id: str,
    auth: Annotated[
        tuple[list[str], HTTPBasicCredentials],
        Depends(_ui_basic_auth()),
    ],
):
    _require_access(graphname, auth)
    with _sync_state_lock:
        state = dict(_sync_state.get(run_id) or {})
    if (
        not state
        or state.get("graphname") != graphname
        or state.get("source_id") != source_id
    ):
        raise HTTPException(status_code=404, detail="Sync run not found.")
    return state
