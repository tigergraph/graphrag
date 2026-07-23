# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>
# for the full license text.

import boto3, botocore
from langchain_aws import ChatBedrock
import logging
from common.llm_services import LLM_Model
from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter

logger = logging.getLogger(__name__)


#: Per-model-family ``max_tokens`` caps for Bedrock-hosted models.
#: Keys are case-insensitive prefixes / substrings of the model id; the
#: longest matching prefix wins. Models not matched fall back to the
#: generic default (see :data:`_BEDROCK_MAX_TOKENS_DEFAULT`).
#:
#: References (cap = max output tokens supported by the model on Bedrock):
#:   - Anthropic Claude 3 / 3.5 Haiku / 3 Opus: 4096
#:   - Anthropic Claude 3.5 / 3.7 Sonnet: 8192
#:   - Anthropic Claude Sonnet 4 / 4.5: 64000 (we cap at 8192 for safety)
#:   - Amazon Titan Text: 4096
#:   - Amazon Nova: 5120
#:   - Cohere Command: 4000
#:   - Meta Llama 2: 2048; Llama 3: 4096
#:   - AI21 Jurassic / Jamba: 4096
#:   - Mistral: 8192
_BEDROCK_MAX_TOKENS_BY_MODEL: tuple = (
    # Anthropic Claude 3.x family — explicit capped at 4096
    ("anthropic.claude-3-5-haiku", 4096),
    ("anthropic.claude-3-haiku", 4096),
    ("anthropic.claude-3-opus", 4096),
    ("anthropic.claude-3-sonnet", 4096),
    ("anthropic.claude-instant", 4096),
    # Anthropic Claude 3.5 / 3.7 Sonnet — 8192
    ("anthropic.claude-3-5-sonnet", 8192),
    ("anthropic.claude-3-7-sonnet", 8192),
    # Amazon Titan Text models — capped at 4096
    ("amazon.titan-text", 4096),
    ("amazon.titan-tg1", 4096),
    # Cohere Command — 4000
    ("cohere.command", 4000),
    # Meta Llama
    ("meta.llama2", 2048),
    ("meta.llama3", 4096),
    # AI21 Jamba / Jurassic
    ("ai21.", 4096),
)
_BEDROCK_MAX_TOKENS_DEFAULT = 8192


def _bedrock_max_tokens_for_model(model_id: str) -> int:
    """Return the recommended ``max_tokens`` for the given Bedrock model
    id. Falls back to :data:`_BEDROCK_MAX_TOKENS_DEFAULT` when no
    family-specific cap is registered.
    """
    if not model_id:
        return _BEDROCK_MAX_TOKENS_DEFAULT
    mid = model_id.lower()
    # Cross-region inference profiles are prefixed with the region
    # short code (``us.``, ``eu.``, ``apac.``, ``us-gov.``); strip so
    # ``us.anthropic.claude-3-haiku-...`` matches the same family.
    for prefix in ("us.", "eu.", "apac.", "us-gov."):
        if mid.startswith(prefix):
            mid = mid[len(prefix):]
            break
    # Walk the table in order; longest-prefix match wins. The table is
    # already sorted with more-specific entries first
    # (``claude-3-5-haiku`` before ``claude-3-haiku``).
    best: tuple = ("", _BEDROCK_MAX_TOKENS_DEFAULT)
    for prefix, cap in _BEDROCK_MAX_TOKENS_BY_MODEL:
        if mid.startswith(prefix) and len(prefix) > len(best[0]):
            best = (prefix, cap)
    return best[1]


class AWSBedrock(LLM_Model):
    def __init__(self, config):
        super().__init__(config)
        model_name = config["llm_model"]

        boto3_config = config.get("boto3_config", {})
        client_config = botocore.config.Config(
            max_pool_connections=boto3_config.get("max_pool_connections", 50),
            read_timeout=boto3_config.get("read_timeout", 300),
            retries={"max_attempts": boto3_config.get("retries", 5)},
        )

        client = boto3.client(
            "bedrock-runtime",
            region_name=config.get("region_name", "us-east-1"),
            config=client_config,
            aws_access_key_id=config["authentication_configuration"][
                "AWS_ACCESS_KEY_ID"
            ],
            aws_secret_access_key=config["authentication_configuration"][
                "AWS_SECRET_ACCESS_KEY"
            ],
        )
        # Resolve ``max_tokens`` so the langchain-aws built-in default
        # of 1024 (Anthropic Claude on InvokeModel) doesn't truncate
        # large prompts. Priority:
        #   1. ``model_kwargs["max_tokens"]`` — explicit per-deployment override
        #   2. ``token_limit`` config field — shared with retrieval-side context cap
        #   3. Known model-family cap (Claude 3.x, Titan, Cohere, etc.)
        #   4. Generic fallback: 8192
        merged_kwargs = dict(config.get("model_kwargs") or {"temperature": 0})
        if "max_tokens" not in merged_kwargs:
            cfg_limit = config.get("token_limit")
            if isinstance(cfg_limit, int) and cfg_limit > 0:
                merged_kwargs["max_tokens"] = cfg_limit
            else:
                merged_kwargs["max_tokens"] = _bedrock_max_tokens_for_model(model_name)
        self.llm = ChatBedrock(
            client=client,
            model_id=model_name,
            region_name=config.get("region_name", "us-east-1"),
            model_kwargs=merged_kwargs,
        )

        self.prompt_path = config["prompt_path"]
        LogWriter.info(
            f"request_id={req_id_cv.get()} instantiated AWSBedrock model_name={model_name}"
        )
