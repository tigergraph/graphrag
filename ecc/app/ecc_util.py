from common.chunkers import character_chunker, regex_chunker, semantic_chunker, markdown_chunker, recursive_chunker, html_chunker, single_chunker
from common.chunkers.structured import StructuredChunker
from common.chunkers.auto import AutoChunker
from common.config import get_graphrag_config, get_embedding_service

def get_chunker(chunker_type: str = "", graphname: str = None):
    cfg = get_graphrag_config(graphname)
    if not chunker_type:
        chunker_type = cfg.get("chunker", "auto")
    chunker_config = cfg.get("chunker_config", {})
    if chunker_type == "auto":
        # Per-document dispatcher: inspects each document's structure and
        # delegates to the best concrete chunker (structured for markdown/HTML,
        # semantic for unstructured prose). Used when no ctype pins a chunker.
        chunker = AutoChunker(
            factory=lambda kind: get_chunker(kind, graphname=graphname)
        )
    elif chunker_type == "semantic":
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
    elif chunker_type in ("structured", "markdown", "html"):
        # Structure-aware chunker for markdown AND HTML: tables/figures/lists/
        # code stay atomic (never split mid-row), prose char-splits by size.
        # Supersedes MarkdownChunker/HTMLChunker, which split structure blindly.
        chunker = StructuredChunker(
            chunk_size=chunker_config.get("chunk_size", 0),
            overlap_size=chunker_config.get("overlap_size", -1),
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
