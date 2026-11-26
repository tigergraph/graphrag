"""Pre-run checks for constructs TigerGraph's interpreted openCypher mishandles."""

from tools.generate_cypher import unsafe_constructs


def _flags(query):
    return " ".join(unsafe_constructs(query))


def test_a_string_range_next_to_an_edge_is_rejected_and_the_filter_first_form_is_not():
    bad = ('MATCH (t:Ticket)<-[:assigned]-(p:Person) WHERE t.created >= "2025-03-01" '
           'RETURN p.name, count(t)')
    assert "returns no rows" in _flags(bad)
    good = ('MATCH (t:Ticket) WHERE t.created >= "2025-03-01" WITH t '
            'MATCH (p:Person)-[:assigned]->(t) RETURN p.name, count(t)')
    assert unsafe_constructs(good) == []
    datetime_range = ('MATCH (i:Issue)-[:assigned_to]->(p:Person) '
                      'WHERE i.created >= to_datetime("2025-03-01 00:00:00") RETURN p.name')
    assert unsafe_constructs(datetime_range) == []


def test_case_needs_else_and_coalesce_is_rejected():
    assert "CASE without ELSE" in _flags(
        'MATCH (t:Ticket) RETURN count(CASE WHEN t.status = "closed" THEN 1 END)')
    assert unsafe_constructs(
        'MATCH (t:Ticket) RETURN CASE WHEN t.status = "closed" THEN "a" ELSE "b" END AS k'
    ) == []
    assert "coalesce" in _flags('MATCH (p:Person) RETURN coalesce(p.name, "none")')


def test_text_fields_are_matched_by_words_not_equality():
    assert "CONTAINS on each" in _flags(
        'MATCH (p:Page) WHERE toLower(p.title) = "release checklist" RETURN p.url')
    assert "CONTAINS on each" in _flags('MATCH (i:Issue) WHERE i.summary = "x" RETURN i')
    assert unsafe_constructs(
        'MATCH (p:Page) WHERE toLower(p.title) CONTAINS "release" RETURN p.url') == []
    assert unsafe_constructs('MATCH (p:Page) WHERE p.title <> "" RETURN p.url') == []


def test_a_plan_step_id_is_not_a_value():
    assert "step id" in _flags('MATCH (c:Comment) WHERE c.page_id = "S1" RETURN c.body')
    assert "step id" in _flags('MATCH (c:Comment) WHERE c.page_id = $S1_page_id RETURN c')
    assert unsafe_constructs('MATCH (s:Space) WHERE s.key = "SRE" RETURN s') == []


def test_the_reserved_tags_attribute_is_reported():
    assert "reserved word" in _flags('MATCH (t:Ticket) RETURN t.tags')
