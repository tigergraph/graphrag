"""Image links in a final answer, rewritten so the chat UI can load them.

Ingestion refers to each stored image as ``![description](tg://image_id)``,
and the answer prompts tell the model to keep those links. The browser cannot
load ``tg://``, so every engine passes its final answer through
``resolve_image_links`` before it returns.
"""
import logging
import re

logger = logging.getLogger(__name__)

_TG_IMAGE = re.compile(r"!\[([^\]]*)\]\(tg://([^\)]+)\)")
_S3_URL = re.compile(r"\(s3://([^/]+)/([^\)]+)\)")


def tg_links_to_endpoints(text, graphname: str):
    """``![d](tg://id)`` -> ``![d](/ui/image_vertex/<graphname>/id)``.

    The endpoint serves the Image vertex with the caller's HTTP Basic auth;
    PATH_PREFIX is applied at the router, so the URL stays relative.
    """
    if not isinstance(text, str) or "(tg://" not in text:
        return text
    converted, count = _TG_IMAGE.subn(rf"![\1](/ui/image_vertex/{graphname}/\2)", text)
    if count:
        logger.info(f"Converted {count} tg:// image reference(s) to endpoint URLs")
    return converted


def presign_s3_urls(content, expires_in: int = 3600):
    """Replace ``(s3://bucket/key)`` links with presigned URLs, in a string or
    in the strings of a list or dict. A link that cannot be signed stays."""
    import boto3

    s3 = boto3.client("s3")

    def presign(match):
        bucket, key = match.group(1), match.group(2)
        try:
            url = s3.generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_in
            )
            return f"({url})"
        except Exception as e:
            logger.error(f"Failed to presign S3 url for s3://{bucket}/{key}: {e}")
            return match.group(0)

    def process(value):
        if isinstance(value, str):
            return _S3_URL.sub(presign, value)
        if isinstance(value, list):
            return [process(v) for v in value]
        if isinstance(value, dict):
            return {k: process(v) for k, v in value.items()}
        return value

    return process(content)


def resolve_image_links(text, graphname: str, llm_provider=None):
    """The answer text with image links the chat UI can load: S3 links
    presigned when the chat model runs on AWS Bedrock, and ``tg://`` links
    turned into the image endpoint of the graph."""
    if not isinstance(text, str):
        return text
    if llm_provider is not None and "(s3://" in text:
        from common.llm_services.aws_bedrock_service import AWSBedrock

        if isinstance(llm_provider, AWSBedrock):
            text = presign_s3_urls(text)
    return tg_links_to_endpoints(text, graphname)
