import base64
import io
import logging
import os
import re
from langchain_core.messages import HumanMessage, SystemMessage
from common.config import get_llm_service, get_multimodal_config

logger = logging.getLogger(__name__)

_multimodal_client = None
_multimodal_provider = None


def _graphrag_cfg(graphname=None) -> dict:
    try:
        from common.config import get_graphrag_config
        return get_graphrag_config(graphname) or {}
    except Exception:
        return {}


def should_extract_images(graphname=None) -> bool:
    """Whether to run the multimodal LLM on extracted images. Resolved from
    per-graph or global ``graphrag_config.extract_images``; defaults to True."""
    cfg = _graphrag_cfg(graphname)
    if "extract_images" in cfg:
        return bool(cfg["extract_images"])
    return True


def min_image_dim_px(graphname=None) -> int:
    """Smallest side (in px) an image must have to be sent to the LLM.
    Resolved from per-graph or global ``graphrag_config.min_image_dim_px``;
    defaults to 100."""
    cfg = _graphrag_cfg(graphname)
    try:
        return int(cfg.get("min_image_dim_px", 100))
    except (TypeError, ValueError):
        return 100


def image_describe_workers(graphname=None) -> int:
    """Per-PDF thread-pool size for the multimodal describe pass.
    Reuses ``graphrag_config.default_concurrency`` (defaults to 10) so
    deployments only tune one concurrency knob."""
    cfg = _graphrag_cfg(graphname)
    try:
        return max(1, int(cfg.get("default_concurrency", 10)))
    except (TypeError, ValueError):
        return 10


def is_decorative(description: str) -> bool:
    """True when the multimodal LLM signalled the image carries no
    retrieval-worthy content. Robust to trailing punctuation / case."""
    if not description:
        return False
    cleaned = description.strip().lower().rstrip(".").strip()
    return cleaned == "decorative image"

def _get_client():
    global _multimodal_client, _multimodal_provider
    if _multimodal_client is None and get_multimodal_config():
        try:
            config = get_multimodal_config()
            _multimodal_provider = config.get("llm_service", "").lower()
            _multimodal_client = get_llm_service(config)
        except Exception:
            logger.warning("Failed to create multimodal LLM client")
    return _multimodal_client

def _build_image_content_block(image_base64: str, media_type: str) -> dict:
    """Build a LangChain image content block appropriate for the configured provider."""
    if _multimodal_provider in ("genai", "vertexai"):
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{image_base64}"},
        }
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": image_base64},
    }

_DESCRIBE_PROMPT = (
    "Describe the substantive CONTENT of this image so it "
    "can be retrieved alongside the surrounding document. "
    "Prioritize, in this order: (1) any text — copy it "
    "verbatim, including headings, labels, axis ticks, "
    "captions, and footnotes; (2) the data and structure of "
    "any chart, graph, or table — name the chart type, the "
    "axes / columns, and the values or trend the chart "
    "actually shows. For time-series charts (line / bar / "
    "stacked bar with a time-period axis), TRANSCRIBE every "
    "(period, value) pair you can read in the format "
    "`period: value; period: value; …` — do not summarize "
    "the trend in place of the values. For categorical "
    "bar/rank charts (categories such as regions, "
    "companies, or age groups on one axis and a numeric "
    "unit on the other), TRANSCRIBE every visible "
    "category with its value as "
    "`category: value; category: value; …` including the "
    "unit — never list categories without their numbers; "
    "(3) the entities, "
    "relationships, or process steps in any diagram or "
    "flowchart; (4) any logo or branding mark, identified by "
    "name. Do NOT describe layout, background color, "
    "decorative styling, slide templates, or generic visual "
    "impressions — those add no retrieval value. Write the "
    "description in the same language as the text inside the "
    "image; if the image has no text, infer the document's "
    "language from any visible labels, captions, or branding "
    "and match that. Default to English only if no language "
    "signal is present. EXCEPTION: if the image is purely "
    "decorative (no text, no data, no diagram), reply with "
    "exactly the English phrase \"decorative image\" "
    "(lowercase, no punctuation, no translation) and nothing "
    "else — this is a fixed sentinel, never localized. "
    "Respond as a SINGLE plain-text paragraph — no markdown "
    "headings, no bullet lists, no blank lines. The reply is "
    "used verbatim as the alt-text inside `![alt](url)`."
)


