import pytest

from app.tools.generate_cypher import validate_tigergraph_cypher


SCHEMA = """Edge Types:
REPORTS_TO
	From Vertex: Person
	To Vertex: Person
	Edge direction: Directed
	Attributes:
		since of type DATETIME
WORKS_FOR
	From Vertex: Person
	To Vertex: Company
	Edge direction: Directed
	Attributes:
		No attributes
"""


def test_rejects_optional_undirected_directed_self_edge_object():
    query = """
MATCH (person:Person)
OPTIONAL MATCH (person)-[manager_edge:REPORTS_TO]-(manager:Person)
RETURN person, manager_edge, manager
"""

    with pytest.raises(ValueError, match="cannot return manager_edge"):
        validate_tigergraph_cypher(query, SCHEMA)


@pytest.mark.parametrize(
    "query",
    [
        """
        OPTIONAL MATCH (person:Person)-[manager_edge:REPORTS_TO]->(manager:Person)
        RETURN person, manager_edge, manager
        """,
        """
        MATCH (person:Person)-[manager_edge:REPORTS_TO]-(manager:Person)
        RETURN person, manager_edge, manager
        """,
        """
        OPTIONAL MATCH (person:Person)-[employment:WORKS_FOR]-(company:Company)
        RETURN person, employment, company
        """,
        """
        OPTIONAL MATCH (person:Person)-[manager_edge:REPORTS_TO]-(manager:Person)
        RETURN person, manager
        """,
        """
        OPTIONAL MATCH (person:Person)-[manager_edge:REPORTS_TO]-(manager:Person)
        RETURN person, manager_edge.since, manager
        """,
    ],
)
def test_accepts_other_query_shapes(query):
    validate_tigergraph_cypher(query, SCHEMA)
