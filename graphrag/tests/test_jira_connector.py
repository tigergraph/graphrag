from __future__ import annotations

import json
from datetime import date, datetime, timezone

import httpx
import pytest

from connectors.jira.adf import adf_to_markdown
from connectors.jira.client import JiraCloudClient
from connectors.jira.config import JiraDataSource
from connectors.jira.mapper import JiraIssueMapper
from connectors.jira.schema import (
    JIRA_ASSIGNEE_EDGE,
    JIRA_COMMENT_AFTER_EDGE,
    JIRA_COMMENT_AUTHOR_EDGE,
    JIRA_COMMENT_ISSUE_EDGE,
    JIRA_COMMENT_REPLY_EDGE,
    JIRA_LINK_EDGE,
    JIRA_PARENT_EDGE,
    JIRA_PROJECT_EDGE,
    JIRA_REPORTER_EDGE,
    jira_schema_proposal,
    jira_schema_status,
)
from connectors.jira.state import JiraSourceStore
from connectors.jira.sync import JiraSyncService


def test_sync_state_records_public_run_id_without_argument_collision():
    from routers import data_sources

    run_id = "test-run-id"
    try:
        data_sources._set_sync_state(
            run_id,
            run_id=run_id,
            status="queued",
        )
        assert data_sources._sync_state[run_id] == {
            "run_id": run_id,
            "status": "queued",
        }
    finally:
        with data_sources._sync_state_lock:
            data_sources._sync_state.pop(run_id, None)


def source(**overrides) -> JiraDataSource:
    payload = {
        "id": "jira-acme",
        "display_name": "Acme Jira",
        "connection": {
            "site_url": "https://acme.atlassian.net/",
            "email": "svc@example.com",
            "api_token": "secret",
        },
        "scope": {
            "project_keys": ["pay"],
            "include_comments": True,
        },
    }
    payload.update(overrides)
    return JiraDataSource.model_validate(payload)


def test_source_config_normalizes_url_and_projects():
    config = source()
    assert config.connection.site_url == "https://acme.atlassian.net"
    assert config.scope.project_keys == ["PAY"]


def test_store_rejects_atlassian_service_url(tmp_path):
    store = JiraSourceStore(str(tmp_path))
    with pytest.raises(ValueError, match="Jira tenant URL"):
        store.upsert(
            "TestGraph",
            source(
                connection={
                    "site_url": "https://graphql.atlassian.net",
                    "email": "svc@example.com",
                    "api_token": "secret",
                }
            ),
        )


def test_source_config_allows_connection_draft_without_scope():
    config = source(scope={"project_keys": []})
    assert config.scope.project_keys == []


def test_adf_to_markdown_preserves_structure_and_links():
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "Decision"}],
            },
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Use "},
                    {
                        "type": "text",
                        "text": "GraphRAG",
                        "marks": [
                            {"type": "strong"},
                            {
                                "type": "link",
                                "attrs": {"href": "https://example.com"},
                            },
                        ],
                    },
                ],
            },
        ],
    }
    markdown = adf_to_markdown(adf)
    assert "## Decision" in markdown
    assert "[**GraphRAG**](https://example.com)" in markdown


