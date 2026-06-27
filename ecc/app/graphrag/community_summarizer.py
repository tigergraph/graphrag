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

import re
import logging

from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import PydanticOutputParser

from common.llm_services import LLM_Model
from common.py_schemas import CommunitySummary

logger = logging.getLogger(__name__)


# src: https://github.com/microsoft/graphrag/blob/main/graphrag/index/graph/extractors/summarize/prompts.py

id_pat = re.compile(r"(_\d+)+$")


class CommunitySummarizer:
    def __init__(
        self,
        llm_service: LLM_Model,
    ):
        self.llm_service = llm_service

    async def summarize(self, name: str, text: list[str]) -> dict:
        summary_parser = PydanticOutputParser(pydantic_object=CommunitySummary)
        # The system prompt owns {format_instructions} (see base_llm A1b);
        # bind it as a partial — do not append it here.
        prompt = PromptTemplate(
            template=self.llm_service.community_summarize_prompt,
            input_variables=["entity_name", "description_list"],
            partial_variables={"format_instructions": summary_parser.get_format_instructions()},
        )

        # remove iteration tags from name
        name = id_pat.sub("", name)
        try:
            summary = await self.llm_service.ainvoke_with_parser(
                prompt, summary_parser,
                {"entity_name": name, "description_list": text},
                caller_name="community_summarize",
            )
        except Exception as e:
            return {"error": True, "summary": "", "message": str(e)}
        return {"error": False, "summary": summary.summary}