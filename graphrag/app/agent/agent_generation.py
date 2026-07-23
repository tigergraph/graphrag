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
        self.token_calculator = get_token_calculator(token_limit=svc_config.get("token_limit"), model_name=svc_config.get("llm_model"))

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

        # Truncate context to fit within token limit
        if not self.token_calculator.is_unlimited_tokens():
            # Reserve tokens for question, query, and format instructions (approximately 1000 tokens)
            max_context_tokens = self.token_calculator.get_max_context_tokens() - 1000

            if len(context) > max_context_tokens:
                context_tokens = self.token_calculator.count_tokens(context)
                if context_tokens > max_context_tokens:
                    context = self.token_calculator.truncate_to_token_limit(context, max_context_tokens)
                    logger.info(f"Truncated context from {context_tokens} to {max_context_tokens} tokens")

        answer_parser = PydanticOutputParser(pydantic_object=GraphRAGAnswerOutput)
        prompt = PromptTemplate(
            template=self.llm.chatbot_response_prompt,
            input_variables=["question", "context", "query"],
            partial_variables={
                "format_instructions": answer_parser.get_format_instructions()
            }
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
        except Exception:
            logger.warning("generate_answer: generation failed")
            generation = GraphRAGAnswerOutput(
                generated_answer="I wasn't able to generate an answer for this question.",
                citation=[],
            )

        LogWriter.info(f"request_id={req_id_cv.get()} EXIT generate_answer")

        return generation
