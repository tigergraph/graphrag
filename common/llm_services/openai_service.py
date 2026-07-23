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

import logging
import os

from langchain_openai.chat_models import ChatOpenAI

from common.llm_services import LLM_Model
from common.llm_services.capabilities import openai_rejects_temperature
from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter

logger = logging.getLogger(__name__)


class OpenAI(LLM_Model):
    def __init__(self, config):
        super().__init__(config)
        for auth_detail in config["authentication_configuration"].keys():
            os.environ[auth_detail] = config["authentication_configuration"][
                auth_detail
            ]

        model_name = config["llm_model"]
        base_url = config.get("base_url")
        llm_kwargs = {"model_name": model_name, "base_url": base_url}
        # o-series reasoning models reject the temperature parameter; only pass
        # it for models that accept a custom value.
        if not openai_rejects_temperature(model_name):
            llm_kwargs["temperature"] = config["model_kwargs"]["temperature"]
        self.llm = ChatOpenAI(**llm_kwargs)
        self.prompt_path = config["prompt_path"]
        LogWriter.info(
            f"request_id={req_id_cv.get()} instantiated OpenAI model_name={model_name}"
        )
