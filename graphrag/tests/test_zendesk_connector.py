"""Zendesk connector: schema, client, sync, and Jira link edges."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from connectors.jira.config import JiraDataSource
from connectors.jira.state import JiraSourceStore
from connectors.prompt_selection import allows_type, guidance_for
from connectors.schema_reuse import can_reuse_vertex, must_remain_separate
from connectors.zendesk.client import (
    ZendeskAPIError,
    ZendeskClient,
    export_start_time,
    ticket_search_query,
)
from connectors.zendesk.config import ZendeskDataSource
from connectors.zendesk.linking import normalized_link, reconcile_links
from connectors.zendesk.mapper import ZendeskMapper
from connectors.zendesk.schema import (
    LINKS_ISSUE,
    zendesk_schema_proposal,
    zendesk_schema_status,
)
from connectors.zendesk.state import ZendeskSourceStore
from connectors.zendesk.sync import ZendeskSyncService


def source(**overrides) -> ZendeskDataSource:
    payload = {
        "id": "zendesk-acme",
        "display_name": "Acme Zendesk",
        "connection": {
            "subdomain": "Acme",
            "email": "agent@example.com",
            "api_token": "secret",
        },
        "scope": {"statuses": ["open"], "updated_after": "2026-10-01T00:00:00Z"},
    }
    payload.update(overrides)
    return ZendeskDataSource.model_validate(payload)


def jira_source() -> JiraDataSource:
    return JiraDataSource.model_validate(
        {
            "id": "jira-acme",
            "display_name": "Acme Jira",
            "connection": {
                "site_url": "https://acme.atlassian.net",
                "email": "svc@example.com",
                "api_token": "secret",
            },
            "scope": {"project_keys": ["PAY"]},
        }
    )


class Response:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload

    @property
    def is_error(self):
        return self.status_code >= 400


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        key = (method, path.split("?", 1)[0])
        handler = self.routes[key]
        return handler(kwargs)

    def close(self):
        pass


class Graph:
    def __init__(self, vertices=None, vertex_types=None, edge_types=None):
        self.present = set(vertices or [])
        self.vertex_types = list(vertex_types or ["JiraIssue", "ZendeskTicket"])
        self.edge_types = list(edge_types or [LINKS_ISSUE])
        self.edges = {}
        self.payloads = []
        self.attrs = {}

    def getVertexTypes(self):
        return self.vertex_types

    def getEdgeTypes(self):
        return self.edge_types

    def getVerticesById(self, vertex_type, ids):
        return [
            {
                "v_id": vertex_id,
                "attributes": self.attrs.get((vertex_type, vertex_id), {}),
            }
            for vertex_id in ids
            if (vertex_type, vertex_id) in self.present
        ]

    def getEdges(self, vertex_type, vertex_id, edge_type):
        targets = self.edges.get((vertex_type, vertex_id, edge_type), set())
        return [
            {"to_id": target_id, "to_type": target_type}
            for target_type, target_id in targets
        ]

    def delEdges(self, vertex_type, vertex_id, edge_type):
        self.edges.pop((vertex_type, vertex_id, edge_type), None)

    def upsertData(self, payload):
        data = json.loads(payload)
        self.payloads.append(data)
        for vertex_type, items in data["vertices"].items():
            for vertex_id, attributes in items.items():
                self.present.add((vertex_type, vertex_id))
                stored = self.attrs.setdefault((vertex_type, vertex_id), {})
                for name, value in attributes.items():
                    stored[name] = value.get("value") if isinstance(value, dict) else value
        for source_type, sources in data["edges"].items():
            for source_id, edge_types in sources.items():
                for edge_type, targets in edge_types.items():
                    bucket = self.edges.setdefault(
                        (source_type, source_id, edge_type),
                        set(),
                    )
                    for target_type, target_ids in targets.items():
                        for target_id in target_ids:
                            bucket.add((target_type, target_id))
        return {}

    def delVerticesById(self, vertex_type, ids):
        for vertex_id in ids:
            self.present.discard((vertex_type, vertex_id))

    def upsertVertex(self, vertex_type, vertex_id, attributes=None):
        self.present.add((vertex_type, vertex_id))
        self.attrs.setdefault((vertex_type, vertex_id), {}).update(attributes or {})

    def getVertices(self, vertex_type, where="", **kwargs):
        needle = ""
        if isinstance(where, str) and 'email="' in where:
            needle = where.split('email="', 1)[1].split('"', 1)[0].casefold()
        rows = []
        for (stored_type, vertex_id), attributes in self.attrs.items():
            if stored_type != vertex_type or (stored_type, vertex_id) not in self.present:
                continue
            email = str(attributes.get("email") or "").casefold()
            if needle and email != needle:
                continue
            rows.append({"v_id": vertex_id, "attributes": attributes})
        return rows

    def link_targets(self, ticket_vertex_id):
        return {
            target_id
            for target_type, target_id in self.edges.get(
                ("ZendeskTicket", ticket_vertex_id, LINKS_ISSUE),
                set(),
            )
        }


class SchemaConnection:
    def __init__(self, *, with_jira=False, drop_document=False):
        names = {"JiraIssue"} if with_jira else set()
        proposal = zendesk_schema_proposal(names)
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
        if with_jira:
            self.vertices["JiraIssue"] = {
                "PrimaryId": {"AttributeName": "id"},
                "Attributes": [
                    {
                        "AttributeName": "issue_key",
                        "AttributeType": {"Name": "STRING"},
                    }
                ],
            }
        if drop_document:
            self.vertices.pop("Document")
        self.edges = {
            edge.name: {
                "FromVertexTypeName": edge.pairs[0][0],
                "ToVertexTypeName": edge.pairs[0][1],
                "IsDirected": edge.directed,
                "Attributes": [],
                "EdgePairs": [
                    {"From": source, "To": target} for source, target in edge.pairs
                ],
            }
            for edge in proposal.edges
        }
        self.edges["CONTAINS_ENTITY"] = {
            "FromVertexTypeName": "*",
            "ToVertexTypeName": "*",
            "IsDirected": True,
            "EdgePairs": [
                {"From": container, "To": name}
                for name in ("ZendeskTicket", "ZendeskComment")
                for container in ("Document", "DocumentChunk")
            ],
        }
        self.edges["HAS_CHILD"] = {
            "FromVertexTypeName": "Document",
            "ToVertexTypeName": "DocumentChunk",
            "IsDirected": True,
        }
        self.edges["HAS_CONTENT"] = {
            "FromVertexTypeName": "DocumentChunk",
            "ToVertexTypeName": "Content",
            "IsDirected": True,
        }

    def getVertexTypes(self):
        return list(self.vertices)

    def getVertexType(self, name):
        return self.vertices[name]

    def getEdgeTypes(self):
        return list(self.edges)

    def getEdgeType(self, name):
        return self.edges[name]


class Embeddings:
    def __init__(self):
        self.added = []

    async def aadd_embeddings(self, texts, metadatas):
        self.added.append((texts, metadatas))

    def remove_embeddings(self, ids=None):
        return None


class FakeClient:
    def __init__(self, pages, comments=None, users=None, links=None, fail_comments=(), organizations=None, groups=None):
        self.pages = pages
        self.comments_by_ticket = comments or {}
        self.user_rows = users or []
        self.organization_rows = organizations or []
        self.group_rows = groups or []
        self.link_pages = links if links is not None else [[]]
        self.fail_comments = {str(ticket_id) for ticket_id in fail_comments}
        self.ticket_calls = []

    def iter_ticket_pages(self):
        yield from self.pages

    def ticket(self, ticket_id):
        self.ticket_calls.append(str(ticket_id))
        return {
            "id": int(ticket_id),
            "subject": "Retried",
            "description": "again",
            "status": "open",
            "requester_id": 7,
            "updated_at": "2026-10-02T00:00:00Z",
            "url": f"https://acme.zendesk.com/api/v2/tickets/{ticket_id}.json",
        }

    def comments(self, ticket_id):
        if str(ticket_id) in self.fail_comments:
            raise RuntimeError("comments failed")
        return list(self.comments_by_ticket.get(str(ticket_id), []))

    def users(self, user_ids):
        wanted = {str(user_id) for user_id in user_ids}
        return [user for user in self.user_rows if str(user["id"]) in wanted]

    def organizations(self, organization_ids):
        wanted = {str(item_id) for item_id in organization_ids}
        return [row for row in self.organization_rows if str(row["id"]) in wanted]

    def groups(self, group_ids):
        wanted = {str(item_id) for item_id in group_ids}
        return [row for row in self.group_rows if str(row["id"]) in wanted]

    def audits(self, ticket_id):
        return []

    def iter_jira_link_pages(self):
        if isinstance(self.link_pages, Exception):
            raise self.link_pages
        yield from self.link_pages

    def close(self):
        return None


def test_schema_keeps_ticket_separate_and_adds_jira_edge_only_when_installed():
    proposal = zendesk_schema_proposal()
    assert {vertex.name for vertex in proposal.vertices} == {
        "ZendeskTicket",
        "Person",
        "Organization",
        "ZendeskComment",
        "ZendeskChange",
        "Attachment",
    }
    assert LINKS_ISSUE not in {edge.name for edge in proposal.edges}
    linked = zendesk_schema_proposal({"JiraIssue"})
    edge = next(edge for edge in linked.edges if edge.name == LINKS_ISSUE)
    assert edge.pairs == [("ZendeskTicket", "JiraIssue")]
    assert must_remain_separate("ZendeskTicket", "JiraIssue")
    assert not can_reuse_vertex(
        "ZendeskTicket",
        {"ticket_id": "STRING"},
        "JiraIssue",
        {"ticket_id": "STRING", "issue_key": "STRING"},
    )
    assert can_reuse_vertex(
        "Person",
        {"email": "STRING", "display_name": "STRING"},
        "person",
        {"email": "STRING", "display_name": "STRING"},
    )


def test_schema_status_reports_installed_conflict_and_uninitialized():
    assert zendesk_schema_status(SchemaConnection())["status"] == "installed"
    assert zendesk_schema_status(SchemaConnection(with_jira=True))["status"] == "installed"
    conflict = SchemaConnection()
    conflict.vertices["ZendeskTicket"]["Attributes"][0]["AttributeType"]["Name"] = "INT"
    result = zendesk_schema_status(conflict)
    assert result["status"] == "conflict"
    assert "ZendeskTicket.ticket_id must be STRING" in result["conflicts"][0]
    missing_jira_edge = SchemaConnection()
    missing_jira_edge.vertices["JiraIssue"] = {
        "PrimaryId": {"AttributeName": "id"},
        "Attributes": [],
    }
    partial = zendesk_schema_status(missing_jira_edge)
    assert partial["status"] == "incomplete"
    assert any(LINKS_ISSUE in pair for pair in partial["missing"]["edges"])
    assert zendesk_schema_status(SchemaConnection(drop_document=True))["status"] == "not_initialized"
    missing_priority = SchemaConnection()
    missing_priority.vertices["ZendeskTicket"]["Attributes"] = [
        attribute
        for attribute in missing_priority.vertices["ZendeskTicket"]["Attributes"]
        if attribute["AttributeName"] != "priority"
    ]
    partial_attributes = zendesk_schema_status(missing_priority)
    assert partial_attributes["status"] == "incomplete"
    assert "ZendeskTicket.priority" in partial_attributes["missing"]["attributes"]


def test_client_uses_token_auth_and_documented_endpoints(monkeypatch):
    created = {}

    class RecordingClient:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.routes = FakeHttp(
                {
                    ("GET", "/api/v2/users/me.json"): lambda kwargs: Response(
                        200, {"user": {"id": 1, "name": "Ada"}}
                    ),
                    ("GET", "/api/v2/search/count.json"): lambda kwargs: Response(
                        200, {"count": 3}
                    ),
                    ("GET", "/api/v2/incremental/tickets/cursor.json"): lambda kwargs: (
                        Response(
                            200,
                            {
                                "tickets": [{"id": 10, "generated_timestamp": 1700000000}],
                                "end_of_stream": False,
                                "after_cursor": "cursor-token",
                            },
                        )
                        if kwargs.get("params", {}).get("start_time")
                        else Response(
                            200,
                            {
                                "tickets": [],
                                "end_of_stream": True,
                                "after_cursor": "cursor-token-2",
                            },
                        )
                    ),
                    ("GET", "/api/v2/tickets/10/comments.json"): lambda kwargs: Response(
                        200,
                        {
                            "comments": [{"id": 3, "author_id": 7, "body": "Tried again"}],
                            "next_page": "https://acme.zendesk.com/api/v2/tickets/10/comments.json?page=2",
                        },
                    ),
                    ("GET", "https://acme.zendesk.com/api/v2/tickets/10/comments.json"): lambda kwargs: Response(
                        200, {"comments": [{"id": 4, "body": "next"}], "next_page": None}
                    ),
                    ("GET", "/api/v2/users/show_many.json"): lambda kwargs: Response(
                        200, {"users": [{"id": 7, "name": "Ada"}]}
                    ),
                    ("GET", "/api/v2/integrations/jira/ext-1/links"): lambda kwargs: Response(
                        200,
                        {
                            "links": [{"issue_key": "TSE-1073", "ticket_id": "59326"}],
                            "meta": {"has_more": False, "after": ""},
                        },
                    ),
                }
            )

        def request(self, method, path, **kwargs):
            return self.routes.request(method, path, **kwargs)

        def close(self):
            return None

    holder = {}

    class BoundClient(RecordingClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            holder["client"] = self

    monkeypatch.setattr("connectors.zendesk.client.httpx.Client", BoundClient)
    configured = source()
    configured.scope.jira_integration_external_id = "ext-1"
    with ZendeskClient(configured) as client:
        assert client.me()["name"] == "Ada"
        assert client.count_tickets() == 3
        pages = list(client.iter_ticket_pages())
        assert pages[0][0][0]["id"] == 10
        assert pages[0][2] is False
        assert [comment["id"] for comment in client.comments("10")] == [3, 4]
        assert client.users(["7"])[0]["name"] == "Ada"
        assert list(client.iter_jira_link_pages())[0][0]["issue_key"] == "TSE-1073"
    calls = holder["client"].routes.calls
    cursor_calls = [
        call for call in calls
        if "incremental/tickets" in call[1]
        and (call[2].get("params") or {}).get("cursor") == "cursor-token"
    ]
    assert cursor_calls
    assert created["base_url"] == "https://acme.zendesk.com"
    assert created["auth"] == ("agent@example.com/token", "secret")


def test_link_inventory_uses_integration_id_when_legacy_links_are_rejected(monkeypatch):
    class RecordingClient:
        def __init__(self, **kwargs):
            self.routes = FakeHttp(
                {
                    ("GET", "/api/v2/jira/links"): lambda kwargs: Response(
                        422,
                        {"errors": [{"title": "Legacy Jira links are not available"}]},
                    ),
                    ("GET", "/api/services/jira/links"): lambda kwargs: Response(
                        404,
                        {"errors": [{"title": "Not found"}]},
                    ),
                    ("GET", "/api/v2/integrations/jira"): lambda kwargs: Response(
                        200,
                        {"integrations": [{"external_id": "ext-9", "name": "Jira"}]},
                    ),
                    ("GET", "/api/v2/integrations/jira/ext-9/links"): lambda kwargs: Response(
                        200,
                        {
                            "links": [{"issue_key": "TSE-1073", "ticket_id": "59326"}],
                            "meta": {"has_more": False},
                        },
                    ),
                }
            )

        def request(self, method, path, **kwargs):
            return self.routes.request(method, path, **kwargs)

        def close(self):
            return None

    monkeypatch.setattr("connectors.zendesk.client.httpx.Client", RecordingClient)
    with ZendeskClient(source()) as client:
        pages = list(client.iter_jira_link_pages())
    assert pages[0][0]["issue_key"] == "TSE-1073"


def test_link_pages_follow_the_cursor_url(monkeypatch):
    calls = []

    class RecordingClient:
        def __init__(self, **kwargs):
            self.routes = FakeHttp(
                {
                    ("GET", "/api/v2/jira/links"): lambda kwargs: Response(
                        200,
                        {
                            "links": [{"issue_key": "TSE-1", "ticket_id": "9"}],
                            "meta": {
                                "has_more": True,
                                "after_cursor": "https://acme.zendesk.com/api/v2/jira/links?page[after]=46462872",
                            },
                        },
                    ),
                    ("GET", "https://acme.zendesk.com/api/v2/jira/links"): lambda kwargs: Response(
                        200,
                        {
                            "links": [{"issue_key": "TSE-1073", "ticket_id": "59326"}],
                            "meta": {"has_more": False},
                        },
                    ),
                }
            )

        def request(self, method, path, **kwargs):
            calls.append((method, path, kwargs.get("params")))
            return self.routes.request(method, path, **kwargs)

        def close(self):
            return None

    monkeypatch.setattr("connectors.zendesk.client.httpx.Client", RecordingClient)
    with ZendeskClient(source()) as client:
        pages = list(client.iter_jira_link_pages())
    assert [row["issue_key"] for page in pages for row in page] == ["TSE-1", "TSE-1073"]
    assert calls[1][1] == "https://acme.zendesk.com/api/v2/jira/links?page[after]=46462872"
    assert calls[1][2] is None


def test_search_query_and_export_start_follow_scope():
    configured = source()
    assert ticket_search_query(configured) == "type:ticket updated>=2026-10-01 status:open"
    several = source()
    several.scope.statuses = ["open", "pending"]
    assert "status:open OR status:pending" in ticket_search_query(several)
    assert export_start_time(source()) == int(
        datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
    )
    checkpointed = source()
    checkpointed.sync.checkpoint = datetime(2026, 10, 5, tzinfo=timezone.utc)
    checkpointed.sync.overlap_seconds = 120
    assert export_start_time(checkpointed) == int(
        datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp()
    ) - 120
    unset = source()
    unset.scope.updated_after = None
    unset.sync.checkpoint = None
    assert export_start_time(unset) == 1


def test_link_rows_require_an_issue_key_and_numeric_ticket():
    assert normalized_link(
        {"issue_key": "TSE-1073", "ticket_id": "59326", "issue_id": "10001"}
    ) == ("TSE-1073", "59326")
    assert normalized_link({"issue_key": "see PAY-1", "ticket_id": "10"}) is None
    assert normalized_link({"issue_key": "PAY-1", "ticket_id": "ticket-10"}) is None
    assert normalized_link({"issue_key": "jira_escalated", "ticket_id": "10"}) is None


def test_reconcile_links_existing_issue_and_creates_a_placeholder_for_a_missing_one():
    ticket = "zendesk:acme:ticket:59326"
    issue = "jira:tse-1073:issue"
    other = "jira:pay-1:issue"
    graph = Graph(vertices={("ZendeskTicket", ticket), ("JiraIssue", issue), ("JiraIssue", other)})
    graph.edges[("ZendeskTicket", ticket, LINKS_ISSUE)] = {("JiraIssue", other)}
    result = reconcile_links(
        graph,
        "acme",
        [[{"issue_key": "TSE-1073", "ticket_id": "59326"}, {"issue_key": "not a key", "ticket_id": "1"}]],
        [],
    )
    assert result.links_written == 1
    assert graph.link_targets(ticket) == {issue}

    missing_ticket = "zendesk:acme:ticket:1"
    missing_issue = "jira:gle-9999:issue"
    absent = Graph(vertices={("ZendeskTicket", missing_ticket)})
    absent.attrs[("ZendeskTicket", missing_ticket)] = {
        "subject": "Missing issue",
        "description": "Steps",
    }
    created = reconcile_links(
        absent,
        "acme",
        [[{"issue_key": "GLE-9999", "ticket_id": "1"}]],
        [],
    )
    assert created.links_written == 1
    assert absent.link_targets(missing_ticket) == {missing_issue}
    assert absent.attrs[("JiraIssue", missing_issue)]["issue_key"] == "GLE-9999"
    assert absent.attrs[("JiraIssue", missing_issue)]["summary"] == "Missing issue"
    assert absent.attrs[("JiraIssue", missing_issue)]["description"] == "Steps"

    filled = Graph(vertices={("ZendeskTicket", ticket), ("JiraIssue", issue)})
    filled.attrs[("JiraIssue", issue)] = {"issue_key": "TSE-1073", "summary": "RCA"}
    reconcile_links(
        filled,
        "acme",
        [[{"issue_key": "TSE-1073", "ticket_id": "59326"}]],
        [],
    )
    assert filled.attrs[("JiraIssue", issue)]["summary"] == "RCA"
    assert filled.link_targets(ticket) == {issue}

    removed = Graph(vertices={("ZendeskTicket", ticket), ("JiraIssue", issue)})
    removed.edges[("ZendeskTicket", ticket, LINKS_ISSUE)] = {("JiraIssue", issue)}
    reconcile_links(removed, "acme", [[]], ["59326"])
    assert removed.link_targets(ticket) == set()


def _install_sync_patches(monkeypatch):
    embeddings = Embeddings()
    monkeypatch.setattr(
        "connectors.zendesk.sync.zendesk_schema_status",
        lambda conn: {"status": "installed"},
    )
    monkeypatch.setattr(
        "connectors.zendesk.mapper.get_graphrag_config",
        lambda graphname=None: {"chunker_config": {"chunk_size": 4000, "overlap_size": 0}},
    )
    monkeypatch.setattr(
        "connectors.zendesk.sync.get_embedding_store",
        lambda graphname: embeddings,
    )
    return embeddings


def _ticket(ticket_id=10, **overrides):
    payload = {
        "id": ticket_id,
        "subject": "Login fails",
        "description": "See https://acme.atlassian.net/browse/PAY-1",
        "status": "open",
        "requester_id": 7,
        "updated_at": "2026-10-01T00:00:00Z",
        "url": f"https://acme.zendesk.com/api/v2/tickets/{ticket_id}.json",
    }
    payload.update(overrides)
    return payload


def test_sync_writes_records_chunks_and_checkpoint_without_inferring_links(
    tmp_path, monkeypatch
):
    embeddings = _install_sync_patches(monkeypatch)
    store = ZendeskSourceStore(str(tmp_path))
    saved = store.upsert("TestGraph", source())
    graph = Graph(vertex_types=["ZendeskTicket"], edge_types=[])
    client = FakeClient(
        pages=[([_ticket(), _ticket(11, status="closed", updated_at="2020-01-01T00:00:00Z")], 1700000000, True, None)],
        comments={"10": [{"id": 3, "author_id": 7, "body": "Tried again"}, {"id": 4, "author_id": 7, "body": "  "}]},
        users=[{"id": 7, "name": "Ada"}],
        links=[[]],
    )
    result = ZendeskSyncService(
        "TestGraph",
        saved,
        graph,
        store=store,
        client=client,
    ).run()
    assert result["tickets_upserted"] == 1
    assert result["comments_upserted"] == 1
    assert result["links_written"] == 0
    assert ("Document", "anything") not in graph.present
    assert ("ZendeskTicket", "zendesk:acme:ticket:10") in graph.present
    assert ("ZendeskComment", "zendesk:acme:comment:3") in graph.present
    assert graph.link_targets("zendesk:acme:ticket:10") == set()
    texts = [
        text
        for batch, _metadatas in embeddings.added
        for text, _meta in batch
    ]
    assert any("Login fails" in text and "PAY-1" in text for text in texts)
    assert any("Tried again" in text and "Ada" in text for text in texts)
    assert store.get("TestGraph", "zendesk-acme").sync.checkpoint == datetime(
        2026, 10, 1, tzinfo=timezone.utc
    )
    assert store.get("TestGraph", "zendesk-acme").sync.last_ticket_id == 10


def test_sync_writes_priority_group_person_and_organization(tmp_path, monkeypatch):
    _install_sync_patches(monkeypatch)
    store = ZendeskSourceStore(str(tmp_path))
    saved = store.upsert("TestGraph", source())
    graph = Graph(
        vertex_types=["ZendeskTicket", "Person", "Organization", "JiraIssue"],
        edge_types=[LINKS_ISSUE, "requested", "member"],
    )
    client = FakeClient(
        pages=[
            (
                [_ticket(priority="high", organization_id=42, group_id=9)],
                1700000000,
                True,
                None,
            )
        ],
        users=[{"id": 7, "name": "Ada", "email": "Ada@acme.com", "organization_id": 42}],
        organizations=[{"id": 42, "name": "Acme", "domain_names": ["acme.com"]}],
        groups=[{"id": 9, "name": "Support"}],
        links=[[]],
    )
    ZendeskSyncService(
        "TestGraph", saved, graph, store=store, client=client
    ).run()
    ticket = graph.attrs[("ZendeskTicket", "zendesk:acme:ticket:10")]
    assert ticket["priority"] == "high"
    assert ticket["description"]
    assert ticket["group_name"] == "Support"
    assert graph.attrs[("Person", "person:ada@acme.com")]["email"] == "ada@acme.com"
    assert ("Person", "person:ada@acme.com") in graph.edges[
        ("Organization", "organization:acme.com", "member")
    ]


def test_comment_failure_is_retried_without_blocking_the_ticket(tmp_path, monkeypatch):
    _install_sync_patches(monkeypatch)
    store = ZendeskSourceStore(str(tmp_path))
    saved = store.upsert("TestGraph", source())
    graph = Graph()
    failing = FakeClient(
        pages=[([_ticket()], 1700000000, True, None)],
        fail_comments=["10"],
        users=[{"id": 7, "name": "Ada"}],
    )
    result = ZendeskSyncService(
        "TestGraph", saved, graph, store=store, client=failing
    ).run()
    assert result["pending_comment_ticket_ids"] == ["10"]
    assert ("ZendeskTicket", "zendesk:acme:ticket:10") in graph.present
    assert ("ZendeskComment", "zendesk:acme:comment:3") not in graph.present

    saved = store.get("TestGraph", "zendesk-acme")
    retry = FakeClient(
        pages=[],
        comments={"10": [{"id": 3, "author_id": 7, "body": "Tried again"}]},
        users=[{"id": 7, "name": "Ada"}],
    )
    retried = ZendeskSyncService(
        "TestGraph", saved, graph, store=store, client=retry
    ).run()
    assert retried["pending_comment_ticket_ids"] == []
    assert retry.ticket_calls == ["10"]
    assert ("ZendeskComment", "zendesk:acme:comment:3") in graph.present


def test_link_inventory_failure_does_not_roll_back_tickets(tmp_path, monkeypatch):
    _install_sync_patches(monkeypatch)
    store = ZendeskSourceStore(str(tmp_path))
    saved = store.upsert("TestGraph", source())
    graph = Graph()
    client = FakeClient(
        pages=[([_ticket()], 1700000000, True, None)],
        users=[{"id": 7, "name": "Ada"}],
        links=RuntimeError("inventory down"),
    )
    result = ZendeskSyncService(
        "TestGraph", saved, graph, store=store, client=client
    ).run()
    assert result["tickets_upserted"] == 1
    assert result["last_link_error"] == "inventory down"
    stored = store.get("TestGraph", "zendesk-acme")
    assert stored.sync.last_error is None
    assert stored.sync.last_link_error == "inventory down"
    assert stored.sync.checkpoint == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert stored.sync.last_ticket_id == 10


def test_sync_refuses_disabled_source_and_missing_schema(tmp_path, monkeypatch):
    store = ZendeskSourceStore(str(tmp_path))
    saved = store.upsert("TestGraph", source())
    saved.enabled = False
    with pytest.raises(ValueError, match="disabled"):
        ZendeskSyncService("TestGraph", saved, Graph(), store=store, client=FakeClient(pages=[])).run()
    saved.enabled = True
    monkeypatch.setattr(
        "connectors.zendesk.sync.zendesk_schema_status",
        lambda conn: {"status": "not_installed"},
    )
    with pytest.raises(RuntimeError, match="not installed"):
        ZendeskSyncService("TestGraph", saved, Graph(), store=store, client=FakeClient(pages=[])).run()


def test_sync_links_an_ingested_jira_issue(tmp_path, monkeypatch):
    _install_sync_patches(monkeypatch)
    store = ZendeskSourceStore(str(tmp_path))
    saved = store.upsert("TestGraph", source())
    graph = Graph(
        vertices={("JiraIssue", "jira:tse-1073:issue")},
    )
    client = FakeClient(
        pages=[([_ticket(59326)], 1700000000, True, None)],
        users=[{"id": 7, "name": "Ada"}],
        links=[[{"issue_key": "TSE-1073", "ticket_id": "59326", "issue_id": "10001"}]],
    )
    result = ZendeskSyncService(
        "TestGraph", saved, graph, store=store, client=client
    ).run()
    assert result["links_written"] == 1
    assert graph.link_targets("zendesk:acme:ticket:59326") == {"jira:tse-1073:issue"}
    assert store.get("TestGraph", "zendesk-acme").sync.linked_ticket_ids == ["59326"]


def test_source_ids_cannot_collide_and_jira_save_keeps_zendesk(tmp_path):
    jira_store = JiraSourceStore(str(tmp_path))
    zendesk_store = ZendeskSourceStore(str(tmp_path))
    jira_store.upsert("TestGraph", jira_source())
    with pytest.raises(ValueError, match="already used"):
        zendesk_store.upsert("TestGraph", source(id="jira-acme"))
    zendesk_store.upsert("TestGraph", source())
    renamed = jira_store.get("TestGraph", "jira-acme")
    renamed.display_name = "Renamed Jira"
    jira_store.upsert("TestGraph", renamed)
    document = json.loads((tmp_path / "TestGraph" / "data_sources.json").read_text())
    assert document["sources"][0]["display_name"] == "Renamed Jira"
    assert document["zendesk_sources"][0]["id"] == "zendesk-acme"
    assert "api_token" not in document["zendesk_sources"][0]["connection"]


def test_missing_ticket_history_is_not_an_auth_failure():
    class AuditClient:
        def audits(self, ticket_id):
            if str(ticket_id) == "25":
                raise ZendeskAPIError(404, "/api/v2/tickets/25/audits.json: Not found")
            if str(ticket_id) == "26":
                raise ZendeskAPIError(401, "/api/v2/tickets/26/audits.json: Couldn't authenticate you")
            return [{"id": 1, "author_id": 7, "events": []}]

    service = ZendeskSyncService.__new__(ZendeskSyncService)
    service.client = AuditClient()
    found = service._audits_for([{"id": 10}, {"id": 25}])
    assert found["10"] == [{"id": 1, "author_id": 7, "events": []}]
    assert found["25"] == []
    with pytest.raises(ZendeskAPIError, match="rejected the API token"):
        service._audits_for([{"id": 26}])


def test_open_zendesk_question_is_search_and_an_unknown_field_is_refused():
    planner = guidance_for("What went wrong on Zendesk for this customer?", "planner")
    assert "word search" in planner
    answer = guidance_for("Who is the assignee of Zendesk ticket 10?", "answer")
    assert "not on the ticket" in answer
    assert guidance_for("What is the ticket status?", "planner") == (
        "The source system is ambiguous. Ask the user whether they mean a Jira "
        "issue or a Zendesk ticket before answering, unless the conversation has "
        "already established the source. Do not guess from a bare number, and do "
        "not apply either connector's rules until the source is known."
    )
    guidance = guidance_for(
        "How is Zendesk ticket 59326 linked to TSE-1073?",
        "query",
    )
    assert "links_issue" in guidance
    assert "CROSS_SOURCE_LINK" not in guidance
    assert "ZendeskTicketRef" not in guidance
    assert allows_type("ZendeskTicket", {"jira"})
    assert allows_type("JiraIssue", {"zendesk"})
    assert not allows_type("ZendeskTicketRef", {"jira", "zendesk"})


def test_mapper_omits_empty_comments_and_uses_subdomain_ids(monkeypatch):
    monkeypatch.setattr(
        "connectors.zendesk.mapper.get_graphrag_config",
        lambda graphname=None: {"chunker_config": {"chunk_size": 4000, "overlap_size": 0}},
    )
    mapped = ZendeskMapper(source()).map_ticket(
        _ticket(),
        [{"id": 3, "author_id": 7, "body": "Tried again"}, {"id": 8, "body": ""}],
        {"7": "Ada"},
    )
    assert mapped.vertex_id == "zendesk:acme:ticket:10"
    assert mapped.comments[0].vertex_id == "zendesk:acme:comment:3"
    assert len(mapped.comments) == 1
    assert "Login fails" in mapped.ticket_chunks[0].text
    assert mapped.ticket_chunks[0].text.splitlines()[0] == "10"


def test_unchanged_ticket_is_not_embedded_again(tmp_path, monkeypatch):
    embeddings = _install_sync_patches(monkeypatch)
    store = ZendeskSourceStore(str(tmp_path))
    saved = store.upsert("TestGraph", source())
    graph = Graph()
    client = FakeClient(
        pages=[([_ticket()], 1700000000, True, None)],
        comments={"10": [{"id": 3, "author_id": 7, "body": "Tried again"}]},
        users=[{"id": 7, "name": "Ada"}],
    )
    ZendeskSyncService("TestGraph", saved, graph, store=store, client=client).run()
    embedded = len(embeddings.added)
    assert embedded > 0
    saved = store.get("TestGraph", "zendesk-acme")
    saved.sync.checkpoint = None
    ZendeskSyncService("TestGraph", saved, graph, store=store, client=client).run()
    assert len(embeddings.added) == embedded


def test_failed_embedding_retries_only_the_unfinished_ticket(tmp_path, monkeypatch):
    embeddings = _install_sync_patches(monkeypatch)

    class FailingEmbeddings(Embeddings):
        async def aadd_embeddings(self, texts, metadatas):
            raise RuntimeError("embed failed")

    failing = FailingEmbeddings()
    monkeypatch.setattr(
        "connectors.zendesk.sync.get_embedding_store",
        lambda graphname: failing,
    )
    store = ZendeskSourceStore(str(tmp_path))
    saved = store.upsert("TestGraph", source())
    graph = Graph()
    client = FakeClient(
        pages=[([_ticket()], 1700000000, True, None)],
        comments={"10": [{"id": 3, "author_id": 7, "body": "Tried again"}]},
        users=[{"id": 7, "name": "Ada"}],
    )
    ZendeskSyncService("TestGraph", saved, graph, store=store, client=client).run()
    stored = store.get("TestGraph", "zendesk-acme")
    assert stored.sync.checkpoint is None
    assert stored.sync.failed_ticket_ids == ["10"]
    assert graph.attrs[("ZendeskTicket", "zendesk:acme:ticket:10")].get("content_hash") in (None, "")

    monkeypatch.setattr(
        "connectors.zendesk.sync.get_embedding_store",
        lambda graphname: embeddings,
    )
    saved = store.get("TestGraph", "zendesk-acme")
    ZendeskSyncService("TestGraph", saved, graph, store=store, client=client).run()
    assert store.get("TestGraph", "zendesk-acme").sync.failed_ticket_ids == []
    assert store.get("TestGraph", "zendesk-acme").sync.checkpoint == datetime(
        2026, 10, 1, tzinfo=timezone.utc
    )
    assert graph.attrs[("ZendeskTicket", "zendesk:acme:ticket:10")]["content_hash"]
    assert embeddings.added
