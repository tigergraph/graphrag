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
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from typing import Optional
from pydantic import BaseModel, Field
from common.logs.logwriter import LogWriter
from common.logs.log import req_id_cv
from common.utils.token_calculator import get_token_calculator
from common.py_schemas import GraphRAGAnswerOutput

logger = logging.getLogger(__name__)

class TigerGraphAgentGenerator:
    def __init__(self, llm_service):
        self.llm = llm_service
        svc_config = getattr(llm_service, "config", {})
        token_limit = (
            llm_service.context_token_limit()
            if hasattr(llm_service, "context_token_limit")
            else svc_config.get("token_limit")
        )
        self.token_calculator = get_token_calculator(token_limit=token_limit, model_name=svc_config.get("llm_model"))

    def generate_answer(self, question: str, context: str | dict, query: str = "") -> dict:
        """Generate an answer based on the question and context.
        Args:
            question: str: The question to generate an answer for.
            context: str: The context to generate an answer from.
            query: str: The original query used to fetch the conext.
        Returns:
            str: The answer to the question.
        """
        LogWriter.info(f"request_id={req_id_cv.get()} ENTRY generate_answer")

        # Serialize dict context BEFORE truncation so the token counter
        # operates on the same string that ultimately reaches the LLM.
        # Without this the truncation check inspects the dict's repr and
        # ``json.dumps`` (often 1.5-3x longer for Japanese due to \uXXXX
        # escaping) silently overflows the model's input window. Keep
        # ``ensure_ascii=False`` so non-ASCII content stays compact.
        if isinstance(context, dict):
            context = json.dumps(context, ensure_ascii=False)

        answer_parser = PydanticOutputParser(pydantic_object=GraphRAGAnswerOutput)
        prompt = PromptTemplate(
            template=self.llm.chatbot_response_prompt,
            input_variables=["question", "context", "query"],
            partial_variables={
                "format_instructions": answer_parser.get_format_instructions()
            }
        )

        # Trim the context so the full prompt fits the model's token limit.
        if not self.token_calculator.is_unlimited_tokens():
            context = self.token_calculator.fit_context(
                context, prompt.format(question=question, context="", query=query)
            )

        try:
            generation = self.llm.invoke_with_parser(
                prompt, answer_parser,
                {"question": question, "context": context, "query": query},
                caller_name="generate_answer",
                # On malformed JSON, recover the answer (and citation if intact)
                # from the raw model output.
                on_parse_error=self.llm._salvage_answer_output,
            )
        except Exception as e:
            logger.warning(f"generate_answer: generation failed: {type(e).__name__}: {e}")
            logger.debug("generate_answer: generation failure traceback", exc_info=True)
            generation = GraphRAGAnswerOutput(
                generated_answer=(
                    "I wasn't able to generate an answer for this question. "
                    "Try asking again, or rephrase it to be more specific or "
                    "focused on a single topic. If the problem continues, "
                    "contact your administrator for more details."
                ),
                citation=[],
            )

        LogWriter.info(f"request_id={req_id_cv.get()} EXIT generate_answer")

        return generation
