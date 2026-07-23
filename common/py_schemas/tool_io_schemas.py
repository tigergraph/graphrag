# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from typing import Dict, List, Optional

from pydantic import BaseModel, Field
from langchain_community.graphs.graph_document import Node as BaseNode
from langchain_community.graphs.graph_document import Relationship as BaseRelationship


class MapQuestionToSchemaResponse(BaseModel):
    question: str = Field(
        description="The question restated in terms of the graph schema"
    )
    target_vertex_types: List[str] = Field(
        description="The list of vertices mentioned in the question. If there are no vertices mentioned, then use an empty list."
    )
    target_vertex_attributes: Optional[Dict[str, List[str]]] = Field(
        description="The dictionary of vertex attributes mentioned in the question, formated in {'vertex_type_1': ['vertex_attribute_1', 'vertex_attribute_2'], 'vertex_type_2': ['vertex_attribute_1', 'vertex_attribute_2']}"
    )
    target_vertex_ids: Optional[Dict[str, List[str]]] = Field(
        description="The dictionary of vertex ids mentioned in the question. If there are no vertex ids mentioned, then use an empty dict. formated in {'vertex_type_1': ['vertex_id_1', 'vertex_id_2'], 'vertex_type_2': ['vertex_id_1', 'vertex_id_2']}"
    )
    target_edge_types: Optional[List[str]] = Field(
        description="The list of edges mentioned in the question"
    )
    target_edge_attributes: Optional[Dict[str, List[str]]] = Field(
        description="The dictionary of edge attributes mentioned in the question, formated in {'edge_type': ['edge_attribute_1', 'edge_attribute_2']}"
    )


class AgentOutput(BaseModel):
    answer: str = Field(description="Natural language answer generated")
    function_call: str = Field(description="Function call used to generate answer")


class MapAttributeToAttributeResponse(BaseModel):
    attr_map: Optional[Dict[str, str]] = Field(
        description="The dictionary of the form {'source_attribute': 'output_attribute'}"
    )


class GenerateFunctionResponse(BaseModel):
    connection_func_call: str = Field(
        description="The function call to make to answer the question. Must start with conn."
    )
    func_call_reasoning: str = Field(
        description="The reason why the function call was generated to answer the question."
    )


class Node(BaseNode):
    node_type: str = Field(
        description="Type of the node. Describe what the entity is. Ensure you use basic or elementary types for node labels.\n"
        "For example, when you identify an entity representing a person, "
        "always label it as 'Person'. Avoid using more specific terms "
        "like 'Mathematician' or 'Scientist'"
    )
    definition: str = Field(
        description="Definition of the node. Describe what the entity is."
    )


class Relationship(BaseRelationship):
    relation_type: str = Field(
        description="Type of the relationship. Describe what the relationship is. Instead of using specific and momentary types such as "
        "'BECAME_PROFESSOR', use more general and timeless relationship types like "
        "'PROFESSOR'. However, do not sacrifice any accuracy for generality"
    )
    source: Node = Field(description="The source node of the relationship.")
    target: Node = Field(description="The target node of the relationship.")
    definition: str = Field(
        description="Definition of the relationship. Describe what the relationship is."
    )


class ChunkSummary(BaseModel):
    """Compact metadata summary for a chunk, used to augment its dense
    embedding so retrieval matches natural-language queries more
    reliably on table-heavy and numeric content. Tag-line format keeps
    each field short and clusterable per keyword.
    """

    topic: str = Field(
        "",
        description=(
            "One short noun phrase (<= 12 chars) naming what this chunk is "
            "primarily about. In the source language."
        ),
    )
    section: str = Field(
        "",
        description=(
            "The heading or section title this chunk falls under, copied "
            "verbatim from the source when present; empty string otherwise."
        ),
    )
    entities: List[str] = Field(
        default_factory=list,
        description=(
            "Proper nouns / named entities / categories mentioned in the "
            "chunk (e.g. company names, prefecture names, years, "
            "regulatory bodies). When the chunk contains a table, include "
            "every column header / row label as an entity too — they carry "
            "the dimensional vocabulary a retrieval query is most likely to "
            "match on. Used for keyword-style retrieval signals."
        ),
    )


class KnowledgeGraph(BaseModel):
    """Generate a knowledge graph with entities and relationships."""

    nodes: List[Node] = Field(..., description="List of nodes in the knowledge graph")
    rels: List[Relationship] = Field(
        ..., description="List of relationships in the knowledge graph"
    )
    summary: Optional[ChunkSummary] = Field(
        default=None,
        description=(
            "Compact metadata summary for the chunk. Used by Contextual "
            "Retrieval — concatenated with the raw text before embedding so "
            "dense vectors carry the chunk's topic / entities / values "
            "explicitly. Optional: parsers tolerate missing summaries from "
            "legacy outputs."
        ),
    )


class ReportQuestion(BaseModel):
    question: str = Field("The question to be asked")
    reasoning: str = Field("The reasoning behind the question")


class ReportSection(BaseModel):
    section: str = Field("Name of the section")
    description: str = Field("Description of the section")
    questions: List[ReportQuestion] = Field(
        "List of questions and reasoning for the section"
    )


class ReportSections(BaseModel):
    sections: List[ReportSection] = Field("List of sections for the report")


class CommunitySummary(BaseModel):
    """Generate a summary of the documents that are within this community."""

    summary: str = Field(
        ..., description="The community summary derived from the input documents"
    )

class GraphRAGAnswerOutput(BaseModel):
    generated_answer: str = Field(description="The generated answer to the question. Make sure maintain a professional tone.")
    citation: Optional[list[str]] = Field(description="The citation for the answer. List the metadata, mostly the keys, of the parts of the context used.", default=[])


class CandidateScore(BaseModel):
    candidate: str = Field(description="The candidate answer according to the prompt.")
    quality_score: int = Field(description="The quality of the candidate answer, based on how well it meets the requirement in the prompt. Rate the candidate from 0 (poor) to 100 (excellent).")


class CandidateGenerator(BaseModel):
    candidates: List[CandidateScore] = Field(..., description="List of candidate questions with quality scores")

class CommunityAnswer(BaseModel):
    answer: str = Field(description="The answer to the question, based off of the context provided.")
    quality_score: int = Field(description="The quality of the answer, based on how well it answers the question. Rate the answer from 0 (poor) to 100 (excellent).")
