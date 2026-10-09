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

import logging
import os

from common.llm_services import LLM_Model
from langchain_google_genai import ChatGoogleGenerativeAI

from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter

logger = logging.getLogger(__name__)


class _ChatGoogleGenerativeAIWithoutAFC(ChatGoogleGenerativeAI):
    """Keep tool execution under GraphRAG's orchestrators.

    The Google Gen AI SDK enables automatic function calling (AFC) by
    default. GraphRAG already owns the tool loop, so allowing the SDK to run
    functions can bypass planning, tracing, and the configured step order.
    ``_prepare_request`` is shared by normal, structured, streaming, and
    tool-bound calls, making this the single place to disable AFC.
    """

    def _prepare_request(self, *args, **kwargs):
        kwargs["automatic_function_calling"] = {"disable": True}
        return super()._prepare_request(*args, **kwargs)


class GoogleGenAI(LLM_Model):
    def __init__(self, config):
        super().__init__(config)
        for auth_detail in config["authentication_configuration"].keys():
            os.environ[auth_detail] = config["authentication_configuration"][
                auth_detail
            ]

        model_name = config["llm_model"]
        self.llm = _ChatGoogleGenerativeAIWithoutAFC(
            temperature=config["model_kwargs"]["temperature"],
            model=model_name,
            timeout=None,
            max_retries=2,
        )
        self.prompt_path = config["prompt_path"]
        LogWriter.info(
            f"request_id={req_id_cv.get()} instantiated GoogleGenAI model_name={model_name}"
        )
