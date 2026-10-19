"""Zendesk connector card support: config, shared file, schema, and ingest."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from connectors.jira.config import JiraDataSource
from connectors.jira.schema import jira_schema_proposal
from connectors.jira.state import JiraSourceStore
from connectors.zendesk.config import ZendeskDataSource
from connectors.zendesk.schema import zendesk_schema_proposal
from connectors.zendesk.state import ZendeskSourceStore
from connectors.zendesk.sync import ZendeskSyncService


def _jira_source() -> JiraDataSource:
    return JiraDataSource.model_validate(
        {
            "id": "jira-acme",
            "type": "jira_cloud",
            "display_name": "Acme Jira",
            "connection": {
                "site_url": "https://acme.atlassian.net",
                "email": "dev@acme.com",
                "api_token": "secret",
            },
            "scope": {"project_keys": ["PAY"], "include_comments": True},
        }
    )


def _zendesk_source() -> ZendeskDataSource:
    return ZendeskDataSource.model_validate(
        {
            "id": "zendesk-acme",
            "type": "zendesk",
            "display_name": "Acme Zendesk",
            "connection": {
                "subdomain": "https://acme.zendesk.com",
                "email": "agent@acme.com",
                "api_token": "secret",
            },
            "scope": {"include_comments": True, "statuses": ["open", "open"]},
        }
    )


def test_subdomain_accepts_host_and_dedupes_status():
    source = _zendesk_source()
    assert source.connection.subdomain == "acme"
    assert source.scope.statuses == ["open"]


def test_jira_and_zendesk_sources_share_one_file(tmp_path):
    jira_store = JiraSourceStore(str(tmp_path))
    zendesk_store = ZendeskSourceStore(str(tmp_path))
    jira_store.upsert("TestGraph", _jira_source())
    zendesk_store.upsert("TestGraph", _zendesk_source())
    jira_store.upsert("TestGraph", _jira_source())

    document = json.loads(
        (tmp_path / "TestGraph" / "data_sources.json").read_text()
    )
    assert document["sources"][0]["id"] == "jira-acme"
    assert document["zendesk_sources"][0]["id"] == "zendesk-acme"
    assert "api_token" not in document["zendesk_sources"][0]["connection"]
    assert jira_store.list("TestGraph")[0]["type"] == "jira_cloud"
    assert zendesk_store.list("TestGraph")[0]["type"] == "zendesk"


def test_source_ids_cannot_collide(tmp_path):
    jira_store = JiraSourceStore(str(tmp_path))
    zendesk_store = ZendeskSourceStore(str(tmp_path))
    jira_store.upsert("TestGraph", _jira_source())
    copied = _zendesk_source()
    copied.id = "jira-acme"
    try:
        zendesk_store.upsert("TestGraph", copied)
    except ValueError as exc:
        assert "Jira" in str(exc)
    else:
        raise AssertionError("expected a source id collision")


def test_schema_adds_cross_edge_only_when_the_other_type_exists():
    alone = zendesk_schema_proposal()
    assert "CROSS_SOURCE_LINK" not in {edge.name for edge in alone.edges}
    together = zendesk_schema_proposal({"JiraIssue"})
    assert "CROSS_SOURCE_LINK" in {edge.name for edge in together.edges}
    jira_alone = jira_schema_proposal()
    assert "CROSS_SOURCE_LINK" not in {edge.name for edge in jira_alone.edges}
    jira_together = jira_schema_proposal({"ZendeskTicket"})
    assert "CROSS_SOURCE_LINK" in {edge.name for edge in jira_together.edges}


class _Client:
    def __init__(self):
        self.closed = False

    def iter_ticket_pages(self, start_time):
        assert start_time >= 1
        yield (
            [
                {
                    "id": 10,
                    "subject": "Login fails",
                    "description": "See https://acme.atlassian.net/browse/PAY-1",
                    "status": "open",
                    "requester_id": 7,
                    "updated_at": "2026-10-01T00:00:00Z",
                }
            ],
            1_700_000_000,
            True,
        )

    def comments(self, ticket_id):
        assert ticket_id == "10"
        return [{"id": 3, "body": "Tried again", "author_id": 7}]

    def users(self, user_ids):
        assert "7" in user_ids
        return {"7": {"id": 7, "name": "Ada"}}

    def close(self):
        self.closed = True


class _Conn:
    def __init__(self):
        self.payloads = []
        self.vertex_updates = []

    def upsertData(self, payload):
        self.payloads.append(json.loads(payload))

    def upsertVertex(self, vertex_type, vertex_id, attributes=None):
        self.vertex_updates.append((vertex_type, vertex_id, attributes))

    def getEdgeTypes(self):
        return ["CROSS_SOURCE_LINK", "PENDING_REF", "ZENDESK_REQUESTED_BY"]

    def getVertexTypes(self):
        return [
            "JiraIssue",
            "ZendeskTicket",
            "ZendeskUser",
            "ZendeskComment",
            "PendingRef",
            "PendingLink",
        ]

    def getEdges(self, *_args, **_kwargs):
        return []

    def delEdges(self, *_args, **_kwargs):
        return None

    def delVerticesById(self, *_args, **_kwargs):
        return None


def test_sync_writes_ticket_without_replacing_jira(tmp_path, monkeypatch):
    store = ZendeskSourceStore(str(tmp_path))
    source = _zendesk_source()
    store.upsert("TestGraph", source)
    saved = store.get("TestGraph", "zendesk-acme")
    saved.sync.last_tested_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    store.update_runtime_state("TestGraph", saved)
    conn = _Conn()
    service = ZendeskSyncService(
        "TestGraph",
        store.get("TestGraph", "zendesk-acme"),
        conn,
        store=store,
        client=_Client(),
    )
    monkeypatch.setattr(
        "connectors.zendesk.sync.zendesk_schema_status",
        lambda _conn: {"status": "installed"},
    )
    service._embed_chunks = lambda _chunks: None
    result = service.run()
    assert result["tickets"] == 1
    ticket_ids = []
    edge_names = []
    for payload in conn.payloads:
        ticket_ids.extend(payload["vertices"].get("ZendeskTicket", {}))
        for edges in payload["edges"].get("ZendeskTicket", {}).values():
            edge_names.extend(edges)
    assert ticket_ids == ["zendesk:acme:ticket:10"]
    assert "CROSS_SOURCE_LINK" in edge_names
    assert "JiraIssue" not in {
        vertex_type
        for payload in conn.payloads
        for vertex_type in payload["vertices"]
    }
    assert store.get("TestGraph", "zendesk-acme").sync.last_ticket_count == 1
