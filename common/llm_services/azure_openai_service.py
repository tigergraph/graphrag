import os
import logging
from common.llm_services import LLM_Model
from common.llm_services.capabilities import openai_rejects_temperature
from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter

logger = logging.getLogger(__name__)


class AzureOpenAI(LLM_Model):
    def __init__(self, config):
        super().__init__(config)
        for auth_detail in config["authentication_configuration"].keys():
            os.environ[auth_detail] = config["authentication_configuration"][
                auth_detail
            ]
        from langchain_openai import AzureChatOpenAI

        model_name = config["llm_model"]
        llm_kwargs = {
            "azure_deployment": config["azure_deployment"],
            "openai_api_version": config["openai_api_version"],
            "model_name": config["llm_model"],
        }
        # o-series reasoning models reject the temperature parameter; only pass
        # it for models that accept a custom value.
        if not openai_rejects_temperature(model_name):
            llm_kwargs["temperature"] = config["model_kwargs"]["temperature"]
        self.llm = AzureChatOpenAI(**llm_kwargs)

        self.prompt_path = config["prompt_path"]
        LogWriter.info(
            f"request_id={req_id_cv.get()} instantiated AzureOpenAI model_name={model_name}"
        )
