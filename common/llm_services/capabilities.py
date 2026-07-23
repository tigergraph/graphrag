# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>
# for the full license text.

"""Per provider/model capability map for the agentic chat engine.

The agentic path needs reliable **tool-calling**; "deep thinking" mode
additionally benefits from **extended thinking / reasoning**. Detection
is heuristic and conservative — when unsure we return ``False`` so the
agentic engine falls back to the classic LangGraph path rather than
failing at runtime.

The map keys on the resolved chat-config's ``llm_service`` (provider)
and ``llm_model`` (model id), matching the shapes produced by
``get_chat_config`` / ``get_llm_service``.
"""

import logging

logger = logging.getLogger(__name__)

# Region-prefixed Bedrock inference profiles (us./eu./apac./us-gov.) are
# stripped before matching, so "us.anthropic.claude-..." matches the same
# family entry as "anthropic.claude-...".
_BEDROCK_REGION_PREFIXES = ("us.", "eu.", "apac.", "us-gov.")


def _strip_region(model: str) -> str:
    for p in _BEDROCK_REGION_PREFIXES:
        if model.startswith(p):
            return model[len(p):]
    return model


def _bedrock_tool_calling(model: str) -> bool:
    # Anthropic Claude 3+/4, Amazon Nova, Cohere Command-R, Mistral
    # Large, and Meta Llama 3.1+ support Bedrock tool use. Older Titan /
    # Llama 2 / AI21 Jurassic do not.
    return (
        "anthropic.claude-3" in model
        or "anthropic.claude-sonnet-4" in model
        or "anthropic.claude-opus-4" in model
        or "anthropic.claude-haiku-4" in model
        or "amazon.nova" in model
        or "cohere.command-r" in model
        or "mistral.mistral-large" in model
        or "meta.llama3-1" in model
        or "meta.llama3-2" in model
        or "meta.llama3-3" in model
    )


def _bedrock_thinking(model: str) -> bool:
    # Anthropic extended thinking landed with Claude 3.7 / Sonnet 4 / 4.5
    # and the Opus 4 family.
    return (
        "anthropic.claude-3-7" in model
        or "anthropic.claude-sonnet-4" in model
        or "anthropic.claude-opus-4" in model
    )


def _openai_tool_calling(model: str) -> bool:
    # GPT-4 family, GPT-4o, GPT-4.1, GPT-5, o-series, and recent
    # gpt-3.5-turbo all support function/tool calling.
    return (
        model.startswith("gpt-4")
        or model.startswith("gpt-5")
        or model.startswith("o1")
        or model.startswith("o3")
        or model.startswith("o4")
        or "gpt-3.5-turbo" in model
    )


def _openai_thinking(model: str) -> bool:
    return (
        model.startswith("o1")
        or model.startswith("o3")
        or model.startswith("o4")
        or model.startswith("gpt-5")
    )


def openai_rejects_temperature(model: str) -> bool:
    """OpenAI o-series reasoning models (o1/o3/o4) reject a custom
    ``temperature`` — only the default value is accepted, and sending the
    parameter fails the request. Callers should omit ``temperature`` for these
    models. GPT-5 models accept a custom temperature and are not included.
    Case-insensitive.
    """
    m = (model or "").strip().lower()
    return (
        m.startswith("o1")
        or m.startswith("o3")
        or m.startswith("o4")
    )


def _gemini_tool_calling(model: str) -> bool:
    # Gemini 1.5+ and 2.x support function calling.
    return "gemini-1.5" in model or "gemini-2" in model or "gemini-exp" in model


def _gemini_thinking(model: str) -> bool:
    return "gemini-2.5" in model or "thinking" in model


def model_capabilities(config: dict) -> dict:
    """Return ``{"supports_tool_calling": bool, "supports_thinking": bool}``
    for a resolved chat-LLM config. Conservative: unknown → ``False``.
    """
    if not isinstance(config, dict):
        return {"supports_tool_calling": False, "supports_thinking": False}

    service = (config.get("llm_service") or "").strip().lower()
    model = (config.get("llm_model") or "").strip().lower()
    model = _strip_region(model)

    tool_calling = False
    thinking = False

    if service in ("bedrock", "aws_bedrock", "awsbedrock"):
        tool_calling = _bedrock_tool_calling(model)
        thinking = _bedrock_thinking(model)
    elif service in ("openai", "azure", "azure_openai", "azureopenai"):
        tool_calling = _openai_tool_calling(model)
        thinking = _openai_thinking(model)
    elif service in ("vertexai", "google_vertexai", "genai", "google_genai", "googlegenai"):
        tool_calling = _gemini_tool_calling(model)
        thinking = _gemini_thinking(model)
    elif service == "groq":
        # Groq exposes tool use on Llama 3.1+/3.3 and Mixtral.
        tool_calling = "llama-3.1" in model or "llama-3.3" in model or "llama3-groq" in model or "mixtral" in model
    elif service == "ollama":
        # Local models vary; only the families we've verified for tool use.
        tool_calling = "llama3.1" in model or "llama3.2" in model or "qwen2.5" in model or "mistral-nemo" in model
    # sagemaker / watsonx / huggingface endpoints: leave both False
    # (no reliable, uniform tool-calling guarantee) → classic fallback.

    return {"supports_tool_calling": tool_calling, "supports_thinking": thinking}


def model_supports_agentic(config: dict) -> bool:
    """Gate for the agentic engine: requires reliable tool-calling."""
    caps = model_capabilities(config)
    if not caps["supports_tool_calling"]:
        logger.info(
            "Agentic mode unavailable for llm_service=%r llm_model=%r "
            "(no tool-calling support); using classic engine.",
            (config or {}).get("llm_service"),
            (config or {}).get("llm_model"),
        )
    return caps["supports_tool_calling"]
