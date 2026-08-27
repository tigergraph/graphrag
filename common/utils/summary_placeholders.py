"""Community-summary placeholder markers, shared by ECC (writes them) and the
graphrag app / Migration Assistant (detects them). Single source of truth so the
health check and the rebuild pipeline agree on what "needs re-summarization"
means.
"""

# Written as a community's description when summarization can't produce a real
# one. Non-empty so the layer-completion check passes and the rebuild finishes,
# and stable so it can be found and regenerated later.
COMMUNITY_SUMMARY_PLACEHOLDER = "[summary unavailable - regenerate]"

# Placeholder written by pre-2.0.1 builds; kept so re-summarization and progress
# checks recognize communities left behind by older graphs too.
LEGACY_SUMMARY_PLACEHOLDER = "Should ignore due to summary error."

# Non-empty markers a description may carry. An empty description is also treated
# as needing re-summarization, but "" is handled separately (GSQL length check).
PLACEHOLDER_MARKERS = [COMMUNITY_SUMMARY_PLACEHOLDER, LEGACY_SUMMARY_PLACEHOLDER]


def is_placeholder_summary(text: str) -> bool:
    """True if a community description is a placeholder needing regeneration:
    the current or legacy sentinel, or empty."""
    t = (text or "").strip()
    return t in ("", COMMUNITY_SUMMARY_PLACEHOLDER, LEGACY_SUMMARY_PLACEHOLDER)
