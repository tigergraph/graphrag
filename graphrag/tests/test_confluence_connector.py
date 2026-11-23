"""Confluence connector: schema, client, and connection test."""

from __future__ import annotations

import json

import pytest

from connectors.confluence.client import (
    ConfluenceAPIError,
    ConfluenceClient,
    content_cql,
    wiki_base_url,
)
from connectors.confluence.config import ConfluenceDataSource
from connectors.confluence.schema import (
    LINKS_ISSUE,
    LINKS_TICKET,
    confluence_schema_proposal,
    confluence_schema_status,
)
from connectors.confluence.service import ConnectorCallError
from connectors.confluence.service import test_connection as check_connection
from connectors.confluence.state import ConfluenceSourceStore
from connectors.jira.state import JiraSourceStore
from connectors.schema_reuse import can_reuse_vertex, must_remain_separate
from connectors.zendesk.config import ZendeskDataSource
from connectors.zendesk.state import ZendeskSourceStore


def source(**overrides) -> ConfluenceDataSource:
    payload = {
        "id": "conf-acme",
        "display_name": "Acme Confluence",
        "connection": {
            "site": "https://acme.atlassian.net/wiki",
            "email": "svc@example.com",
            "api_token": "secret",
        },
        "scope": {
            "space_keys": ["DOCS"],
            "content_types": ["page"],
            "updated_after": "2026-07-01T00:00:00Z",
        },
    }
    payload.update(overrides)
    return ConfluenceDataSource.model_validate(payload)


def jira_source():
    from connectors.jira.config import JiraDataSource

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
        self.base_url = "https://acme.atlassian.net/wiki"

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        key = (method, path.split("?", 1)[0])
        handler = self.routes[key]
        return handler(kwargs)

    def close(self):
        pass


