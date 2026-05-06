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


# Per-request collector for LLM usage so callers (e.g. agent trace logs) can
# aggregate token usage without breaking the existing return signatures.
# It's a context-local list the agent resets before each node executes.
import contextvars as _contextvars

_usage_collector: _contextvars.ContextVar = _contextvars.ContextVar(
    "llm_usage_collector", default=None
)


def start_usage_collection():
    """Begin collecting LLM usage for the current context (per node)."""
    _usage_collector.set([])


def get_collected_usage():
    """Return the usage entries collected since the last start (or None)."""
    return _usage_collector.get()


def _record_usage(caller_name: str, usage_data: dict):
    bucket = _usage_collector.get()
    if bucket is not None:
        bucket.append({"caller_name": caller_name, **usage_data})


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
            _record_usage(caller_name, usage_data)

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
            _record_usage(caller_name, usage_data)

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
        return self._read_prompt_file(self.prompt_path + "map_question_to_schema.txt")

    @property
    def generate_function_prompt(self):
        """Property to get the prompt for the GenerateFunction tool."""
        return self._read_prompt_file(self.prompt_path + "generate_function.txt")

    @property
    def entity_relationship_extraction_prompt(self):
        """Property to get the prompt for the EntityRelationshipExtraction tool."""
        return self._read_prompt_file(
            self.prompt_path + "entity_relationship_extraction.txt"
        )

    @property
    def generate_cypher_prompt(self):
        """Property to get the prompt for the GenerateCypher tool."""
        result = self._read_prompt_file(self.prompt_path + "generate_cypher.txt")
        if result is not None:
            return result
        return """You're an expert in OpenCypher programming. Given the following schema and history, what is the OpenCypher query that retrieves the {question}
                    Only include attributes that are found in the schema. Never include any attributes that are not found in the schema.
                    Use attributes instead of primary id if attribute name is closer to the keyword type in the question.
                    Use as less vertex type, edge type and attributes as possible. If an attribute is not found in the schema, please exclude it from the query.
                    Do not return attributes that are not explicitly mentioned in the question. If a vertex type is mentioned in the question, only return the vertex.
                    Never use directed edge pattern in the OpenCypher query. Always use and create query using undirected pattern.
                    Always use double quotes for strings instead of single quotes.

                    Avoid generating invalid OpenCypher queries based on the errors from history below.

                    Schema: {schema}
                    History: {history}

                    You cannot use the following clauses:
                    OPTIONAL MATCH
                    CREATE
                    MERGE
                    REMOVE
                    UNION
                    UNION ALL
                    UNWIND
                    SET

                    Make sure to have correct attribute names in the OpenCypher query and not to name result aliases that are vertex or edge types.

                    ONLY write the OpenCypher query in the response. Do not include any other information in the response."""

    @property
    def generate_gsql_prompt(self):
        """Property to get the prompt for the GenerateGSQL tool."""
        result = self._read_prompt_file(self.prompt_path + "generate_gsql.txt")
        if result is not None:
            return result
        return """You're an expert in GSQL (Graph SQL) programming for TigerGraph. Given the following schema: {schema}, what is the GSQL query that retrieves the answer for question: {question}
                    Only include attributes that are found in the schema. Never include any attributes that are not found in the schema.
                    Use attributes instead of primary id if attribute name is more similar to the keyword type in the question.
                    Use as few vertex types, edge types and attributes as possible. If an attribute is not found in the schema, please exclude it from the query.
                    Do not return attributes that are not explicitly mentioned in the question. If a vertex type is mentioned in the question, only return the vertex.
                    Always use double quotes for strings instead of single quotes.
                    Use alias for ORDER BY if any, and make sure the alias or attributes used in ORDER BY is also in PRINT. Always add ASC or DESC for ORDER BY based on data type.

                    Avoid generating invalid GSQL queries based on the errors from history below.

                    Schema: {schema}
                    History: {history}

                    Additionally, you cannot use the following clauses:
                    CREATE
                    DELETE
                    INSERT
                    UPDATE
                    UPSERT

                    Here's some commonly used abbreviations:
                    dt -> date
                    pct -> percentage
                    qty -> quantity
                    lng -> longitude
                    cm -> Contract Manufacturer

                    Always make the GSQL query returns the entity in the original question together with the data to be queried.
                    Make sure to have correct attribute names in the GSQL query and not to name result aliases that are vertex or edge types, operator or function names, and other reserved keywords, always construct alias with multiple words connected with underscore.

                    ONLY write the GSQL query in the response. Do not include any other information in the response."""

    @property
    def route_response_prompt(self):
        """Property to get the prompt for the RouteResponse tool."""
        result = self._read_prompt_file(self.prompt_path + "route_response.txt")
        if result is not None:
            return result
        return """\
You are an expert at routing a user question to a vectorstore, function calls, or conversation history.
Use the conversation history for questions that are similar to previous ones or that reference earlier answers or responses.
Use the vectorstore for questions that would be best suited by text documents.
Use the function calls for questions that ask about structured data, or operations on structured data.
Questions referring to same entities in a previous, earlier, or above answer or response should be routed to the conversation history.
Keep in mind that some questions about documents such as "how many documents are there?" can be answered by function calls.
The function calls can be used to answer questions about these entities: {v_types} and relationships: {e_types}.
IMPORTANT: Questions about graph database statistics or metadata MUST be routed to function calls. This includes:
- Counting vertices/nodes/edges (e.g. "how many vertices are there", "how many edges in the graph")
- Listing or describing vertex/edge types, schema, or graph structure
- Aggregations, totals, or summaries of data stored in the graph database
- Any question mentioning "graph", "graph db", "graph database", "vertices", "nodes", or "edges" in the context of statistics or counts
These are database queries, NOT document lookups — always route them to function calls.
Otherwise, use vectorstore. Choose one of 'functions', 'vectorstore', or 'history' based on the question and conversation history.
Return a JSON with a single key 'datasource' and no preamble or explanation.
Question to route: {question}
Conversation history: {conversation}
Format: {format_instructions}\
"""

    @property
    def hyde_prompt(self):
        """Property to get the prompt for the HyDE tool."""
        result = self._read_prompt_file(self.prompt_path + "hyde.txt")
        if result is not None:
            return result
        return """You are a helpful agent that is writing an example of a document that might answer this question: {question}
                  Answer:"""

    @property
    def chatbot_response_prompt(self):
        """Property to get the prompt for the SupportAI response."""
        result = self._read_prompt_file(self.prompt_path + "chatbot_response.txt")
        if result is not None:
            return result
        return """Given the answer context in JSON format, rephrase it to answer the question. \n
                   Use only the provided information in context without adding any reasoning or additional logic. \n
                   Make sure all information in the answer are covered in the generated answer.\n

                   Question: {question} \n
                   Answer: {context} \n
                   Format: {format_instructions}"""

    @property
    def keyword_extraction_prompt(self):
        """Property to get the prompt for the Question Expansion response."""
        result = self._read_prompt_file(self.prompt_path + "keyword_extraction.txt")
        if result is not None:
            return result
        return """You are a helpful assistant responsible for extracting key terms (glossary) from all the questions below to represent their original meaning as much as possible. Each term should only contain a couple of words. Include a quality score for the each extracted glossary, based on how important and frequent it's in the given questions. The quality score should range from 0 (poor) to 100 (excellent), with higher scores indicating terms that are both significant and frequent in the context of the questions.\nThe output should only contain the extracted terms and their quality scores using the required format.\n\nQuestion: {question}\n\n{format_instructions}\n"""

    @property
    def question_expansion_prompt(self):
        """Property to get the prompt for the Question Expansion response."""
        result = self._read_prompt_file(self.prompt_path + "question_expansion.txt")
        if result is not None:
            return result
        return """You are a helpful assistant responsible for generating 10 new questions similar to the original question below to represent its meaning in a more clear way.\nInclude a quality score for the answer, based on how well it represents the meaning of the original question. The quality score should be between 0 (poor) and 100 (excellent).\n\nQuestion: {question}\n\n{format_instructions}\n"""

    @property
    def graphrag_scoring_prompt(self):
        """Property to get the prompt for the GraphRAG Scoring response."""
        result = self._read_prompt_file(self.prompt_path + "graphrag_scoring.txt")
        if result is not None:
            return result
        return """You are a helpful assistant responsible for generating an answer to the question below using the data provided.\nInclude a quality score for the answer, based on how well it answers the question. The quality score should be between 0 (poor) and 100 (excellent).\n\nQuestion: {question}\nContext: {context}\n\n{format_instructions}\n"""

    @property
    def community_summarize_prompt(self):
        """Property to get the prompt for community summarization."""
        result = self._read_prompt_file(self.prompt_path + "community_summarization.txt")
        if result is not None:
            return result
        raise FileNotFoundError(
            f"Community summarization prompt file not found in {self.prompt_path}. "
            "Please ensure community_summarization.txt exists in the configured prompt path."
        )

    @property
    def contextualize_question_prompt(self):
        """Property to get the prompt for contextualizing a follow-up question
        into a standalone search query using conversation history."""
        result = self._read_prompt_file(
            self.prompt_path + "contextualize_question.txt"
        )
        if result is not None:
            return result
        return (
            "Given the following conversation history and a follow-up "
            "question, rewrite the follow-up question into a standalone, "
            "self-contained question suitable for searching a knowledge "
            "graph.  Do NOT answer the question; only rewrite it.\n\n"
            "Conversation history:\n{history}\n\n"
            "Follow-up question: {question}\n\n"
            "Standalone question:"
        )

