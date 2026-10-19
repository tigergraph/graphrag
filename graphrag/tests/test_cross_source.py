from connectors.cross_source import (
    CROSS_SOURCE_EDGE,
    LinkGraph,
    TigerGraphLinkGraph,
    extract_targets,
    link_reference,
    link_text,
    resolve_target,
)
from connectors.prompt_selection import guidance_for, selected_connectors
from connectors.schema_reuse import can_reuse_vertex, must_remain_separate
from connectors.zendesk.schema import drop_reused_vertices, zendesk_schema_proposal


def test_explicit_urls_keep_jira_and_zendesk_identities_separate():
    assert extract_targets("See GML-2186 and ticket 12345") == []
    assert extract_targets(
        "https://acme.zendesk.com/agent/tickets/12345"
    ) == [("ZendeskTicket", "zendesk:acme:ticket:12345")]
    assert extract_targets(
        "https://acme.atlassian.net/browse/GML-2186"
    ) == [("JiraIssue", "jira:gml-2186:issue")]


def test_multiple_sources_wait_without_overwriting_each_other():
    graph = LinkGraph()
    graph.upsert_vertex("JiraIssue", "jira:gml-2186:issue")
    graph.upsert_vertex("JiraIssue", "jira:gml-2201:issue")
    target = "zendesk:acme:ticket:12345"
    link_reference(
        graph, "JiraIssue", "jira:gml-2186:issue", "ZendeskTicket", target
    )
    link_reference(
        graph, "JiraIssue", "jira:gml-2201:issue", "ZendeskTicket", target
    )
    link_reference(
        graph, "JiraIssue", "jira:gml-2186:issue", "ZendeskTicket", target
    )

    pending = [
        vertex_id
        for (vertex_type, vertex_id) in graph.vertices
        if vertex_type == "PendingLink"
    ]
    assert len(pending) == 2
    assert ("PendingRef", f"ref:{target}") in graph.vertices

    graph.upsert_vertex("ZendeskTicket", target, {"ticket_id": "12345"})
    resolve_target(graph, "ZendeskTicket", target)

    assert sum(edge[2] == CROSS_SOURCE_EDGE for edge in graph.edges) == 2
    assert not any(vertex_type == "PendingLink" for vertex_type, _ in graph.vertices)
    assert ("PendingRef", f"ref:{target}") not in graph.vertices
    assert graph.vertices[("ZendeskTicket", target)] == {"ticket_id": "12345"}
    assert ("JiraIssue", "jira:gml-2186:issue") in graph.vertices


def test_jira_text_that_mentions_another_jira_issue_does_not_require_cross_edge():
    graph = LinkGraph()
    graph.upsert_vertex("JiraIssue", "jira:pay-1:issue", {"issue_key": "PAY-1"})
    link_text(
        graph,
        "JiraIssue",
        "jira:gml-2186:issue",
        "See https://acme.atlassian.net/browse/PAY-1",
    )
    assert not any(edge[2] == CROSS_SOURCE_EDGE for edge in graph.edges)
    assert graph.vertices[("JiraIssue", "jira:pay-1:issue")] == {
        "issue_key": "PAY-1"
    }


def test_missing_cross_source_edge_type_does_not_fail_ingestion():
    graph = LinkGraph()
    graph.cross_edge_installed = False
    target = "zendesk:acme:ticket:12345"
    graph.upsert_vertex("ZendeskTicket", target, {"status": "open"})
    link_text(
        graph,
        "JiraIssue",
        "jira:gml-2186:issue",
        "Blocked by https://acme.zendesk.com/agent/tickets/12345",
    )
    assert not any(edge[2] == CROSS_SOURCE_EDGE for edge in graph.edges)
    assert graph.vertices[("ZendeskTicket", target)] == {"status": "open"}


def test_uninstalled_target_vertex_type_is_treated_as_missing():
    class JiraOnlyConnection:
        def __init__(self):
            self.edge_lookups = []

        def getVertexTypes(self):
            return ["JiraIssue", "PendingRef", "PendingLink"]

        def getEdgeTypes(self):
            return ["PENDING_REF"]

        def getEdges(self, vertex_type, vertex_id, edge_type):
            self.edge_lookups.append((vertex_type, vertex_id, edge_type))
            if vertex_type == "ZendeskTicket":
                raise RuntimeError(
                    "source_vertex_type = ZendeskTicket is not a valid vertex type"
                )
            return []

    conn = JiraOnlyConnection()
    graph = TigerGraphLinkGraph(conn)

    assert not graph.vertex_exists(
        "ZendeskTicket", "zendesk:acme:ticket:12345"
    )
    assert conn.edge_lookups == []


