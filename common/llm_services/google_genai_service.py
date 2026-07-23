# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>
# for the full license text.

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