def describe_image_with_llm(file_path):
    """
    Read image file and convert to base64 to send to LLM.
    """
    try:
        return _ask_about_image(
            file_path,
            "You are a helpful assistant that describes images concisely for document analysis.",
            _DESCRIBE_PROMPT,
            usage_kind="image_description",
            log_kind="multimodal_describe",
        )
    except Exception as e:
        error_str = str(e).lower()
        if "throttl" in error_str or "rate" in error_str or "too many" in error_str:
            raise  # Let caller retry on rate limit
        logger.error(f"Failed to describe image with LLM: {str(e)}")
        return "Image: Error processing image description"


_FORMULA_PROMPT = (
    "Transcribe the mathematical formula in this image as LaTeX. Keep every "
    "symbol, subscript, superscript and fraction. Keep the text labels in the "
    "formula in their original language, inside \\text{}. When the formula "
    "has an equation number, add it as \\tag{n}. Reply with the LaTeX only: no "
    "$ signs, no code fence and no explanation. If the image has no formula, "
    "reply with nothing."
)


def transcribe_formula_with_llm(file_path) -> str:
    """LaTeX for the formula in an image, or "" when the model gives none."""
    try:
        text = _ask_about_image(
            file_path,
            "You transcribe mathematical formulas from document images.",
            _FORMULA_PROMPT,
            usage_kind="formula_transcription",
            log_kind="multimodal_formula",
        )
    except Exception as e:
        error_str = str(e).lower()
        if "throttl" in error_str or "rate" in error_str or "too many" in error_str:
            raise
        logger.error(f"Failed to transcribe formula with LLM: {str(e)}")
        return ""
    text = (text or "").strip()
    # Some models still wrap the reply in a code fence or in $ signs.
    text = re.sub(r"^```(?:latex|tex)?\s*|\s*```$", "", text).strip()
    text = re.sub(r"^\$+|\$+$", "", text).strip()
    return text


def _ask_about_image(file_path, system_text: str, prompt: str, usage_kind: str,
                     log_kind: str) -> str:
    """Send one image and a prompt to the multimodal model; return its text."""
    from PIL import Image as PILImage
    import time

    client = _get_client()
    if not client:
        return "Image: Failed to create multimodal LLM client"
    pil_image = PILImage.open(file_path)
    buffer = io.BytesIO()
    if pil_image.mode != 'RGB':
        pil_image = pil_image.convert('RGB')
    pil_image.save(buffer, format="JPEG", quality=95)
    image_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
    messages = [
        SystemMessage(content=system_text),
        HumanMessage(
            content=[
                {"type": "text", "text": prompt},
                _build_image_content_block(image_base64, "image/jpeg"),
            ],
        ),
    ]
    langchain_client = client.llm
    # Tag the upcoming chat completion as a multimodal image
    # describe so it's distinguishable from text-only completions
    # in the log stream (e.g. schema extraction, retriever LLM
    # calls). Image-describe runs are typically dozens-to-hundreds
    # per PDF, while text completions are one-shot.
    image_basename = os.path.basename(str(file_path))
    model_name = (
        getattr(_multimodal_client, "config", {}).get("llm_model")
        if _multimodal_client else None
    ) or "?"
    logger.info(
        f"{log_kind}: image={image_basename} "
        f"model={model_name} provider={_multimodal_provider}"
    )
    t0 = time.monotonic()
    from langchain_community.callbacks.manager import get_openai_callback
    from common.llm_services.base_llm import _record_usage, usage_from
    with get_openai_callback() as cb:
        response = langchain_client.invoke(messages)
        _record_usage(usage_kind, usage_from(cb, response),
                      getattr(client, "config", None))
    elapsed = time.monotonic() - t0
    logger.info(
        f"{log_kind} done: image={image_basename} "
        f"elapsed={elapsed:.2f}s"
    )
    # Thinking models (Gemini 3 and others) return the reply as a list
    # of parts; keep the text parts.
    from common.llm_services.base_llm import LLM_Model
    return LLM_Model._message_text(response)
