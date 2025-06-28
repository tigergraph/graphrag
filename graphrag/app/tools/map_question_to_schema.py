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

        vertices = self.conn.getVertexTypes()
        edges = self.conn.getEdgeTypes()

        vertices_info = []
        for vertex in vertices:
            vertex_attrs = self.conn.getVertexAttrs(vertex)
            attributes = [attr[0] for attr in vertex_attrs]
            vertex_info = {"vertex": vertex, "attributes": attributes}
            vertices_info.append(vertex_info)

        edges_info = []
        for edge in edges:
            source_vertex = self.conn.getEdgeSourceVertexType(edge)
            target_vertex = self.conn.getEdgeTargetVertexType(edge)
            edge_info = {"edge": edge, "source": source_vertex, "target": target_vertex}
            edges_info.append(edge_info)

        usage_data = {}
        with get_openai_callback() as cb:
            parsed_q = restate_chain.invoke(
                {
                    "vertices": vertices,
                    "verticesAttrs": vertices_info,
                    "edges": edges,
                    "edgesInfo": edges_info,
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
