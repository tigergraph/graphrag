# Copyright (c) 2025 TigerGraph, Inc.
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

from langchain.tools import BaseTool
from langchain.tools.base import ToolException
from langchain.llms.base import LLM
from langchain.prompts import PromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from langchain_community.callbacks.manager import get_openai_callback

from common.metrics.tg_proxy import TigerGraphConnectionProxy
from common.py_schemas import MapQuestionToSchemaResponse, MapAttributeToAttributeResponse
from typing import List, Dict
from .validation_utils import validate_schema, MapQuestionToSchemaException
import re
import logging
from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter
from common.db.connections import get_schema_ver

logger = logging.getLogger(__name__)


class MapQuestionToSchema(BaseTool):
    """MapQuestionToSchema Tool.
    Tool to map questions to their datatypes in the database. Should be executed before GenerateFunction.
    """

    name: str = "MapQuestionToSchema"
    description: str = "Always run first to map the query to the graph's schema. GenerateFunction before using MapQuestionToSchema"
    conn: TigerGraphConnectionProxy = None
    llm: LLM = None
    prompt: str = None
    handle_tool_error: bool = True
    schema_ver: int = None
    vertices: list[str] = None
    edges: list[str] = None
    vertices_info: list[dict] = None
    edges_info: list[dict] = None


    def __init__(self, conn, llm):
        """Initialize MapQuestionToSchema.
        Args:
            conn (TigerGraphConnectionProxy):
                pyTigerGraph TigerGraphConnection connection to the database; this is a proxy which includes metrics gathering.
            llm (LLM_Model):
                LLM_Model class to interact with an external LLM API.
            prompt (str):
                prompt to use with the LLM_Model. Varies depending on LLM service.
        """
        super().__init__()
        logger.debug(f"request_id={req_id_cv.get()} MapQuestionToSchema instantiated")
        self.conn = conn
        self.llm = llm
        self.schema_ver = -1
        self.vertices = []
        self.edges = []
        self.vertices_info = []
        self.edges_info = []


    def _run(self, query: str, conversation: List[Dict[str, str]]) -> str:
        """Run the tool.
        Args:
            query (str):
                The user's question.
        """
        LogWriter.info(f"request_id={req_id_cv.get()} ENTRY MapQuestionToSchema._run()")
        parser = PydanticOutputParser(pydantic_object=MapQuestionToSchemaResponse)

        RESTATE_QUESTION_PROMPT = PromptTemplate(
            template=self.llm.map_question_schema_prompt,
            input_variables=[
                "question",
                "conversation",
                "vertices",
                "verticesAttrs",
                "edges",
                "edgesInfo",
            ],
            partial_variables={"format_instructions": parser.get_format_instructions()},
        )

        restate_chain = RESTATE_QUESTION_PROMPT | self.llm.model | parser

        schema_ver = get_schema_ver(self.conn)
        if schema_ver is None or self.schema_ver != schema_ver:
            self.schema_ver = schema_ver if schema_ver is not None else -1
            self.vertices = self.conn.getVertexTypes()
            self.edges = self.conn.getEdgeTypes()

            for vertex in self.vertices:
                vertex_attrs = self.conn.getVertexAttrs(vertex)
                attributes = [attr[0] for attr in vertex_attrs]
                vertex_info = {"vertex": vertex, "attributes": attributes}
                self.vertices_info.append(vertex_info)

            for edge in self.edges:
                source_vertex = self.conn.getEdgeSourceVertexType(edge)
                target_vertex = self.conn.getEdgeTargetVertexType(edge)
                edge_info = {"edge": edge, "source": source_vertex, "target": target_vertex}
                self.edges_info.append(edge_info)
        else:
            logger.info(f"Reusing existing schema rep for schema version {schema_ver}")

        usage_data = {}
        with get_openai_callback() as cb:
            parsed_q = restate_chain.invoke(
                {
                    "vertices": self.vertices,
                    "verticesAttrs": self.vertices_info,
                    "edges": self.edges,
                    "edgesInfo": self.edges_info,
                    "question": query,
                    "conversation": conversation,
                }
            )
            usage_data["input_tokens"] = cb.prompt_tokens
            usage_data["output_tokens"] = cb.completion_tokens
            usage_data["total_tokens"] = cb.total_tokens
            usage_data["cost"] = cb.total_cost
        # logger.info(f"parsed_q: {parsed_q}")

        logger.debug_pii(
            f"request_id={req_id_cv.get()} MapQuestionToSchema parsed for question={query} into normalized_form={parsed_q}"
        )

        attr_prompt = """For the following source attributes: {parsed_attrs}, map them to the corresponding output attribute in this list: {real_attrs}.
                         Format the response way explained below:
                        {format_instructions}"""

        attr_parser = PydanticOutputParser(
            pydantic_object=MapAttributeToAttributeResponse
        )

        ATTR_MAP_PROMPT = PromptTemplate(
            template=attr_prompt,
            input_variables=["parsed_attrs", "real_attrs"],
            partial_variables={
                "format_instructions": attr_parser.get_format_instructions()
            },
        )

        attr_map_chain = ATTR_MAP_PROMPT | self.llm.model | attr_parser
        if parsed_q.target_vertex_attributes:
            for vertex in parsed_q.target_vertex_attributes.keys():
                with get_openai_callback() as cb:
                    parsed_map = attr_map_chain.invoke(
                        {
                            "parsed_attrs": parsed_q.target_vertex_attributes[vertex],
                            "real_attrs": [attr[0] for attr in self.conn.getVertexAttrs(vertex)],
                        }
                    ).attr_map
                    usage_data["input_tokens"] += cb.prompt_tokens
                    usage_data["output_tokens"] += cb.completion_tokens
                    usage_data["total_tokens"] += cb.total_tokens
                    usage_data["cost"] += cb.total_cost
                if parsed_map:
                    parsed_q.target_vertex_attributes[vertex] = [
                        parsed_map.get(x) for x in list(parsed_q.target_vertex_attributes[vertex])
                    ]

            logger.debug(f"request_id={req_id_cv.get()} MapVertexAttributes applied")

        if parsed_q.target_edge_attributes:
            for edge in parsed_q.target_edge_attributes.keys():
                with get_openai_callback() as cb:
                    parsed_map = attr_map_chain.invoke(
                        {
                            "parsed_attrs": parsed_q.target_edge_attributes[edge],
                            "real_attrs": self.conn.getEdgeAttrs(edge),
                        }
                    ).attr_map
                    usage_data["input_tokens"] += cb.prompt_tokens
                    usage_data["output_tokens"] += cb.completion_tokens
                    usage_data["total_tokens"] += cb.total_tokens
                    usage_data["cost"] += cb.total_cost
                if parsed_map:
                    parsed_q.target_edge_attributes[edge] = [
                        parsed_map[x] for x in list(parsed_q.target_edge_attributes[edge])
                    ]

            logger.debug(f"request_id={req_id_cv.get()} MapEdgeAttributes applied")

        logger.info(f"map_question_to_schema usage: {usage_data}")

        try:
            validate_schema(
                self.conn,
                parsed_q.target_vertex_types,
                parsed_q.target_edge_types,
                parsed_q.target_vertex_attributes,
                parsed_q.target_edge_attributes,
            )
        except MapQuestionToSchemaException as e:
            LogWriter.warning(
                f"request_id={req_id_cv.get()} WARN MapQuestionToSchema to validate schema"
            )
            raise e
        LogWriter.info(f"request_id={req_id_cv.get()} EXIT MapQuestionToSchema._run()")
        # logger.info(f"parsed_q: {parsed_q}")
        return parsed_q

    async def _arun(self) -> str:
        """Use the tool asynchronously."""
        raise NotImplementedError("custom_search does not support async")

    # def _handle_error(self, error:MapQuestionToSchemaException) -> str:
    #    return  "The following errors occurred during tool execution:" + error.args[0]+ "Please make sure to map the question to the schema"