class SchemaConnection:
    def __init__(self, *, with_jira=False, with_zendesk=False, drop_document=False):
        names = set()
        if with_jira:
            names.add("JiraIssue")
        if with_zendesk:
            names.add("ZendeskTicket")
        proposal = confluence_schema_proposal(names)
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
        if with_zendesk:
            self.vertices["ZendeskTicket"] = {
                "PrimaryId": {"AttributeName": "id"},
                "Attributes": [
                    {
                        "AttributeName": "ticket_id",
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
                    {"From": pair_source, "To": target}
                    for pair_source, target in edge.pairs
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
                for name in ("ConfluencePage", "ConfluenceComment")
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


def test_site_is_stored_as_the_host():
    saved = source()
    assert saved.connection.site == "acme.atlassian.net"
    assert wiki_base_url(saved.connection.site) == "https://acme.atlassian.net/wiki"
    with pytest.raises(ValueError, match="site must be"):
        source(connection={"site": "https://acme.atlassian.net/wiki/spaces/DOCS", "email": "a@b.c", "api_token": "t"})


def test_schema_keeps_the_page_separate_and_adds_link_edges_when_installed():
    proposal = confluence_schema_proposal()
    assert {vertex.name for vertex in proposal.vertices} == {
        "ConfluencePage",
        "Person",
        "Organization",
        "ConfluenceComment",
        "Attachment",
    }
    names = {edge.name for edge in proposal.edges}
    assert LINKS_ISSUE not in names
    assert LINKS_TICKET not in names
    authored = next(edge for edge in proposal.edges if edge.name == "authored")
    assert ("ConfluencePage", "Person") in authored.pairs
    assert ("ConfluenceComment", "Person") in authored.pairs

    linked = confluence_schema_proposal({"JiraIssue", "ZendeskTicket"})
    issue = next(edge for edge in linked.edges if edge.name == LINKS_ISSUE)
    ticket = next(edge for edge in linked.edges if edge.name == LINKS_TICKET)
    assert issue.pairs == [("ConfluencePage", "JiraIssue")]
    assert ticket.pairs == [("ConfluencePage", "ZendeskTicket")]
    assert must_remain_separate("ConfluencePage", "JiraIssue")
    assert must_remain_separate("ConfluencePage", "ZendeskTicket")
    assert not can_reuse_vertex(
        "ConfluencePage",
        {"page_id": "STRING"},
        "JiraIssue",
        {"page_id": "STRING", "issue_key": "STRING"},
    )
    assert can_reuse_vertex(
        "Person",
        {"email": "STRING", "display_name": "STRING"},
        "person",
        {"email": "STRING", "display_name": "STRING"},
    )


def test_schema_status_reports_installed_conflict_and_uninitialized():
    assert confluence_schema_status(SchemaConnection())["status"] == "installed"
    assert confluence_schema_status(SchemaConnection(with_jira=True, with_zendesk=True))["status"] == "installed"
    conflict = SchemaConnection()
    conflict.vertices["ConfluencePage"]["Attributes"][0]["AttributeType"]["Name"] = "INT"
    result = confluence_schema_status(conflict)
    assert result["status"] == "conflict"
    assert "ConfluencePage.page_id must be STRING" in result["conflicts"][0]
    missing_edge = SchemaConnection()
    missing_edge.vertices["JiraIssue"] = {
        "PrimaryId": {"AttributeName": "id"},
        "Attributes": [],
    }
    partial = confluence_schema_status(missing_edge)
    assert partial["status"] == "incomplete"
    assert LINKS_ISSUE in partial["missing"]["edges"]
    assert confluence_schema_status(SchemaConnection(drop_document=True))["status"] == "not_initialized"


def test_client_uses_basic_auth_against_the_wiki_base(monkeypatch):
    created = {}

    class RecordingClient:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.base_url = kwargs["base_url"]

        def request(self, method, path, **kwargs):
            created.setdefault("calls", []).append((method, path, kwargs))
            if path == "/rest/api/user/current":
                return Response(
                    200,
                    {
                        "accountId": "712020:abc",
                        "displayName": "Ada",
                        "email": "ada@example.com",
                    },
                )
            if path == "/api/v2/spaces" and "cursor" not in (kwargs.get("params") or {}) and not any(
                call[1].startswith("/api/v2/spaces?") for call in created["calls"][:-1]
            ):
                return Response(
                    200,
                    {
                        "results": [{"id": "1", "key": "DOCS", "name": "Docs"}],
                        "_links": {"next": "/wiki/api/v2/spaces?cursor=abc"},
                    },
                )
            if path.startswith("/api/v2/spaces"):
                return Response(200, {"results": [{"id": "2", "key": "ENG", "name": "Eng"}]})
            raise AssertionError(path)

        def close(self):
            return None

    monkeypatch.setattr("connectors.confluence.client.httpx.Client", RecordingClient)
    saved = source()
    with ConfluenceClient(saved) as client:
        account = client.current_user()
        spaces = client.spaces()
    assert created["base_url"] == "https://acme.atlassian.net/wiki"
    assert created["auth"] == ("svc@example.com", "secret")
    assert account["displayName"] == "Ada"
    assert [row["key"] for row in spaces] == ["DOCS", "ENG"]
    cql = content_cql(saved, saved.scope.updated_after)
    assert "type in (page)" in cql
    assert 'space in ("DOCS")' in cql
    assert "ORDER BY lastModified ASC" in cql


def test_connection_reports_the_current_user(monkeypatch):
    class StubClient:
        def __init__(self, source, **kwargs):
            self.source = source

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def current_user(self):
            assert self.source.connection.api_token == "secret"
            return {
                "accountId": "712020:abc",
                "displayName": "Ada",
                "email": "ada@example.com",
            }

    monkeypatch.setattr("connectors.confluence.service.ConfluenceClient", StubClient)
    tested = check_connection(source())
    assert tested["account"]["display_name"] == "Ada"
    assert tested["account"]["email"] == "ada@example.com"


def test_rejected_credentials_name_the_token(monkeypatch):
    class StubClient:
        def __init__(self, source, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def current_user(self):
            raise ConfluenceAPIError(401, "unauthorized")

    monkeypatch.setattr("connectors.confluence.service.ConfluenceClient", StubClient)
    with pytest.raises(ConnectorCallError, match="API token") as caught:
        check_connection(source())
    assert caught.value.status_code == 401


def test_source_ids_cannot_collide_and_the_token_is_not_stored(tmp_path):
    jira_store = JiraSourceStore(str(tmp_path))
    zendesk_store = ZendeskSourceStore(str(tmp_path))
    confluence_store = ConfluenceSourceStore(str(tmp_path))
    jira_store.upsert("TestGraph", jira_source())
    zendesk_store.upsert(
        "TestGraph",
        ZendeskDataSource.model_validate(
            {
                "id": "zendesk-acme",
                "display_name": "Acme Zendesk",
                "connection": {
                    "subdomain": "acme",
                    "email": "agent@example.com",
                    "api_token": "secret",
                },
            }
        ),
    )
    with pytest.raises(ValueError, match="already used"):
        confluence_store.upsert("TestGraph", source(id="jira-acme"))
    confluence_store.upsert("TestGraph", source())
    document = json.loads((tmp_path / "TestGraph" / "data_sources.json").read_text())
    assert document["sources"][0]["id"] == "jira-acme"
    assert document["zendesk_sources"][0]["id"] == "zendesk-acme"
    assert document["confluence_sources"][0]["id"] == "conf-acme"
    assert "api_token" not in document["confluence_sources"][0]["connection"]
    assert confluence_store.redact(source())["connection"].get("api_token") is None