def test_existing_target_creates_one_edge_and_does_not_overwrite_it():
    graph = LinkGraph()
    target = "zendesk:acme:ticket:12345"
    graph.upsert_vertex("ZendeskTicket", target, {"status": "open"})
    link_text(
        graph,
        "JiraIssue",
        "jira:gml-2186:issue",
        "Blocked by https://acme.zendesk.com/agent/tickets/12345",
    )
    link_text(
        graph,
        "JiraIssue",
        "jira:gml-2186:issue",
        "Blocked by https://acme.zendesk.com/agent/tickets/12345",
    )

    assert graph.vertices[("ZendeskTicket", target)] == {"status": "open"}
    assert sum(edge[2] == CROSS_SOURCE_EDGE for edge in graph.edges) == 1
    assert not any(vertex_type == "PendingRef" for vertex_type, _ in graph.vertices)


def test_target_arriving_during_the_gap_does_not_leave_a_pending_link():
    graph = LinkGraph()
    target = "zendesk:acme:ticket:12345"
    real_exists = graph.vertex_exists

    calls = {"count": 0}

    def exists(vertex_type, vertex_id):
        calls["count"] += 1
        if vertex_type == "ZendeskTicket" and calls["count"] == 1:
            graph.upsert_vertex("ZendeskTicket", target, {"status": "open"})
            return False
        return real_exists(vertex_type, vertex_id)

    graph.vertex_exists = exists
    link_reference(
        graph, "JiraIssue", "jira:gml-2186:issue", "ZendeskTicket", target
    )

    assert sum(edge[2] == CROSS_SOURCE_EDGE for edge in graph.edges) == 1
    assert not any(vertex_type == "PendingLink" for vertex_type, _ in graph.vertices)


def test_resolving_one_target_preserves_unrelated_pending_links():
    graph = LinkGraph()
    first = "zendesk:acme:ticket:12345"
    second = "zendesk:acme:ticket:67890"
    link_reference(graph, "JiraIssue", "jira:gml-2186:issue", "ZendeskTicket", first)
    link_reference(graph, "JiraIssue", "jira:gml-2201:issue", "ZendeskTicket", second)
    graph.upsert_vertex("ZendeskTicket", first)
    resolve_target(graph, "ZendeskTicket", first)

    assert ("PendingRef", f"ref:{second}") in graph.vertices
    assert any(edge[4] == f"ref:{second}" for edge in graph.edges)


def test_jira_issue_and_zendesk_ticket_types_remain_separate():
    assert must_remain_separate("JiraIssue", "ZendeskTicket")
    assert not can_reuse_vertex(
        "ZendeskTicket",
        {"ticket_id": "STRING"},
        "JiraIssue",
        {"issue_key": "STRING"},
    )
    proposal = zendesk_schema_proposal({"JiraIssue", "PendingRef"})
    reused = drop_reused_vertices(
        proposal,
        {"PendingRef": {}, "JiraIssue": {"issue_key": "STRING"}},
    )
    assert "PendingRef" in reused
    assert "ZendeskTicket" in {vertex.name for vertex in proposal.vertices}
    assert "CROSS_SOURCE_LINK" in {edge.name for edge in proposal.edges}


def test_prompt_selection_is_scoped_and_asks_when_ambiguous():
    assert selected_connectors("What is the status of GML-2186?") == {"jira"}
    jira_query = guidance_for("What is the status of GML-2186?", "query")
    assert "Zendesk" not in jira_query
    assert "JIRA_COMMENT_ON" in jira_query
    assert "from_value" in jira_query
    zendesk = guidance_for(
        "What is https://acme.zendesk.com/agent/tickets/12345?",
        "planner",
    )
    assert "Zendesk" in zendesk
    assert "Jira guidance" not in zendesk
    assert "ZENDESK_REQUESTED_BY" in zendesk
    assert "fields that are not present" in zendesk

    ambiguous = guidance_for("What is the status of ticket 12345?", "answer")
    assert "Ask the user" in ambiguous
    assert "Jira guidance applies" not in ambiguous
    assert "Zendesk guidance applies" not in ambiguous

    cross = guidance_for(
        "How is GML-2186 related to https://acme.zendesk.com/agent/tickets/12345?",
        "answer",
    )
    assert "CROSS_SOURCE_LINK" in cross

    comparison = guidance_for(
        "Compare Jira GML-2186 versus Zendesk ticket 12345",
        "planner",
    )
    assert "CROSS_SOURCE_LINK" in comparison
