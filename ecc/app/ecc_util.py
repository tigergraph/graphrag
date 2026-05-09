from common.chunkers import character_chunker, regex_chunker, semantic_chunker, markdown_chunker, recursive_chunker, html_chunker, single_chunker
from common.config import get_graphrag_config, get_embedding_service

def get_chunker(chunker_type: str = "", graphname: str = None):
    cfg = get_graphrag_config(graphname)
    if not chunker_type:
        chunker_type = cfg.get("chunker", "semantic")
    chunker_config = cfg.get("chunker_config", {})
    if chunker_type == "semantic":
        chunker = semantic_chunker.SemanticChunker(
            get_embedding_service(),
            chunker_config.get("method", "percentile"),
            chunker_config.get("threshold", 0.95),
        )
    elif chunker_type == "regex":
        chunker = regex_chunker.RegexChunker(
            pattern=chunker_config.get("pattern", "\\r?\\n")
        )
    elif chunker_type == "character":
        chunker = character_chunker.CharacterChunker(
            chunk_size=chunker_config.get("chunk_size", 0),
            overlap_size=chunker_config.get("overlap_size", -1),
        )
    elif chunker_type == "markdown":
        chunker = markdown_chunker.MarkdownChunker(
            chunk_size=chunker_config.get("chunk_size", 0),
            overlap_size=chunker_config.get("overlap_size", -1),
        )
    elif chunker_type == "html":
        chunker = html_chunker.HTMLChunker(
            chunk_size=chunker_config.get("chunk_size", 0),
            overlap_size=chunker_config.get("overlap_size", -1),
            headers=chunker_config.get("headers", None),
        )
    elif chunker_type == "recursive":
        chunker = recursive_chunker.RecursiveChunker(
            chunk_size=chunker_config.get("chunk_size", 0),
            overlap_size=chunker_config.get("overlap_size", -1),
        )
    elif chunker_type == "single" or chunker_type == "image":
        # Single chunker: NEVER splits, always returns 1 chunk
        # Used for images to preserve markdown image references
        chunker = single_chunker.SingleChunker()
    else:
        raise ValueError(f"Invalid chunker type: {chunker_type}")

    return chunker
