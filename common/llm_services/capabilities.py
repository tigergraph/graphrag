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
import threading

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
    # Every Gemini family from 1.5 onward supports function/tool calling, and
    # future families (4.x, 5.x, ...) will too. Use a denylist instead of an
    # allowlist so new models work without a code change: any Gemini is capable
    # except the legacy 1.0-era models that predate function calling.
    if "gemini" not in model:
        return False
    if "gemini-1.0" in model or "gemini-pro-vision" in model:
        return False
    if model.strip() == "gemini-pro":  # bare 1.0 alias (versioned ids are fine)
        return False
    return True


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


# ---------------------------------------------------------------------------
# Runtime tool-calling probe (GML-2169)
#
# Policy: Agentic mode is ON by default. We only DISABLE it when we are sure
# the model can't tool-call — a known-legacy model, an explicit "tools not
# supported" error from the probe, or a real tool-calling failure at query
# time (mark_tool_calling_unsupported). Anything uncertain (transient error,
# ambiguous failure, timeout, no provider yet) stays ENABLED and is not cached,
# so a flaky probe never disables a capable model. Results are cached in memory
# (per process), keyed by service:model — never persisted to config, re-probed
# after a restart, and sticky until the model config changes (new key).
# ---------------------------------------------------------------------------

_probe_cache: dict = {}
_probe_lock = threading.Lock()

_PROBE_TIMEOUT_S = 20

# Error text that positively indicates the model rejects tool/function calling.
_NO_TOOL_SUPPORT_MARKERS = (
    "does not support tool",
    "not support tools",
    "tools are not supported",
    "tool use is not supported",
    "tool calling is not supported",
    "function calling is not supported",
    "does not support function",
    "tool_choice is not supported",
    "tools is not supported",
)


def _probe_key(config: dict) -> str:
    service = (config.get("llm_service") or "").strip().lower()
    model = (config.get("llm_model") or "").strip().lower()
    return f"{service}:{model}"


def _known_no_tool_calling(config: dict) -> bool:
    """True only for models we are *sure* predate tool/function calling."""
    service = (config.get("llm_service") or "").strip().lower()
    model = _strip_region((config.get("llm_model") or "").strip().lower())
    if "gemini-1.0" in model or "gemini-pro-vision" in model or model == "gemini-pro":
        return True
    if service in ("openai", "azure", "azure_openai", "azureopenai"):
        if model.startswith(("text-davinci", "davinci", "curie", "babbage", "ada",
                             "text-ada", "text-babbage", "text-curie")):
            return True
    if service in ("bedrock", "aws_bedrock", "awsbedrock"):
        if ("amazon.titan" in model
                or "meta.llama2" in model
                or "ai21.j2" in model
                or "ai21.jurassic" in model
                or "anthropic.claude-instant" in model
                or "anthropic.claude-v2" in model
                or model.startswith("anthropic.claude-2")
                or ("cohere.command" in model and "command-r" not in model)):
            return True
    return False


def _looks_like_no_tool_support(exc) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _NO_TOOL_SUPPORT_MARKERS)


def _invoke_probe(llm):
    from pydantic import BaseModel, Field

    class _ProbePing(BaseModel):
        """Acknowledge readiness by calling this tool."""
        ok: bool = Field(default=True, description="always true")

    bound = llm.bind_tools([_ProbePing])
    bound.invoke([
        ("system", "You can call tools."),
        ("user", "Call the ProbePing tool with ok set to true."),
    ])


def _run_tool_calling_probe(llm_provider):
    """Bind a trivial tool and make a minimal, time-bounded call.

    Returns ``True`` (confirmed tool-calling), ``False`` (the model explicitly
    rejects tools — a *confident* no-support signal), or ``None`` (unknown:
    transient, ambiguous, timeout, or no usable provider). ``None`` must not
    disable Agentic mode.
    """
    import concurrent.futures

    llm = getattr(llm_provider, "llm", None)
    if llm is None or not hasattr(llm, "bind_tools"):
        return None
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(_invoke_probe, llm).result(timeout=_PROBE_TIMEOUT_S)
        return True
    except concurrent.futures.TimeoutError:
        logger.warning("tool-calling probe timed out; leaving Agentic enabled")
        return None
    except Exception as exc:  # noqa: BLE001
        if _looks_like_no_tool_support(exc):
            logger.info("tool-calling probe: model rejects tools (%s)", str(exc)[:200])
            return False
        logger.warning(
            "tool-calling probe inconclusive (%s); leaving Agentic enabled",
            str(exc)[:200],
        )
        return None


def supports_tool_calling(config: dict, llm_provider=None) -> bool:
    """Whether the resolved chat model can drive Agentic mode.

    Optimistic: enabled unless we are sure it can't tool-call. Uses a cached
    in-memory runtime probe; a cached result wins, a known-legacy model is
    disabled, a confident probe/runtime failure disables, and everything else
    stays enabled. Never writes to config.
    """
    if not isinstance(config, dict):
        return False
    key = _probe_key(config)
    with _probe_lock:
        if key in _probe_cache:
            return _probe_cache[key]

    if _known_no_tool_calling(config):
        with _probe_lock:
            _probe_cache[key] = False
        return False

    if llm_provider is None:
        return True  # optimistic; can't probe yet, don't cache

    result = _run_tool_calling_probe(llm_provider)
    if result is True:
        with _probe_lock:
            _probe_cache[key] = True
        return True
    if result is False:  # confident no-support
        with _probe_lock:
            _probe_cache[key] = False
        return False
    return True  # unknown -> stay enabled, don't cache


def mark_tool_calling_unsupported(config: dict) -> None:
    """Record that the chat model failed tool-calling at runtime, so later
    requests downgrade to the classic engine until the model config changes or
    the container restarts."""
    if isinstance(config, dict):
        with _probe_lock:
            _probe_cache[_probe_key(config)] = False


def reset_tool_calling_cache() -> None:
    """Clear the in-memory probe cache (test hook / manual reset)."""
    with _probe_lock:
        _probe_cache.clear()