def test_enhanced_search_uses_next_page_token_and_completes_comments():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/search/jql"):
            body = json.loads(request.content)
            if body.get("nextPageToken") == "page-2":
                return httpx.Response(200, json={"issues": [{"id": "2", "fields": {}}]})
            return httpx.Response(
                200,
                json={
                    "issues": [
                        {
                            "id": "1",
                            "fields": {
                                "comment": {
                                    "total": 2,
                                    "comments": [{"id": "10"}],
                                }
                            },
                        }
                    ],
                    "nextPageToken": "page-2",
                },
            )
        if request.url.path.endswith("/issue/1/comment"):
            return httpx.Response(
                200,
                json={"total": 2, "comments": [{"id": "11"}]},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    http_client = httpx.Client(
        base_url="https://acme.atlassian.net",
        transport=httpx.MockTransport(handler),
    )
    issues = list(JiraCloudClient(source(), client=http_client).iter_issues())
    assert [issue["id"] for issue in issues] == ["1", "2"]
    assert len(issues[0]["fields"]["comment"]["comments"]) == 2
    search_bodies = [
        json.loads(request.content)
        for request in requests
        if request.url.path.endswith("/search/jql")
    ]
    assert search_bodies[1]["nextPageToken"] == "page-2"
    assert "/rest/api/3/search/jql" in str(requests[0].url)


def test_project_loads_one_project_by_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/rest/api/3/project/PAY"
        return httpx.Response(
            200,
            json={"id": "10001", "key": "PAY", "name": "Payments"},
        )

    http_client = httpx.Client(
        base_url="https://acme.atlassian.net",
        transport=httpx.MockTransport(handler),
    )
    project = JiraCloudClient(source(), client=http_client).project("PAY")
    assert project == {"id": "10001", "key": "PAY", "name": "Payments"}


def test_project_endpoint_loads_only_requested_project(monkeypatch):
    from routers import data_sources

    configured = source(
        sync={"last_tested_at": datetime.now(timezone.utc)}
    )

    class Client:
        def __init__(self, jira_source):
            assert jira_source == configured

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def project(self, project_key):
            assert project_key == "PAY"
            return {"id": "10001", "key": "PAY", "name": "Payments"}

    monkeypatch.setattr(data_sources, "_require_access", lambda *args: None)
    monkeypatch.setattr(
        data_sources, "_source_or_404", lambda *args: configured
    )
    monkeypatch.setattr(data_sources, "JiraCloudClient", Client)

    result = data_sources.list_jira_projects(
        "TestGraph",
        "jira-acme",
        auth=(["TestGraph"], object()),
        project_key=" pay ",
    )
    assert result == {
        "projects": [{"id": "10001", "key": "PAY", "name": "Payments"}]
    }


def test_search_applies_structured_scope_filters():
    request_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"issues": []})

    configured = source(
        scope={
            "project_keys": ["PAY"],
            "created_after": "2026-01-01",
            "updated_after": "2026-06-01",
            "status_categories": ["new", "indeterminate"],
            "include_comments": True,
        }
    )
    http_client = httpx.Client(
        base_url="https://acme.atlassian.net",
        transport=httpx.MockTransport(handler),
    )
    assert list(
        JiraCloudClient(configured, client=http_client).iter_issues()
    ) == []
    assert request_bodies[0]["jql"] == (
        'project in (PAY) AND created >= "2026-01-01" '
        'AND updated >= "2026-06-01" '
        'AND statusCategory in ("To Do", "In Progress") '
        "ORDER BY updated ASC, key ASC"
    )


def test_approximate_count_uses_scope_without_ordering():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/3/search/approximate-count"
        body = json.loads(request.content)
        assert body["jql"] == (
            'project in (PAY) AND created >= "2026-01-01" '
            'AND statusCategory in ("To Do", "In Progress")'
        )
        assert "ORDER BY" not in body["jql"]
        return httpx.Response(200, json={"count": 42})

    configured = source(
        scope={
            "project_keys": ["PAY"],
            "created_after": "2026-01-01",
            "status_categories": ["new", "indeterminate"],
        }
    )
    http_client = httpx.Client(
        base_url="https://acme.atlassian.net",
        transport=httpx.MockTransport(handler),
    )
    assert (
        JiraCloudClient(
            configured,
            client=http_client,
        ).approximate_issue_count()
        == 42
    )


def test_existing_hash_lookup_treats_new_issue_ids_as_missing(tmp_path):
    new_id = "jira:cloud-1:issue:100"
    existing_id = "jira:cloud-1:issue:existing"

    class Connection:
        def getVertices(self, vertex_type, select=""):
            assert vertex_type == "JiraIssue"
            assert select == "content_hash"
            return [
                {
                    "v_id": existing_id,
                    "attributes": {"content_hash": "existing-hash"},
                }
            ]

    service = JiraSyncService(
        "TestGraph",
        source(),
        Connection(),
        store=JiraSourceStore(str(tmp_path)),
        client=object(),
    )
    assert service._existing_hashes([new_id, existing_id]) == {
        existing_id: "existing-hash"
    }


