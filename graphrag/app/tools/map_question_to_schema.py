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

from langchain_core.tools import BaseTool
from langchain_core.tools import ToolException
from langchain_core.language_models.llms import LLM
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import PydanticOutputParser

from common.metrics.tg_proxy import TigerGraphConnectionProxy
from common.py_schemas import MapQuestionToSchemaResponse, MapAttributeToAttributeResponse
from typing import List, Dict
from .validation_utils import validate_schema, MapQuestionToSchemaException
import re
import logging
from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter
from common.db.connections import get_schema_ver
from common.db.schema_utils import read_type_metadata

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
            partial_variables={
                "format_instructions": parser.get_format_instructions(),
                # Pre-bind the Query Guidance partial so every render
                # picks up the current per-graph / global override.
                # ``query_guidance_block`` is empty when no override is
                # configured, leaving the template effectively unchanged.
                "query_guidance": self.llm.query_guidance_block,
            },
        )

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

        # Pull the user-defined ``description`` / ``definition`` from
        # the EntityType / RelationshipType meta vertices so the LLM
        # sees the same domain hints that the cypher/gsql generators
        # already get. Empty when no metadata is set — the surface
        # shape stays a plain list of names for those types.
        try:
            entity_descs, rel_defs = read_type_metadata(self.conn)
        except Exception as exc:
            logger.warning(f"read_type_metadata failed in mq2s: {exc}")
            entity_descs, rel_defs = {}, {}

        def _label(name: str, desc_map: dict) -> str:
            d = desc_map.get(name)
            return f"{name} ({d})" if d else name

        vertices_for_llm = [_label(v, entity_descs) for v in self.vertices]
        edges_for_llm = [_label(e, rel_defs) for e in self.edges]

        parsed_q = self.llm.invoke_with_parser(
            RESTATE_QUESTION_PROMPT, parser,
            {
                "vertices": vertices_for_llm,
                "verticesAttrs": self.vertices_info,
                "edges": edges_for_llm,
                "edgesInfo": self.edges_info,
                "question": query,
                "conversation": conversation,
            },
            caller_name="map_question_to_schema",
        )
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

        if parsed_q.target_vertex_attributes:
            for vertex in parsed_q.target_vertex_attributes.keys():
                parsed_map = self.llm.invoke_with_parser(
                    ATTR_MAP_PROMPT, attr_parser,
                    {
                        "parsed_attrs": parsed_q.target_vertex_attributes[vertex],
                        "real_attrs": [attr[0] for attr in self.conn.getVertexAttrs(vertex)],
                    },
                    caller_name="map_vertex_attributes",
                ).attr_map
                if parsed_map:
                    parsed_q.target_vertex_attributes[vertex] = [
                        parsed_map.get(x) for x in list(parsed_q.target_vertex_attributes[vertex])
                    ]

            logger.debug(f"request_id={req_id_cv.get()} MapVertexAttributes applied")

        if parsed_q.target_edge_attributes:
            for edge in parsed_q.target_edge_attributes.keys():
                parsed_map = self.llm.invoke_with_parser(
                    ATTR_MAP_PROMPT, attr_parser,
                    {
                        "parsed_attrs": parsed_q.target_edge_attributes[edge],
                        "real_attrs": self.conn.getEdgeAttrs(edge),
                    },
                    caller_name="map_edge_attributes",
                ).attr_map
                if parsed_map:
                    parsed_q.target_edge_attributes[edge] = [
                        parsed_map[x] for x in list(parsed_q.target_edge_attributes[edge])
                    ]

            logger.debug(f"request_id={req_id_cv.get()} MapEdgeAttributes applied")

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
