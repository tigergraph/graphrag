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
            timeout=None,
            max_retries=2,
        )
        self.prompt_path = config["prompt_path"]
        LogWriter.info(
            f"request_id={req_id_cv.get()} instantiated GoogleGenAI model_name={model_name}"
        )