def test_comment_reconciliation_deletes_only_removed_comments(
    tmp_path,
    monkeypatch,
):
    issue = {
        "id": "10422",
        "key": "PAY-123",
        "fields": {
            "summary": "Login timeout",
            "project": {"id": "10001", "key": "PAY", "name": "Payments"},
            "comment": {
                "comments": [
                    {
                        "id": "9001",
                        "author": {
                            "accountId": "ada",
                            "displayName": "Ada",
                        },
                        "created": "2026-09-18T11:02:00.000+0000",
                        "body": "Keep this comment.",
                    }
                ]
            },
        },
    }
    mapped = JiraIssueMapper(source(), "cloud-1").map(issue)
    current_id = "jira:cloud-1:comment:9001"
    stale_id = "jira:cloud-1:comment:9002"

    class Connection:
        def getEdges(self, vertex_type, vertex_id, edge_type):
            assert vertex_type == "JiraIssue"
            assert vertex_id == mapped.issue_vertex_id
            assert edge_type == f"reverse_{JIRA_COMMENT_ISSUE_EDGE}"
            return [{"to_id": current_id}, {"to_id": stale_id}]

    service = JiraSyncService(
        "TestGraph",
        source(),
        Connection(),
        store=JiraSourceStore(str(tmp_path)),
        client=object(),
    )
    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        service,
        "_delete_comment",
        lambda issue_id, comment_id: deleted.append(
            (issue_id, comment_id)
        ),
    )
    hashes = {current_id: "current", stale_id: "stale"}

    assert service._reconcile_issue_comments(mapped, hashes) == 1
    assert deleted == [(mapped.issue_vertex_id, stale_id)]
    assert hashes == {current_id: "current"}


def test_sync_persists_checkpoint_after_each_committed_page(
    tmp_path,
    monkeypatch,
):
    from connectors.jira import sync as sync_module

    store = JiraSourceStore(str(tmp_path))
    configured = store.upsert("TestGraph", source())
    issue = {
        "id": "10422",
        "key": "PAY-123",
        "fields": {
            "summary": "Login timeout",
            "project": {"id": "10001", "key": "PAY", "name": "Payments"},
            "updated": "2026-09-20T14:03:00.000+0000",
        },
    }

    class Client:
        def cloud_id(self):
            return "cloud-1"

        def iter_issue_pages(self):
            yield [issue]
            assert store.get(
                "TestGraph",
                "jira-acme",
            ).sync.checkpoint == datetime(
                2026,
                9,
                20,
                14,
                3,
                tzinfo=timezone.utc,
            )
            raise RuntimeError("simulated shutdown")

    class Connection:
        def getVertices(self, vertex_type, select=""):
            assert vertex_type in {"Document", "JiraIssue", "JiraComment"}
            assert select == (
                "id" if vertex_type == "Document" else "content_hash"
            )
            return []

    monkeypatch.setattr(
        sync_module,
        "jira_schema_status",
        lambda *args: {"status": "installed"},
    )
    service = JiraSyncService(
        "TestGraph",
        configured,
        Connection(),
        store=store,
        client=Client(),
    )
    monkeypatch.setattr(service, "_upsert_issue_page", lambda *args: (0, 0))

    with pytest.raises(RuntimeError, match="simulated shutdown"):
        service.run()

    stored = store.get("TestGraph", "jira-acme")
    assert stored.sync.checkpoint == datetime(
        2026,
        9,
        20,
        14,
        3,
        tzinfo=timezone.utc,
    )
    assert stored.sync.last_completed_at is None
    assert stored.sync.last_error == "simulated shutdown"


def test_sync_requires_rebuild_when_existing_chunks_lack_embeddings(
    tmp_path,
    monkeypatch,
):
    from connectors.jira import sync as sync_module

    store = JiraSourceStore(str(tmp_path))
    configured = store.upsert("TestGraph", source())

    class Client:
        def cloud_id(self):
            return "cloud-1"

        def iter_issue_pages(self):
            return iter(())

    class Connection:
        def getVertices(self, vertex_type, select=""):
            return []

    monkeypatch.setattr(
        sync_module,
        "jira_schema_status",
        lambda *args: {"status": "installed"},
    )
    monkeypatch.setattr(
        sync_module,
        "embedding_coverage",
        lambda *args: {"total": 12, "missing": 3},
    )

    result = JiraSyncService(
        "TestGraph",
        configured,
        Connection(),
        store=store,
        client=Client(),
    ).run()

    assert result["documents_loaded"] == 0
    assert result["missing_chunk_embeddings"] == 3
    assert result["rebuild_required"] is True


