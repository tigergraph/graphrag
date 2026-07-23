# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# This program may be redistributed and/or modified under the terms of the GNU
# Affero General Public License as published by the Free Software Foundation,
# either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

import json
import logging
from typing import Dict, List, Optional, Type

from langchain_core.language_models.llms import LLM
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.tools import BaseTool
from langchain_core.tools import ToolException
from langchain_community.callbacks.manager import get_openai_callback

from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter
from common.metrics.tg_proxy import TigerGraphConnectionProxy
from common.py_schemas import GenerateFunctionResponse, MapQuestionToSchemaResponse

from .validation_utils import (
    InvalidFunctionCallException,
    MapQuestionToSchemaException,
    NoDocumentsFoundException,
    validate_function_call,
    validate_schema,
)

logger = logging.getLogger(__name__)


class FindExistingQuery(BaseTool):
    """FindExistingQuery Tool.
    Tool to find an existing query in the TigerGraph database that matches the question.
    """

    name: str = "FindExistingQuery"
    description: str = "Finds an existing query in the TigerGraph database that matches the question."
    conn: TigerGraphConnectionProxy = None
    llm: LLM = None
    handle_tool_error: bool = True
    args_schema: Type[MapQuestionToSchemaResponse] = MapQuestionToSchemaResponse

    def __init__(self, conn, llm):
        """Initialize FindExistingQuery.
        Args:
            conn (TigerGraphConnection):
                pyTigerGraph TigerGraphConnection connection to the appropriate database/graph with correct permissions
            llm (LLM_Model):
                LLM_Model class to interact with an external LLM API.
        """
        super().__init__()
        logger.debug(f"request_id={req_id_cv.get()} FindExistingQuery instantiated")
        self.conn = conn
        self.llm = llm

    def _get_installed_queries(self) -> List[str]:
        """Get list of installed queries from TigerGraph.
        
        Returns:
            List of installed query names
        """
        try:
            endpoints = self.conn.getEndpoints(dynamic=True)
            graphname = self.conn.graphname
            installed_queries = [
                q.split("/")[-1] for q in endpoints 
                if f"/{graphname}/" in q and q.endswith(("GET", "POST"))
            ]
            return installed_queries
        except Exception as e:
            logger.warning(f"Error getting installed queries: {e}")
            return []

    def _get_query_metadata(self, query_name: str) -> Dict:
        """Get metadata for a single query.
        
        Args:
            query_name: Name of the query
            
        Returns:
            Dictionary containing query metadata
        """
        try:
            metadata = self.conn.getQueryMetadata(query_name)
            return {
                "name": query_name,
                "description": metadata.get("description", ""),
                "parameters": metadata.get("input", {}),
                "output": metadata.get("output", {}),
                "source": self.conn.showQuery(query_name)
            }
        except Exception as e:
            logger.warning(f"Error getting metadata for query {query_name}: {e}")
            return {
                "name": query_name,
                "description": "",
                "parameters": {},
                "output": {},
                "source": ""
            }

    def _run(
        self,
        question: str,
        target_vertex_types: List[str] = [],
        target_vertex_attributes: Dict[str, List[str]] = {},
        target_vertex_ids: Dict[str, List[str]] = {},
        target_edge_types: List[str] = [],
        target_edge_attributes: Dict[str, List[str]] = {},
    ) -> str:
        """Run the tool.
        Args:
            question (str):
                The question to answer with the database.
            target_vertex_types (List[str]):
                The list of vertex types the question mentions.
            target_vertex_attributes (Dict[str, List[str]]):
                The dictionary of vertex attributes the question mentions, in the form {"vertex_type": ["attr1", "attr2"]}
            target_vertex_ids (Dict[str, List[str]):
                The dictionary of vertex ids the question mentions, in the form of {"vertex_type": ["v_id1", "v_id2"]}
            target_edge_types (List[str]):
                The list of edge types the question mentions.
            target_edge_attributes (Dict[str, List[str]]):
                The dictionary of edge attributes the question mentions, in the form {"edge_type": ["attr1", "attr2"]}
        """
        LogWriter.info(f"request_id={req_id_cv.get()} ENTRY FindExistingQuery._run()")

        if target_vertex_types == [] and target_edge_types == []:
            return {
                "error": "No vertex or edge types recognized. MapQuestionToSchema and then try again."
            }

        try:
            validate_schema(
                self.conn,
                target_vertex_types,
                target_edge_types,
                target_vertex_attributes,
                target_edge_attributes,
            )
        except MapQuestionToSchemaException as e:
            LogWriter.warning(
                f"request_id={req_id_cv.get()} WARN input schema not valid"
            )
            return e

        # Get installed queries
        installed_queries = self._get_installed_queries()
        if not installed_queries:
            return {
                "error": "No installed queries found in the database"
            }

        # Get metadata for all queries
        query_metadata_list = []
        for query_name in installed_queries:
            metadata = self._get_query_metadata(query_name)
            query_metadata_list.append(metadata)

        # Create query descriptions for LLM analysis
        query_descriptions = []
        for metadata in query_metadata_list:
            desc = f"""
**Query Name**: {metadata['name']}
  - Description: {metadata['description']}
  - Parameters: {metadata['parameters']}
  - Output Format: {metadata['output']}
  - Source Code: {metadata['source'][:1000]}...
"""
            query_descriptions.append(desc)

        # Create prompt for LLM to generate function call
        func_parser = PydanticOutputParser(pydantic_object=GenerateFunctionResponse)
        
        PROMPT = PromptTemplate(
            template="""
You are an expert at analyzing TigerGraph queries and generating function calls to execute them.

**Question**: {question}
**Target Vertex Types**: {vertex_types}
**Target Edge Types**: {edge_types}
**Target Vertex Attributes**: {vertex_attributes}
**Target Edge Attributes**: {edge_attributes}
**Target Vertex IDs**: {vertex_ids}

Available Queries:
{query_descriptions}

Analyze the queries and determine which one best matches the question. Then generate a function call to execute that query.
Consider:
1. Query description relevance to the question
2. Parameter compatibility with the question requirements
3. Output format suitability for answering the question
4. Source code analysis for vertex/edge type usage

Generate a function call that starts with "conn." and uses runInstalledQuery for the best matching query.
For parameters, use appropriate values based on the question context.

Provide your analysis in the following format:
{format_instructions}
""",
            input_variables=[
                "question",
                "vertex_types", 
                "edge_types",
                "vertex_attributes",
                "edge_attributes",
                "vertex_ids",
                "query_descriptions"
            ],
            partial_variables={
                "format_instructions": func_parser.get_format_instructions()
            },
        )

        inputs = {
            "question": question,
            "vertex_types": target_vertex_types,
            "edge_types": target_edge_types,
            "vertex_attributes": target_vertex_attributes,
            "edge_attributes": target_edge_attributes,
            "vertex_ids": target_vertex_ids,
            "query_descriptions": "\n".join(query_descriptions)
        }

        chain = PROMPT | self.llm.model | func_parser
        usage_data = {}
        
        with get_openai_callback() as cb:
            try:
                generated = chain.invoke(**inputs)
                usage_data["input_tokens"] = cb.prompt_tokens
                usage_data["output_tokens"] = cb.completion_tokens
                usage_data["total_tokens"] = cb.total_tokens
                usage_data["cost"] = cb.total_cost
                logger.info(f"find_existing_query usage: {usage_data}")

            except Exception as e:
                logger.warning(f"LLM analysis failed: {e}")
                raise ToolException(f"Query finding failed: {str(e)}")

        # Validate the generated function call
        try:
            parsed_func = validate_function_call(
                self.conn, generated.connection_func_call, installed_queries
            )
        except InvalidFunctionCallException as e:
            LogWriter.warning(
                f"request_id={req_id_cv.get()} EXIT FindExistingQuery._run() with exception={e}"
            )
            return e

        # Execute the function call
        try:
            loc = {}
            exec("res = conn." + parsed_func, {"conn": self.conn}, loc)
            LogWriter.info(f"request_id={req_id_cv.get()} EXIT FindExistingQuery._run()")
            if "runInstalledQuery" in parsed_func:
                query_name = parsed_func.split("(")[1].split(",")[0].strip("'")
                return {
                    "function_call": parsed_func,
                    "result": json.dumps(loc["res"]),
                    "reasoning": generated.func_call_reasoning,
                    "query_output_format": self.conn.getQueryMetadata(query_name)["output"]
                }
            else:
                return {
                    "function_call": parsed_func,
                    "result": json.dumps(loc["res"]),
                    "reasoning": generated.func_call_reasoning,
                }
        except Exception as e:
            LogWriter.warning(
                f"request_id={req_id_cv.get()} EXIT FindExistingQuery._run() with exception={e}"
            )
            raise ToolException(
                "The function {} did not execute correctly with error: {}".format(parsed_func, e)
            )
