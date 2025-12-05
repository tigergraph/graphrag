import base64
import io
import logging
from langchain_core.messages import HumanMessage, SystemMessage

from common.config import get_multimodal_service

logger = logging.getLogger(__name__)

def describe_image_with_llm(file_path):
    """
    Read image file and convert to base64 to send to LLM.
    """
    try:
        from PIL import Image as PILImage
        
        client = get_multimodal_service()
        if not client:
            return "[Image: Failed to create multimodal LLM client]"

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
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                    },
                ],
            ),
        ]

        langchain_client = client.llm
        response = langchain_client.invoke(messages)

        return response.content if hasattr(response, "content") else str(response)

    except Exception as e:
        logger.error(f"Failed to describe image with LLM: {str(e)}")
        return "[Image: Error processing image description]"