def test_mapper_writes_small_schema_and_searchable_document():
    issue = {
        "id": "10422",
        "key": "PAY-123",
        "fields": {
            "summary": "Login timeout",
            "project": {"id": "10001", "key": "PAY", "name": "Payments"},
            "issuetype": {"name": "Bug"},
            "status": {
                "name": "In Progress",
                "statusCategory": {"key": "indeterminate"},
            },
            "priority": {"name": "High"},
            "assignee": {"accountId": "ada", "displayName": "Ada"},
            "reporter": {"accountId": "grace", "displayName": "Grace"},
            "created": "2026-09-01T10:00:00.000+0000",
            "updated": "2026-09-20T14:03:00.000+0000",
            "description": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Timeout at checkout"}],
                    }
                ],
            },
            "comment": {
                "comments": [
                    {
                        "id": "9001",
                        "author": {
                            "accountId": "ada",
                            "displayName": "Ada",
                        },
                        "created": "2026-09-18T11:02:00.000+0000",
                        "body": "Increase the gateway timeout.",
                    },
                    {
                        "id": "9002",
                        "parentId": "9001",
                        "author": {
                            "accountId": "grace",
                            "displayName": "Grace",
                        },
                        "created": "2026-09-18T12:02:00.000+0000",
                        "body": "The timeout was increased.",
                    }
                ]
            },
            "issuelinks": [],
        },
    }
    mapped = JiraIssueMapper(source(), "cloud-1").map(issue)
    assert {vertex.vertex_type for vertex in mapped.vertices} == {
        "JiraComment",
        "JiraIssue",
        "JiraProject",
        "JiraUser",
    }
    assert {edge.edge_type for edge in mapped.edges} == {
        JIRA_PROJECT_EDGE,
        JIRA_ASSIGNEE_EDGE,
        JIRA_REPORTER_EDGE,
        JIRA_COMMENT_ISSUE_EDGE,
        JIRA_COMMENT_AUTHOR_EDGE,
        JIRA_COMMENT_REPLY_EDGE,
        JIRA_COMMENT_AFTER_EDGE,
    }
    assert mapped.document["doc_type"] == "jira"
    assert "PAY-123" in mapped.document["content"]
    assert "Increase the gateway timeout." not in mapped.document["content"]
    assert len(mapped.comments) == 2
    assert len(mapped.comments[0].chunks) == 1
    assert mapped.comments[0].chunks[0].chunk_id.startswith(
        "jira:cloud-1:comment:9001:chunk:0:"
    )
    assert "Increase the gateway timeout." in mapped.comments[0].chunks[0].text


def test_long_jira_comment_drops_log_heavy_blocks_before_chunking():
    log_lines = "\n".join(
        f"2026-09-28 12:00:{index:02d} ERROR request failed"
        for index in range(80)
    )
    issue = {
        "id": "10422",
        "key": "PAY-123",
        "fields": {
            "summary": "Login timeout",
            "project": {"id": "10001", "key": "PAY", "name": "Payments"},
            "comment": {
                "comments": [
                    {
                        "id": "9001",
                        "author": {
                            "accountId": "ada",
                            "displayName": "Ada",
                        },
                        "body": (
                            "The gateway failed during checkout.\n\n"
                            f"```\n{log_lines}\n```\n\n"
                            "Please inspect the timeout configuration."
                        ),
                    }
                ]
            },
        },
    }

    mapped = JiraIssueMapper(source(), "cloud-1").map(issue)
    content = "\n".join(
        chunk.text for chunk in mapped.comments[0].chunks
    )
    assert "The gateway failed during checkout." in content
    assert "Please inspect the timeout configuration." in content
    assert "[Log output omitted from search content.]" in content
    assert "ERROR request failed" not in content


