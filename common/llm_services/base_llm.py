# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
import logging
from langchain_core.output_parsers import BaseOutputParser, PydanticOutputParser
from langchain_core.exceptions import OutputParserException
from langchain_core.prompts import BasePromptTemplate
from langchain_community.callbacks.manager import get_openai_callback

logger = logging.getLogger(__name__)


class LLM_Model:
    """Base LLM_Model Class

    Used to connect to external LLM API services, and retrieve customized prompts for the tools.
    """

    def __init__(self, config):
        self.llm = None
        self.config = config
        from common.config import validate_graphname
        self._graphname = validate_graphname(config.get("graphname"))
        self.prompt_path = config.get("prompt_path", "")

    def _read_prompt_file(self, path):
        """Read a prompt file with per-graph override support.

        Resolution order:
          1. configs/graph_configs/<graphname>/prompts/<filename> (if graphname is set)
          2. Original path (from prompt_path config)

        Returns the file content, or None if the file doesn't exist anywhere.
        """
        filename = os.path.basename(path)
        if self._graphname:
            graph_override = os.path.join(
                "configs", "graph_configs", self._graphname, "prompts", filename
            )
            if os.path.exists(graph_override):
                with open(graph_override) as f:
                    return f.read()
        if os.path.exists(path):
            with open(path) as f:
                return f.read()
        return None

    def invoke_with_parser(
        self,
        prompt: BasePromptTemplate,
        parser: BaseOutputParser,
        input_variables: dict,
        caller_name: str = "unknown",
    ):
        """Invoke the LLM with a prompt and parse the output using the given parser.

        Builds a chain (prompt | llm), invokes it, and parses the output.
        Supports PydanticOutputParser (with JSON extraction fallback)
        and StrOutputParser (returns raw text).

        Args:
            prompt: The prompt template.
            parser: The output parser (PydanticOutputParser, StrOutputParser, etc.).
            input_variables: Dict of variables to pass to the prompt.
            caller_name: Name of the calling function (for logging).

        Returns:
            Parsed Pydantic model instance.

        Raises:
            OutputParserException: If all parsing attempts fail.
        """

        chain = prompt | self.llm

        usage_data = {}
        with get_openai_callback() as cb:
            raw_output = chain.invoke(input_variables)

            usage_data["input_tokens"] = cb.prompt_tokens
            usage_data["output_tokens"] = cb.completion_tokens
            usage_data["total_tokens"] = cb.total_tokens
            usage_data["cost"] = cb.total_cost
            logger.info(f"{caller_name} usage: {usage_data}")

        raw_text = raw_output.content if hasattr(raw_output, "content") else str(raw_output)

        try:
            return parser.parse(raw_text)
        except OutputParserException:
            logger.warning(f"{caller_name}: parser failed, attempting JSON extraction")
            json_match = re.search(r'\{[\s\S]*\}', raw_text)
            if json_match:
                return parser.parse(json_match.group())
            raise

    async def ainvoke_with_parser(
        self,
        prompt: BasePromptTemplate,
        parser: BaseOutputParser,
        input_variables: dict,
        caller_name: str = "unknown",
    ):
        """Async version of invoke_with_parser.

        Uses chain.ainvoke() to avoid blocking the event loop,
        suitable for async callers (e.g., ECC workers).
        """

        chain = prompt | self.llm

        usage_data = {}
        with get_openai_callback() as cb:
            raw_output = await chain.ainvoke(input_variables)

            usage_data["input_tokens"] = cb.prompt_tokens
            usage_data["output_tokens"] = cb.completion_tokens
            usage_data["total_tokens"] = cb.total_tokens
            usage_data["cost"] = cb.total_cost
            logger.info(f"{caller_name} usage: {usage_data}")

        raw_text = raw_output.content if hasattr(raw_output, "content") else str(raw_output)

        try:
            return parser.parse(raw_text)
        except OutputParserException:
            logger.warning(f"{caller_name}: parser failed, attempting JSON extraction")
            json_match = re.search(r'\{[\s\S]*\}', raw_text)
            if json_match:
                return parser.parse(json_match.group())
            raise

    @property
    def map_question_schema_prompt(self):
        """Property to get the prompt for the MapQuestionToSchema tool."""
        result = self._read_prompt_file(self.prompt_path + "map_question_to_schema.txt")
        if result is not None:
            return result
        return """# Map Question to Schema

Replace entities and relationships in the question with their canonical schema names provided in the Inputs section below.

## Rules
- If an entity (e.g. "John Doe") is referred to by different names or pronouns ("Joe", "he"), use the most complete identifier ("John Doe") consistently.
- Choose the better mapping between a vertex type and one of its attributes.
- Ensure entities are either source or target vertices of the chosen relationships.
- If an entity maps to a vertex attribute, consider generating a `WHERE` clause.
- For synonyms, output the canonical form from the schema choices.
- Generate the **complete** rewritten question. Keep the case of schema elements unchanged.
- Do NOT generate `target_vertex_ids` unless the term `id` is explicitly mentioned in the question.

## Inputs
- **Vertices**: {vertices}
- **Vertex attributes**: {verticesAttrs}
- **Edges**: {edges}
- **Edge source/target**: {edgesInfo}
- **Question**: {question}
- **Conversation**: {conversation}

{format_instructions}
"""

    @property
    def generate_function_prompt(self):
        """Property to get the prompt for the GenerateFunction tool."""
        result = self._read_prompt_file(self.prompt_path + "generate_function.txt")
        if result is not None:
            return result
        return """# pyTigerGraph Function Selection

Use the schema below to write the pyTigerGraph function call that answers the question via a `pyTigerGraph` connection.

## Selection Rules
- For "how many", counts, totals, or graph-DB statistics, always pick a function whose name contains `Count` (e.g. `getVertexCount`, `getEdgeCount`).
- Never pick a function not described in the docstrings below.
- If entities map to vertex attributes, consider a `WHERE` clause.
- When constructing `WHERE`, quote string attribute values properly. Example: `('Person', where='name="William Torres"')` — applies to every string attribute (name, email, address, etc.).
- Do NOT generate `target_vertex_ids` unless the term `id` is explicitly mentioned in the question.
- Pick exactly **one** function to execute.

## Schema
- **Vertex Types**: {vertex_types}
- **Vertex Attributes**: {vertex_attributes}
- **Vertex IDs**: {vertex_ids}
- **Edge Types**: {edge_types}
- **Edge Attributes**: {edge_attributes}

## Question
{question}

## Reference Docstrings
1. {doc1}
2. {doc2}
3. {doc3}
4. {doc4}
5. {doc5}
6. {doc6}
7. {doc7}
8. {doc8}

## Output
- If the function output answers the user's question, return that answer immediately.
- Output **valid JSON only** — no extra text would render the response invalid.

{format_instructions}
"""

    @property
    def entity_relationship_extraction_prompt(self):
        """Property to get the prompt for the EntityRelationshipExtraction tool."""
        result = self._read_prompt_file(
            self.prompt_path + "entity_relationship_extraction.txt"
        )
        if result is not None:
            return result
        return """# Knowledge Graph Extraction

You are a top-tier algorithm designed for extracting information in structured formats to build a knowledge graph.

## Goals
- **Nodes** represent entities, concepts, and properties of entities.
- Aim for simplicity and clarity so the graph is accessible to a vast audience.

## Node Labeling
- **Consistency**: use basic or elementary types. Label a person as `person`, not `mathematician` / `scientist`.
- **Node IDs**: never use integers. Use names or human-readable identifiers found in the text.

## Numerical Data and Dates
- Incorporate as **attributes / properties** of the respective nodes.
- Do NOT create separate nodes for dates or numerical values.
- Properties are key-value. Use properties only for dates and numbers; string properties become new nodes.
- Never use escaped single or double quotes within property values.
- Use `camelCase` for property keys (e.g. `birthDate`).

## Coreference Resolution
- Maintain entity consistency: if "John Doe" is referred to as "Joe" or "he", always use the most complete identifier (`John Doe`) throughout.

## Strict Compliance
- Follow these rules strictly. Non-compliance, including poor formatting, results in termination.

## No-Relationship Nodes
- Include nodes that have no relationships. Add the node and leave the relationships section empty."""

    @property
    def generate_cypher_prompt(self):
        """Property to get the prompt for the GenerateCypher tool."""
        result = self._read_prompt_file(self.prompt_path + "generate_cypher.txt")
        if result is not None:
            return result
        return """# OpenCypher Query Generation

You are an expert in OpenCypher. Generate the best query that retrieves the answer to: **{question}**.

## Schema and History
- **Schema**: {schema}
- **History**: {history}

## Construction Rules
- Distinguish entity **value** from entity **type** carefully.
- Remove duplicate words with the same meaning in the question.
- Only use attributes that exist in the schema. Pick the closest matching attribute name when multiple candidates exist.
- Prefer attributes over primary IDs when an attribute name is more similar to the keyword in the question.
- Keep the query minimal — fewest vertex types, edge types, and attributes possible.
- Do NOT return attributes that aren't explicitly mentioned in the question. If only a vertex is mentioned, return only the vertex.
- Always include the entity from the `WHERE` clause in the final `RETURN`. Use vertex name over ID when available.
- Always use **undirected** edge patterns. Ensure edges connect correct vertex types per schema.
- Use **double quotes** for strings.
- For string comparisons in `WHERE`, convert with `toLower()`.
- Use multi-word, underscore-joined aliases for `ORDER BY`. Aliases / attributes used in `ORDER BY` must be in `RETURN`. Always specify `ASC` / `DESC` based on data type.
- For "summarize" / "write a summary" questions, fetch all neighbour nodes and edges.
- Avoid invalid queries based on errors in the history above.

## Supported
- **Clauses**: `MATCH`, `OPTIONAL MATCH`, `MANDATORY MATCH`, `WHERE`, `RETURN`, `WITH`, `ORDER BY`, `SKIP`, `LIMIT`, `DELETE`, `DETACH DELETE`
- **Operators**:
  - Math: `+`, `-`, `*`, `/`, `%`, `^`
  - Comparison: `=`, `<`, `<=`, `>`, `>=`, `<>`, `IS NULL`, `IS NOT NULL`
  - Boolean: `AND`, `OR`, `NOT`, `XOR`
  - String / list: `CONTAINS`, `STARTS WITH`, `ENDS WITH`, `IN`, `DISTINCT`, `[ ]`, `.`
- **Functions**:
  - Aggregation: `count`, `sum`, `avg`, `min`, `max`, `stDev`, `stDevP`
  - Math: `abs`, `sqrt`, `log`, `exp`, `sin`, `cos`, `tan`, `radians`, `degrees`
  - String: `left`, `right`, `substring`, `replace`, `trim`, `toLower`, `toUpper`, `split`
  - List: `head`, `last`, `size`, `range`, `coalesce`, `tail`
  - Other: `id`, `elementId`, `labels`, `properties`, `timestamp`
- **Expressions**: `CASE`

## Unsupported
- **Clauses**: `CALL`, `CREATE`, `MERGE`, `REMOVE`, `SET`, `UNION`, `UNION ALL`, `UNWIND`
- **Functions**: `collect`, `exists`, `keys`, `nodes`, `relationships`, `length`, `percentileCont`, `percentileDisc`, `startNode`, `endNode`, `reverse` (list form)
- **Syntax limits**:
  - `WITH` must group by exactly one vertex variable.
  - Path variables (`p = (...)`) not supported.
  - `MATCH` must reference variables from prior `WITH`.
  - Disconnected `MATCH` fragments not supported.

## Output
- The query must return both the entity from the question AND the requested data.
- Validate syntax before responding.
- Aliases must NOT match vertex / edge types, operator / function names, or reserved keywords. Use multi-word underscore identifiers.
- Output ONLY the OpenCypher query — no explanation."""

    @property
    def generate_gsql_prompt(self):
        """Property to get the prompt for the GenerateGSQL tool."""
        result = self._read_prompt_file(self.prompt_path + "generate_gsql.txt")
        if result is not None:
            return result
        return """# GSQL Query Generation

You are an expert in TigerGraph GSQL. Generate the GSQL query that retrieves the answer to: **{question}**.

## Schema and History
- **Schema**: {schema}
- **History**: {history}

## Construction Rules
- Only use attributes in the schema. Never invent attributes.
- Prefer attributes over primary IDs when the attribute name is more similar to a keyword in the question.
- Keep the query minimal — fewest vertex types, edge types, and attributes possible.
- Do NOT return attributes the question doesn't mention. If only a vertex is mentioned, return only the vertex.
- Always use **double quotes** for strings.
- Use aliases for `ORDER BY`. Aliases / attributes used in `ORDER BY` must also be in `PRINT`. Always specify `ASC` / `DESC` based on data type.
- Avoid invalid queries based on errors in the history above.

## Unsupported
- **Clauses**: `CREATE`, `DELETE`, `INSERT`, `UPDATE`, `UPSERT`

## Output
- The query must return both the entity from the question AND the requested data.
- Aliases must NOT match vertex / edge types, operator / function names, or reserved keywords. Use multi-word underscore identifiers.
- Output ONLY the GSQL query — no explanation."""

    @property
    def route_response_prompt(self):
        """Property to get the prompt for the RouteResponse tool."""
        result = self._read_prompt_file(self.prompt_path + "route_response.txt")
        if result is not None:
            return result
        return """# Route the Question

Route the user question to one of: `functions`, `vectorstore`, or `history`.

## Routing
- **`history`**: questions similar to previous ones, or that reference earlier answers / responses, or that refer to the same entities mentioned in a previous answer.
- **`vectorstore`**: questions best answered by text documents.
- **`functions`**: questions about structured data or operations on structured data. Available entities: {v_types}; relationships: {e_types}. Some "how many documents are there?" style questions can be answered here.

## Mandatory `functions` Routing
Any question about graph database **statistics or metadata** MUST route to `functions`:
- Counts of vertices / nodes / edges (e.g. "how many edges in the graph").
- Listing or describing vertex / edge types, schema, or graph structure.
- Aggregations, totals, or summaries of data in the graph database.
- Any question mentioning "graph", "graph db", "graph database", "vertices", "nodes", or "edges" in the context of statistics / counts.

These are **database queries, not document lookups** — always route them to `functions`.

Otherwise, route to `vectorstore`.

## Output
Return JSON with a single key `datasource` (value: `functions`, `vectorstore`, or `history`). No preamble or explanation.

## Inputs
- **Question**: {question}
- **Conversation history**: {conversation}

{format_instructions}"""

    @property
    def hyde_prompt(self):
        """Property to get the prompt for the HyDE tool."""
        result = self._read_prompt_file(self.prompt_path + "hyde.txt")
        if result is not None:
            return result
        return """# Hypothetical Document

Write an example of a document that might answer this question.

**Question**: {question}

**Answer**:"""

    @property
    def chatbot_response_prompt(self):
        """Property to get the prompt for the SupportAI response."""
        result = self._read_prompt_file(self.prompt_path + "chatbot_response.txt")
        if result is not None:
            return result
        return """# AI-Powered Knowledge Graph Assistant

You are a highly efficient, empathetic, and professional AI assistant. Use the provided contexts to answer the user's question.

## Rules
- The contexts arrive as JSON key-context pairs. **Combine and rephrase** them to answer the question.
- **Score** each context for relevance and use only the high-scoring ones — do not invent additional logic.
- **Cover** the relevant information, especially image references that carry critical visual information.
- **Preserve** image links exactly as `![description](url)` in the final answer when used. Do NOT modify or omit them.
- **Format** the answer in Markdown — titles, paragraphs, bulleted / numbered lists, images, and tables. Place images and tables below the related text section.
- **Tables**: every row, including the header, starts on a new line.
- **Output as JSON** — escape characters as needed so the response is valid JSON. Include every field required by the format instructions; set unknown fields to empty.
- Treat context keys as citations only when asked; otherwise do NOT include citations in the final answer.

## Inputs
- **Question**: {question}
- **Contexts**: {context}
- **Query**: {query}

{format_instructions}
"""

    @property
    def keyword_extraction_prompt(self):
        """Property to get the prompt for the Question Expansion response."""
        result = self._read_prompt_file(self.prompt_path + "keyword_extraction.txt")
        if result is not None:
            return result
        return """# Keyword Extraction

Extract key terms (glossary) from the question(s) below to represent their original meaning as faithfully as possible.

## Rules
- Each term should contain only a couple of words.
- Score each extracted term **0 (poor)** to **100 (excellent)** based on how important and frequent it is in the question(s). Higher scores indicate terms that are both significant and frequent.
- Output ONLY the extracted terms with their quality scores in the required format.

## Question
{question}

{format_instructions}
"""

    @property
    def question_expansion_prompt(self):
        """Property to get the prompt for the Question Expansion response."""
        result = self._read_prompt_file(self.prompt_path + "question_expansion.txt")
        if result is not None:
            return result
        return """# Question Expansion

Generate **10 new questions** similar to the original question below to express its meaning more clearly.

## Scoring
Include a quality score per generated question, **0 (poor)** to **100 (excellent)**, based on how well it represents the meaning of the original question.

## Question
{question}

{format_instructions}
"""

    @property
    def graphrag_scoring_prompt(self):
        """Property to get the prompt for the GraphRAG Scoring response."""
        result = self._read_prompt_file(self.prompt_path + "graphrag_scoring.txt")
        if result is not None:
            return result
        return """# Quality-Scored Answer

Generate an answer to the question below using the provided data, and include a quality score.

## Scoring
The quality score is between **0 (poor)** and **100 (excellent)**, based on how well the answer addresses the question.

## Inputs
- **Question**: {question}
- **Context**: {context}

{format_instructions}
"""

    @property
    def community_summarize_prompt(self):
        """Property to get the prompt for community summarization."""
        result = self._read_prompt_file(self.prompt_path + "community_summarization.txt")
        if result is not None:
            return result
        return """# Community Summary

Generate a comprehensive summary of the data below.

## Rules
- Concatenate the descriptions into a single, comprehensive summary that includes information from **all** descriptions.
- Resolve contradictions; do NOT add information that is not in the descriptions.
- Write in **third person** and include the entity name(s) for full context.

## Data
- **Community Title**: {entity_name}
- **Description List**: {description_list}
"""

    @property
    def schema_extraction_prompt(self):
        """Property to get the prompt for sample-doc schema extraction."""
        result = self._read_prompt_file(self.prompt_path + "schema_extraction.txt")
        if result is not None:
            return result
        return """# Schema Extraction

You are a knowledge-graph schema architect. From the sample documents provided in the Inputs section below, produce a domain schema as TigerGraph GSQL `VERTEX` / `DIRECTED EDGE` / `UNDIRECTED EDGE` declarations (no leading `ADD`). Return GSQL only — no fences, no commentary, no JSON.

## Rules

1. **Vertex inclusion**: a vertex type's instances must be individuated in the source (each instance has its own identity), appear **2+ times**, and have at least one natural attribute beyond `name`. Concrete or conceptual is fine. Skip categorical wrappers — names ending in `_record`, `_management`, `_context`, `_grouping`, or labels of classes-of-classes.
2. **Skip layout**: do NOT produce types for axes, page numbers, captions, table cells, or other document-rendering artifacts.
3. **Edge naming**: use a specific action verb. Include an edge type ONLY IF the source documents contain **2+ concrete instances** of that relationship between named entities — do NOT propose merely-plausible edges. Avoid generic edges (`RELATED_TO`, `CONNECTED_TO`, `ASSOCIATED_WITH`, `HAS`, `BELONGS_TO`). Use `DIRECTED EDGE` for asymmetric verbs and `UNDIRECTED EDGE` only for genuinely symmetric peer relationships.
4. **Reserved names**: do NOT use a name (case-insensitive) matching any of the reserved structural types or GSQL keywords listed in the Inputs section. Pick a synonym or qualifier (e.g. `KeywordRecord`).
5. **Attributes**: each `VERTEX` has **1–5** attributes; each `EDGE` has **0–3**. Primitive types only: `STRING`, `INT`, `UINT`, `DOUBLE`, `FLOAT`, `BOOL`, `DATETIME`. Do NOT include any id / primary-key field.
6. **Comments**: every `VERTEX` and `EDGE` MUST be preceded by exactly one `// <one-sentence definition>` line.
7. **Size**: produce **8–25** vertex types and **8–25** edge types.

## Example Output (illustrative — pick names that fit YOUR documents)

    // A natural person referenced in the documents.
    VERTEX Person(name STRING, role STRING);

    // An organization or institutional body.
    VERTEX Organization(name STRING, founded_at DATETIME);

    // A person works for an organization in a given role.
    DIRECTED EDGE WORKS_FOR(FROM Person, TO Organization, role STRING);

    // Two people are colleagues — symmetric peer relationship.
    UNDIRECTED EDGE COLLEAGUE_OF(FROM Person, TO Person);

## Inputs
- **Reserved structural types** (case-insensitive): {structural_types}
- **Reserved GSQL keywords** (case-insensitive): {tg_keywords}
- **Sample documents**:

{samples}
"""

    @property
    def contextualize_question_prompt(self):
        """Property to get the prompt for contextualizing a follow-up question
        into a standalone search query using conversation history."""
        result = self._read_prompt_file(
            self.prompt_path + "contextualize_question.txt"
        )
        if result is not None:
            return result
        return """# Standalone Question Rewrite

Given the conversation history and a follow-up question, rewrite the follow-up into a **standalone, self-contained** question suitable for searching a knowledge graph.

Do **NOT** answer the question — only rewrite it.

## Conversation History
{history}

## Follow-up Question
{question}

## Standalone Question
"""

