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

from langchain_core.prompts import PromptTemplate
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

    def route_question(self, question: str, conversation: list[dict[str, str]] = None) -> str:
        """Route a question to the appropriate datasource.

        Args:
            question (str): The question to route.

        Returns:
            str: The datasource to use for the question.
        """
        LogWriter.info(f"request_id={req_id_cv.get()} ENTRY route_question with {question}")
        v_types = self.db_conn.getVertexTypes()
        e_types = self.db_conn.getEdgeTypes()

        router_parser = PydanticOutputParser[RouterResponse](pydantic_object=RouterResponse)

        prompt = PromptTemplate(
            template=self.llm.route_response_prompt,
            input_variables=["question", "v_types", "e_types", "conversation"],
            partial_variables={
                "format_instructions": router_parser.get_format_instructions()
            }
        )

        res = self.llm.invoke_with_parser(
            prompt, router_parser,
            {"question": question, "v_types": v_types, "e_types": e_types, "conversation": conversation},
            caller_name="route_question",
        )
        LogWriter.info(f"request_id={req_id_cv.get()} EXIT route_question with {res}")
        return res
