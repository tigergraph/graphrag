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
import os

from common.llm_services import LLM_Model
from langchain_google_genai import ChatGoogleGenerativeAI

from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter

logger = logging.getLogger(__name__)


class GoogleGenAI(LLM_Model):
    def __init__(self, config):
        super().__init__(config)
        for auth_detail in config["authentication_configuration"].keys():
            os.environ[auth_detail] = config["authentication_configuration"][
                auth_detail
            ]

        model_name = config["llm_model"]
        self.llm = ChatGoogleGenerativeAI(
            temperature=config["model_kwargs"]["temperature"],
            model=model_name,
            max_tokens=None,
            timeout=None,
            max_retries=2,
        )
        self.prompt_path = config["prompt_path"]
        LogWriter.info(
            f"request_id={req_id_cv.get()} instantiated OpenAI model_name={model_name}"
        )

    @property
    def map_question_schema_prompt(self):
        return self._read_prompt_file(self.prompt_path + "map_question_to_schema.txt")

    @property
    def generate_function_prompt(self):
        return self._read_prompt_file(self.prompt_path + "generate_function.txt")

    @property
    def generate_cypher_prompt(self):
        filepath = self.prompt_path + "generate_cypher.txt"
        if os.path.exists(filepath):
            return self._read_prompt_file(filepath)
        else:
            return super().generate_cypher_prompt

    @property
    def generate_gsql_prompt(self):
        filepath = self.prompt_path + "generate_gsql.txt"
        if os.path.exists(filepath):
            return self._read_prompt_file(filepath)
        else:
            return super().generate_gsql_prompt

    @property
    def entity_relationship_extraction_prompt(self):
        return self._read_prompt_file(
            self.prompt_path + "entity_relationship_extraction.txt"
        )

    @property
    def route_response_prompt(self):
        filepath = self.prompt_path + "route_response.txt"
        if os.path.exists(filepath):
            return self._read_prompt_file(filepath)
        else:
            return super().route_response_prompt

    @property
    def graphrag_scoring_prompt(self):
        filepath = self.prompt_path + "graphrag_scoring.txt"
        if os.path.exists(filepath):
            return self._read_prompt_file(filepath)
        else:
            return super().graphrag_scoring_prompt

    @property
    def keyword_extraction_prompt(self):
        filepath = self.prompt_path + "keyword_extraction.txt"
        if os.path.exists(filepath):
            return self._read_prompt_file(filepath)
        else:
            return super().keyword_extraction_prompt

    @property
    def question_expansion_prompt(self):
        filepath = self.prompt_path + "question_expansion.txt"
        if os.path.exists(filepath):
            return self._read_prompt_file(filepath)
        else:
            return super().question_expansion_prompt

    @property
    def supportai_response_prompt(self):
        filepath = self.prompt_path + "supportai_response.txt"
        if os.path.exists(filepath):
            return self._read_prompt_file(filepath)
        else:
            return super().supportai_response_prompt

    @property
    def chatbot_response_prompt(self):
        filepath = self.prompt_path + "chatbot_response.txt"
        if os.path.exists(filepath):
            return self._read_prompt_file(filepath)
        else:
            return super().chatbot_response_prompt

    @property
    def hyde_prompt(self):
        filepath = self.prompt_path + "hyde.txt"
        if os.path.exists(filepath):
            return self._read_prompt_file(filepath)
        else:
            return super().hyde_prompt

    @property
    def model(self):
        return self.llm
