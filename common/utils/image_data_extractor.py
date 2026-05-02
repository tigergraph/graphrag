import base64
import io
import logging
from langchain_core.messages import HumanMessage, SystemMessage
from common.config import get_llm_service, get_multimodal_config

logger = logging.getLogger(__name__)

_multimodal_client = None
_multimodal_provider = None

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

def describe_image_with_llm(file_path):
    """
    Read image file and convert to base64 to send to LLM.
    """
    try:
        from PIL import Image as PILImage
        
        client = _get_client()
        if not client:
            return "Image: Failed to create multimodal LLM client"
        # Read image and convert to base64
        pil_image = PILImage.open(file_path)
        buffer = io.BytesIO()
        if pil_image.mode != 'RGB':
            pil_image = pil_image.convert('RGB')
        pil_image.save(buffer, format="JPEG", quality=95)
        image_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
        messages = [
            SystemMessage(
                content="You are a helpful assistant that describes images concisely for document analysis."
            ),
            HumanMessage(
                content=[
                    {
                        "type": "text",
                        "text": (
                            "Please describe what you see in this image and "
                            "if the image has scanned text then extract all the text. "
                            "If the image has any graph, chart, table, or other diagram, describe it. "
                            "If the image has any logo, identify and describe the logo."
                        ),
                    },
                    _build_image_content_block(image_base64, "image/jpeg"),
                ],
            ),
        ]

        langchain_client = client.llm
        response = langchain_client.invoke(messages)
        return response.content if hasattr(response, "content") else str(response)
    except Exception as e:
        error_str = str(e).lower()
        if "throttl" in error_str or "rate" in error_str or "too many" in error_str:
            raise  # Let caller retry on rate limit
        logger.error(f"Failed to describe image with LLM: {str(e)}")
        return "Image: Error processing image description"
