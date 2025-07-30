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

import logging
from langchain.prompts import PromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from langchain_community.callbacks.manager import get_openai_callback
from pydantic import BaseModel, Field
from common.logs.logwriter import LogWriter
from common.logs.log import req_id_cv


logger = logging.getLogger(__name__)

class GraphRAGAnswerOutput(BaseModel):
    generated_answer: str = Field(description="The generated answer to the question. Make sure maintain a professional tone.")
    citation: list[str] = Field(description="The citation for the answer. List the information used.")

class TigerGraphAgentGenerator:
    def __init__(self, llm_model):
        self.llm = llm_model

    def generate_answer(self, question: str, context: str, query: str = "") -> dict:
        """Generate an answer based on the question and context.
        Args:
            question: str: The question to generate an answer for.
            context: str: The context to generate an answer from.
            query: str: The original query used to fetch the conext.
        Returns:
            str: The answer to the question.
        """
        LogWriter.info(f"request_id={req_id_cv.get()} ENTRY generate_answer")

        answer_parser = PydanticOutputParser(pydantic_object=GraphRAGAnswerOutput)

        prompt = PromptTemplate(
            template=self.llm.chatbot_response_prompt,
            input_variables=["question", "context", "query"],
            partial_variables={
                "format_instructions": answer_parser.get_format_instructions()
            }
        )

        full_prompt = prompt.format(
            question=question,
            context=context,
            query=query,
            format_instructions=answer_parser.get_format_instructions()
        )

        # Chain
        rag_chain = prompt | self.llm.model | answer_parser

        usage_data = {}
        with get_openai_callback() as cb:
            generation = rag_chain.invoke({"question": question, "context": context, "query": query})

            usage_data["input_tokens"] = cb.prompt_tokens
            usage_data["output_tokens"] = cb.completion_tokens
            usage_data["total_tokens"] = cb.total_tokens
            usage_data["cost"] = cb.total_cost
            logger.info(f"generate_answer usage: {usage_data}")
        LogWriter.info(f"request_id={req_id_cv.get()} EXIT generate_answer")

        return generation