def test_comment_chunks_are_upserted_directly_without_document(tmp_path, monkeypatch):
    issue = {
        "id": "10422",
        "key": "PAY-123",
        "fields": {
            "summary": "Login timeout",
            "project": {"id": "10001", "key": "PAY", "name": "Payments"},
            "comment": {
                "comments": [
                    {
                        "id": "9001",
                        "author": {
                            "accountId": "ada",
                            "displayName": "Ada",
                        },
                        "body": "Increase the gateway timeout.",
                    }
                ]
            },
        },
    }
    comment = JiraIssueMapper(source(), "cloud-1").map(issue).comments[0]
    service = JiraSyncService(
        "TestGraph",
        source(),
        object(),
        store=JiraSourceStore(str(tmp_path)),
        client=object(),
    )
    captured: dict = {}
    monkeypatch.setattr(
        service,
        "_upsert_records",
        lambda vertices, edges: captured.update(
            vertices=vertices,
            edges=edges,
        ),
    )

    service._upsert_comment_chunks([comment])

    assert {vertex.vertex_type for vertex in captured["vertices"]} == {
        "DocumentChunk",
        "Content",
    }
    assert "Document" not in {
        vertex.vertex_type for vertex in captured["vertices"]
    }
    chunk_id = comment.chunks[0].chunk_id
    assert {
        (edge.source_type, edge.edge_type, edge.target_type)
        for edge in captured["edges"]
    } == {
        ("DocumentChunk", "HAS_CONTENT", "Content"),
        ("DocumentChunk", "CONTAINS_ENTITY", "JiraComment"),
        ("DocumentChunk", "CONTAINS_ENTITY", "JiraIssue"),
    }
    chunk_vertex = next(
        vertex
        for vertex in captured["vertices"]
        if vertex.vertex_type == "DocumentChunk"
    )
    assert chunk_vertex.vertex_id == chunk_id
    assert chunk_vertex.attributes["epoch_processed"] == 0


def test_comment_chunks_are_embedded_by_existing_store(tmp_path, monkeypatch):
    issue = {
        "id": "10422",
        "key": "PAY-123",
        "fields": {
            "summary": "Login timeout",
            "project": {"id": "10001", "key": "PAY", "name": "Payments"},
            "comment": {
                "comments": [
                    {
                        "id": "9001",
                        "body": "Increase the gateway timeout.",
                    }
                ]
            },
        },
    }
    comment = JiraIssueMapper(source(), "cloud-1").map(issue).comments[0]
    embedded: list[tuple[list, list]] = []
    processed: list[tuple[str, str, dict]] = []

    class Store:
        async def aadd_embeddings(self, embeddings, metadatas):
            embedded.append((embeddings, metadatas))

    class Connection:
        def upsertVertex(self, vertex_type, vertex_id, attributes):
            processed.append((vertex_type, vertex_id, attributes))

    monkeypatch.setattr(
        "connectors.jira.sync.get_embedding_store",
        lambda **kwargs: Store(),
    )
    service = JiraSyncService(
        "TestGraph",
        source(),
        Connection(),
        store=JiraSourceStore(str(tmp_path)),
        client=object(),
    )

    service._embed_comment_chunks([comment])

    assert embedded == [
        (
            [(comment.chunks[0].text, [])],
            [
                {
                    "vertex_id": (
                        comment.chunks[0].chunk_id,
                        "DocumentChunk",
                    )
                }
            ],
        )
    ]
    assert processed[0][:2] == (
        "DocumentChunk",
        comment.chunks[0].chunk_id,
    )
    assert processed[0][2]["epoch_processed"] > 0


def test_comment_cleanup_removes_direct_and_legacy_content(tmp_path, monkeypatch):
    direct_chunk_id = "jira:cloud-1:comment:9001:chunk:0:abc"

    class Connection:
        def getEdges(self, vertex_type, vertex_id, edge_type):
            if vertex_type == "JiraComment":
                return [
                    {
                        "to_type": "DocumentChunk",
                        "to_id": direct_chunk_id,
                    }
                ]
            if vertex_type == "Document":
                return [{"to_id": "legacy-chunk"}]
            raise AssertionError((vertex_type, vertex_id, edge_type))

        def delVerticesById(self, vertex_type, vertex_ids):
            deleted.append((vertex_type, tuple(vertex_ids)))

    deleted: list[tuple[str, tuple[str, ...]]] = []
    service = JiraSyncService(
        "TestGraph",
        source(),
        Connection(),
        store=JiraSourceStore(str(tmp_path)),
        client=object(),
    )
    monkeypatch.setattr(
        "connectors.jira.sync.get_embedding_store",
        lambda **kwargs: type(
            "Store",
            (),
            {"remove_embeddings": lambda self, ids: None},
        )(),
    )

    service._delete_comment_content(
        "jira:cloud-1:issue:10422",
        "jira:cloud-1:comment:9001",
    )

    assert ("DocumentChunk", (direct_chunk_id,)) in deleted
    assert ("Content", (direct_chunk_id,)) in deleted
    assert ("DocumentChunk", ("legacy-chunk",)) in deleted
    assert (
        "Document",
        ("jira:cloud-1:issue:10422:comment-doc:9001",),
    ) in deleted


