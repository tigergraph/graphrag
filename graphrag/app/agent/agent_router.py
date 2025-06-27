from langchain.prompts import PromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field
from common.logs.logwriter import LogWriter
from common.logs.log import req_id_cv
from pyTigerGraph.pyTigerGraph import TigerGraphConnection
import logging

logger = logging.getLogger(__name__)

class RouterResponse(BaseModel):
    datasource: str = Field(description="The datasource to use for the question")

class TigerGraphAgentRouter:
    def __init__(self, llm_model, db_conn: TigerGraphConnection):
        self.llm = llm_model
        self.db_conn = db_conn

    def route_question(self, question: str) -> str:
        """Route a question to the appropriate datasource.

        Args:
            question (str): The question to route.

        Returns:
            str: The datasource to use for the question.
        """
        LogWriter.info(f"request_id={req_id_cv.get()} ENTRY route_question with {question}")
        v_types = self.db_conn.getVertexTypes()
        e_types = self.db_conn.getEdgeTypes()

        router_parser = PydanticOutputParser(pydantic_object=RouterResponse)

        prompt = PromptTemplate(
            template=self.llm.route_response_prompt,
            input_variables=["question", "v_types", "e_types"],
            partial_variables={
                "format_instructions": router_parser.get_format_instructions()
            }
        )

        question_router = prompt | self.llm.model | router_parser
        res = question_router.invoke({"question": question, "v_types": v_types, "e_types": e_types})
        LogWriter.info(f"request_id={req_id_cv.get()} EXIT route_question with {res}")
        return res
