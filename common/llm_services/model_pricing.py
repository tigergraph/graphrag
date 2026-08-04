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

"""LiteLLM rate fallback when LangChain ``total_cost`` is 0.

Fetches the catalog once per process (24h TTL) and keeps only the configured
model's rates. No ``litellm`` package dependency.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
_TTL_S = 24 * 60 * 60

# Our ``llm_service`` values -> LiteLLM catalog key prefix.
_PROVIDER_PREFIX = {
    "openai": "openai",
    "azure": "azure",
    "bedrock": "bedrock",
    "genai": "gemini",
    "vertexai": "vertex_ai",
    "groq": "groq",
    "ollama": "ollama",
    "watsonx": "watsonx",
    "huggingface": "huggingface",
    "sagemaker": "sagemaker",
}

_lock = threading.Lock()
# cache_key -> (input, output, cache_read) per-token USD, or None if unknown
_rates: Dict[str, Optional[Tuple[float, float, float]]] = {}
_fetched_at = 0.0


def _cache_key(model: str, provider: str = "") -> str:
    p = (provider or "").strip().lower()
    m = (model or "").strip()
    return f"{p}:{m}" if p else m


def _rates_from_entry(entry: object) -> Optional[Tuple[float, float, float]]:
    if not isinstance(entry, dict):
        return None
    try:
        return (
            float(entry["input_cost_per_token"]),
            float(entry["output_cost_per_token"]),
            float(entry.get("cache_read_input_token_cost") or 0.0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _lookup(
    catalog: dict, model: str, provider: str = ""
) -> Optional[Tuple[float, float, float]]:
    """Resolve rates using configured provider prefix, then bare model id."""
    m = model.strip()
    if not m:
        return None
    m_lower = m.lower()
    prefix = _PROVIDER_PREFIX.get((provider or "").strip().lower())

    keys = []
    if prefix:
        keys.extend((f"{prefix}/{m_lower}", f"{prefix}/{m}"))
    keys.extend((m_lower, m))

    seen = set()
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        rates = _rates_from_entry(catalog.get(key))
        if rates is not None:
            return rates
    return None


def _fetch() -> dict:
    req = urllib.request.Request(_URL, headers={"User-Agent": "graphrag-pricing"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def ensure_model_rates(
    models: Iterable[str],
    *,
    provider: str = "",
    catalog: Optional[dict] = None,
) -> None:
    """Load/cache rates for the given configured model ids only."""
    global _fetched_at
    wanted = [m.strip() for m in models if m and str(m).strip()]
    if not wanted:
        return
    keys = [_cache_key(m, provider) for m in wanted]

    with _lock:
        stale = _fetched_at <= 0 or (time.time() - _fetched_at) > _TTL_S
        if not stale and all(k in _rates for k in keys):
            return

    try:
        data = catalog if catalog is not None else _fetch()
    except Exception as exc:
        logger.warning("LiteLLM pricing fetch failed: %s", exc)
        with _lock:
            for k in keys:
                _rates.setdefault(k, None)
        return

    resolved = {_cache_key(m, provider): _lookup(data, m, provider) for m in wanted}
    with _lock:
        _rates.update(resolved)
        _fetched_at = time.time()


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    provider: str = "",
    cached_input_tokens: int = 0,
) -> Optional[float]:
    if not model:
        return None
    ensure_model_rates([model], provider=provider)
    with _lock:
        rates = _rates.get(_cache_key(model, provider))
    if not rates:
        return None
    inp, out, cache = rates
    cached = max(0, int(cached_input_tokens or 0))
    return (
        max(0, int(input_tokens or 0) - cached) * inp
        + cached * cache
        + max(0, int(output_tokens or 0)) * out
    )


def resolve_usage_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    langchain_cost: float = 0.0,
    *,
    provider: str = "",
    cached_input_tokens: int = 0,
) -> float:
    """LangChain cost if > 0; otherwise LiteLLM estimate for ``model``."""
    lc = float(langchain_cost or 0.0)
    if lc > 0:
        return lc
    return float(
        estimate_cost(
            model,
            input_tokens,
            output_tokens,
            provider=provider,
            cached_input_tokens=cached_input_tokens,
        )
        or 0.0
    )


def clear_pricing_cache() -> None:
    global _fetched_at
    with _lock:
        _rates.clear()
        _fetched_at = 0.0