def test_legacy_comment_migration_resets_checkpoint_only_once(tmp_path):
    store = JiraSourceStore(str(tmp_path))
    configured = store.upsert("TestGraph", source())
    configured.sync.checkpoint = datetime(2026, 9, 24, tzinfo=timezone.utc)
    store.update_runtime_state("TestGraph", configured)

    class Connection:
        def getVertices(self, vertex_type, select=""):
            assert vertex_type == "Document"
            assert select == "id"
            return [
                {
                    "v_id": (
                        "jira:cloud-1:issue:10422:"
                        "comment-doc:9001"
                    )
                }
            ]

    service = JiraSyncService(
        "TestGraph",
        store.get("TestGraph", "jira-acme"),
        Connection(),
        store=store,
        client=object(),
    )
    service._prepare_legacy_comment_migration()

    migrated = store.get("TestGraph", "jira-acme")
    assert migrated.sync.checkpoint is None
    assert migrated.sync.migrating_legacy_comments is True

    migrated.sync.checkpoint = datetime(2026, 9, 25, tzinfo=timezone.utc)
    store.update_runtime_state("TestGraph", migrated)
    resumed = JiraSyncService(
        "TestGraph",
        store.get("TestGraph", "jira-acme"),
        Connection(),
        store=store,
        client=object(),
    )
    resumed._prepare_legacy_comment_migration()
    assert store.get(
        "TestGraph",
        "jira-acme",
    ).sync.checkpoint == datetime(2026, 9, 25, tzinfo=timezone.utc)


def test_schema_is_bounded():
    proposal = jira_schema_proposal()
    assert {vertex.name for vertex in proposal.vertices} == {
        "JiraComment",
        "JiraIssue",
        "JiraProject",
        "JiraUser",
    }
    assert {edge.name for edge in proposal.edges} == {
        JIRA_PROJECT_EDGE,
        JIRA_ASSIGNEE_EDGE,
        JIRA_REPORTER_EDGE,
        JIRA_PARENT_EDGE,
        JIRA_LINK_EDGE,
        JIRA_COMMENT_ISSUE_EDGE,
        JIRA_COMMENT_AUTHOR_EDGE,
        JIRA_COMMENT_REPLY_EDGE,
        JIRA_COMMENT_AFTER_EDGE,
    }


def test_store_only_preserves_manually_configured_token(tmp_path):
    store = JiraSourceStore(str(tmp_path))
    original = source()
    tested_at = datetime(2026, 9, 24, tzinfo=timezone.utc)
    store.upsert("TestGraph", original)
    config_path = tmp_path / "TestGraph" / "data_sources.json"
    payload = json.loads(config_path.read_text())
    assert "api_token" not in payload["sources"][0]["connection"]

    payload["sources"][0]["connection"]["api_token"] = "configured-secret"
    config_path.write_text(json.dumps(payload))
    runtime_source = store.get("TestGraph", "jira-acme")
    runtime_source.sync.last_tested_at = tested_at
    store.update_runtime_state("TestGraph", runtime_source)
    redacted = store.list("TestGraph")[0]
    assert redacted["connection"]["api_token"] == ""

    submitted = JiraDataSource.model_validate(redacted)
    submitted.display_name = "Renamed"
    store.upsert("TestGraph", submitted)
    assert (
        store.get("TestGraph", "jira-acme").connection.api_token
        == "configured-secret"
    )
    assert store.get("TestGraph", "jira-acme").sync.last_tested_at == tested_at

    changed_credentials = store.get("TestGraph", "jira-acme")
    changed_credentials.connection.api_token = "new-secret"
    store.upsert("TestGraph", changed_credentials)
    assert (
        store.get("TestGraph", "jira-acme").connection.api_token
        == "configured-secret"
    )

    submitted.display_name = "Renamed again"
    store.upsert("TestGraph", submitted)

