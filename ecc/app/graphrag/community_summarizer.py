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

import re

from langchain_core.prompts import PromptTemplate

from common.llm_services import LLM_Model
from common.py_schemas import CommunitySummary

# src: https://github.com/microsoft/graphrag/blob/main/graphrag/index/graph/extractors/summarize/prompts.py
SUMMARIZE_PROMPT = PromptTemplate.from_template("""
You are a helpful assistant responsible for generating a comprehensive summary of the data provided below.
Given one or two entities, and a list of descriptions, all related to the same entity or group of entities.
Please concatenate all of these into a single, comprehensive description. Make sure to include information collected from all the descriptions.
If the provided descriptions are contradictory, please resolve the contradictions and provide a single, coherent summary, but do not add any information that is not in the description.
Make sure it is written in third person, and include the entity names so we the have full context.

#######
-Data-
Commuinty Title: {entity_name}
Description List: {description_list}
""")

id_pat = re.compile(r"[_\d]*")


class CommunitySummarizer:
    def __init__(
        self,
        llm_service: LLM_Model,
    ):
        self.llm_service = llm_service

    async def summarize(self, name: str, text: list[str]) -> CommunitySummary:
        structured_llm = self.llm_service.model.with_structured_output(CommunitySummary)
        chain = SUMMARIZE_PROMPT | structured_llm

        # remove iteration tags from name
        name = id_pat.sub("", name)
        try:
            summary = await chain.ainvoke(
                {
                    "entity_name": name,
                    "description_list": text,
                }
            )
        except Exception as e:
            return {"error": True, "summary": "", "message": str(e)}
        return {"error": False, "summary": summary.summary}
