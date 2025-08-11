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

import boto3
from langchain_community.chat_models import BedrockChat
import logging
from common.llm_services import LLM_Model
from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter

logger = logging.getLogger(__name__)


class AWSBedrock(LLM_Model):
    def __init__(self, config):
        super().__init__(config)
        model_name = config["llm_model"]
        client = boto3.client(
            "bedrock-runtime",
            region_name="us-east-1",
            aws_access_key_id=config["authentication_configuration"][
                "AWS_ACCESS_KEY_ID"
            ],
            aws_secret_access_key=config["authentication_configuration"][
                "AWS_SECRET_ACCESS_KEY"
            ],
        )
        self.llm = BedrockChat(
            client=client,
            model_id=model_name,
            model_kwargs={"temperature": 0},
        )

        self.prompt_path = config["prompt_path"]
        LogWriter.info(
            f"request_id={req_id_cv.get()} instantiated AWSBedrock model_name={model_name}"
        )

    @property
    def map_question_schema_prompt(self):
        return self._read_prompt_file(self.prompt_path + "map_question_to_schema.txt")

    @property
    def generate_function_prompt(self):
        return self._read_prompt_file(self.prompt_path + "generate_function.txt")

    @property
    def entity_relationship_extraction_prompt(self):
        return self._read_prompt_file(
            self.prompt_path + "entity_relationship_extraction.txt"
        )

    @property
    def model(self):
        return self.llm