def test_store_resets_checkpoint_when_ingestion_filters_change(tmp_path):
    store = JiraSourceStore(str(tmp_path))
    store.upsert("TestGraph", source())
    configured = store.get("TestGraph", "jira-acme")
    configured.sync.checkpoint = datetime(2026, 9, 24, tzinfo=timezone.utc)
    store.update_runtime_state("TestGraph", configured)

    changed = store.get("TestGraph", "jira-acme")
    changed.scope.created_after = date(2026, 1, 1)
    store.upsert("TestGraph", changed)

    assert store.get("TestGraph", "jira-acme").sync.checkpoint is None


class SchemaConnection:
    def __init__(self):
        proposal = jira_schema_proposal()
        self.vertices = {
            "Document": {},
            "DocumentChunk": {},
            "Content": {},
            **{
                vertex.name: {
                    "PrimaryId": {"AttributeName": "id"},
                    "Attributes": [
                        {
                            "AttributeName": attribute.name,
                            "AttributeType": {"Name": attribute.type},
                        }
                        for attribute in vertex.attributes
                    ],
                }
                for vertex in proposal.vertices
            },
        }
        self.edges = {
            edge.name: {
                "FromVertexTypeName": edge.pairs[0][0],
                "ToVertexTypeName": edge.pairs[0][1],
                "IsDirected": edge.directed,
                "Attributes": [
                    {
                        "AttributeName": attribute.name,
                        "AttributeType": {"Name": attribute.type},
                    }
                    for attribute in edge.attributes
                ],
            }
            for edge in proposal.edges
        }
        self.edges["CONTAINS_ENTITY"] = {
            "FromVertexTypeName": "*",
            "ToVertexTypeName": "*",
            "IsDirected": True,
            "EdgePairs": [
                {"From": "Document", "To": "JiraIssue"},
                {"From": "DocumentChunk", "To": "JiraIssue"},
                {"From": "Document", "To": "JiraComment"},
                {"From": "DocumentChunk", "To": "JiraComment"},
            ],
        }
        self.edges["HAS_CHILD"] = {
            "FromVertexTypeName": "Document",
            "ToVertexTypeName": "DocumentChunk",
            "IsDirected": True,
        }
        self.edges["HAS_CONTENT"] = {
            "FromVertexTypeName": "*",
            "ToVertexTypeName": "*",
            "IsDirected": True,
            "EdgePairs": [
                {"From": "Document", "To": "Content"},
                {"From": "DocumentChunk", "To": "Content"},
            ],
        }

    def getVertexTypes(self):
        return list(self.vertices)

    def getVertexType(self, name):
        return self.vertices[name]

    def getEdgeTypes(self):
        return list(self.edges)

    def getEdgeType(self, name):
        return self.edges[name]


def test_schema_status_detects_installed_and_conflicting_schema():
    conn = SchemaConnection()
    assert jira_schema_status(conn)["status"] == "installed"

    issue_attributes = conn.vertices["JiraIssue"]["Attributes"]
    next(
        attribute
        for attribute in issue_attributes
        if attribute["AttributeName"] == "story_points"
    )["AttributeType"]["Name"] = "STRING"
    result = jira_schema_status(conn)
    assert result["status"] == "conflict"
    assert "JiraIssue.story_points must be DOUBLE" in result["conflicts"][0]


def test_schema_status_marks_partial_graph_incomplete():
    conn = SchemaConnection()
    conn.vertices.pop("JiraComment")
    for edge_type in (
        JIRA_COMMENT_ISSUE_EDGE,
        JIRA_COMMENT_AUTHOR_EDGE,
        JIRA_COMMENT_REPLY_EDGE,
        JIRA_COMMENT_AFTER_EDGE,
    ):
        conn.edges.pop(edge_type)
    conn.edges["CONTAINS_ENTITY"]["EdgePairs"] = [
        pair
        for pair in conn.edges["CONTAINS_ENTITY"]["EdgePairs"]
        if pair["To"] != "JiraComment"
    ]

    result = jira_schema_status(conn)
    assert result["status"] == "incomplete"
