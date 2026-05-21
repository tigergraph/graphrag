# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import json
import logging
import os
import re
import threading

from fastapi.security import HTTPBasic

logger = logging.getLogger(__name__)

# Lock for all reads/writes to SERVER_CONFIG to prevent concurrent modifications
# from different endpoints (LLM, DB, GraphRAG config saves) from overwriting each other.
_config_file_lock = threading.Lock()
from pyTigerGraph import TigerGraphConnection

from common.embeddings.embedding_services import (
    AWS_Bedrock_Embedding,
    AzureOpenAI_Ada002,
    OpenAI_Embedding,
    VertexAI_PaLM_Embedding,
    GenAI_Embedding,
    Ollama_Embedding,
)
from common.embeddings.tigergraph_embedding_store import TigerGraphEmbeddingStore
from common.llm_services import (
    AWS_SageMaker_Endpoint,
    AWSBedrock,
    AzureOpenAI,
    GoogleVertexAI,
    GoogleGenAI,
    Groq,
    HuggingFaceEndpoint,
    LLM_Model,
    Ollama,
    OpenAI,
    IBMWatsonX
)
from common.session import SessionHandler
from common.status import StatusManager

security = HTTPBasic()
session_handler = SessionHandler()
status_manager = StatusManager()
service_status = {}

# Configs
SERVER_CONFIG = os.getenv("SERVER_CONFIG", "configs/server_config.json")


_VALID_GRAPHNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_graphname(graphname: str) -> str:
    """Validate graphname to prevent path traversal.

    Raises ValueError if graphname contains path separators or other unsafe characters.
    Returns the graphname unchanged if valid.
    """
    if not graphname:
        return graphname
    if not _VALID_GRAPHNAME_RE.match(graphname):
        raise ValueError(f"Invalid graph name: {graphname!r}")
    return graphname


def _load_graph_config(graphname):
    """Load entire graph-specific server config overrides, or empty dict if none exist."""
    if not graphname:
        return {}
    validate_graphname(graphname)
    graph_path = f"configs/graph_configs/{graphname}/server_config.json"
    if not os.path.exists(graph_path):
        return {}
    with open(graph_path, "r") as f:
        return json.load(f)


def _load_graph_llm_config(graphname):
    """Load graph-specific llm_config overrides, or empty dict if none exist."""
    return _load_graph_config(graphname).get("llm_config", {})


def _resolve_service_config(base_config, override=None):
    """
    Merge a service override on top of a base config (typically completion_service).

    - Starts with base_config as the foundation
    - Overlays override keys on top (if provided)
    - authentication_configuration: override keys take precedence,
      missing keys fall back to base auth
    """
    result = base_config.copy()

    if not override:
        return result

    for key, value in override.items():
        if key == "authentication_configuration":
            continue  # Handle separately below
        result[key] = value

    if "authentication_configuration" in override:
        merged_auth = result.get("authentication_configuration", {}).copy()
        merged_auth.update(override["authentication_configuration"])
        result["authentication_configuration"] = merged_auth
    # else: keep base's auth

    return result


def resolve_llm_services(llm_cfg: dict) -> dict:
    """
    Resolve per-service configs from an llm_config dict.

    Applies the same resolution chain as the get_xxx_config() getters but
    operates on the provided dict instead of the global llm_config. This
    allows both the on-disk config and a candidate config (from UI payload)
    to be resolved with the same logic.

    Resolution:
      1. Inject top-level authentication_configuration into each service
      2. completion_service / embedding_service: used as-is
      3. chat_service / multimodal_service: completion_service base + overrides

    When chat_service or multimodal_service is absent, the resolved config
    falls back to completion_service (inherit).

    Returns dict with keys: completion_service, embedding_service,
    chat_service, multimodal_service — each a fully resolved config.
    """
    # Work on deep copies to avoid mutating the input
    cfg = copy.deepcopy(llm_cfg)

    # Inject top-level auth into service configs (same as reload_llm_config)
    top_auth = cfg.get("authentication_configuration", {})
    if top_auth:
        for svc_key in ["completion_service", "embedding_service", "multimodal_service", "chat_service"]:
            if svc_key in cfg:
                svc = cfg[svc_key]
                if "authentication_configuration" not in svc:
                    svc["authentication_configuration"] = top_auth.copy()
                else:
                    merged = top_auth.copy()
                    merged.update(svc["authentication_configuration"])
                    svc["authentication_configuration"] = merged

    # Inject top-level region_name into service configs if missing
    top_region = cfg.get("region_name")
    if top_region:
        for svc_key in ["completion_service", "embedding_service", "multimodal_service", "chat_service"]:
            if svc_key in cfg and "region_name" not in cfg[svc_key]:
                cfg[svc_key]["region_name"] = top_region

    # Inject top-level prompt_path into LLM-prompted service configs
    # if missing. The UI never lets users set per-service prompt_paths;
    # in practice they are always identical to completion's.
    # ``embedding_service`` is excluded — embedding models never load
    # prompt files (their class hierarchy has no prompt-property machinery).
    top_prompt_path = cfg.get("prompt_path")
    if top_prompt_path:
        for svc_key in ["completion_service", "multimodal_service", "chat_service"]:
            if svc_key in cfg and "prompt_path" not in cfg[svc_key]:
                cfg[svc_key]["prompt_path"] = top_prompt_path

    completion = cfg.get("completion_service", {})

    # Resolve embedding: inherit provider-level config from completion
    # when the embedding provider matches the completion provider.
    # (embedding has a different schema — model_name vs llm_model —
    # so we only inherit shared provider fields like region_name.)
    embedding = cfg.get("embedding_service", {}).copy()
    embedding_provider = embedding.get("embedding_model_service", "").lower()
    completion_provider = completion.get("llm_service", "").lower()
    if embedding_provider and embedding_provider == completion_provider:
        # Identity/schema keys that belong to the embedding service itself
        embedding_own_keys = {"embedding_model_service", "model_name", "authentication_configuration", "token_limit"}
        for k, v in completion.items():
            if k not in embedding_own_keys and k not in embedding:
                embedding[k] = v

    return {
        "completion_service": completion.copy(),
        "embedding_service": embedding,
        "chat_service": _resolve_service_config(completion, cfg.get("chat_service")),
        "multimodal_service": _resolve_service_config(completion, cfg.get("multimodal_service")),
    }


def get_completion_config(graphname=None):
    """
    Return completion_service config for the given graph.

    Resolution: merge graph-specific completion_service overrides on top of
    global completion_service. Graph configs only store overrides, so unchanged
    fields always inherit the latest global values.
    """
    graph_llm = _load_graph_llm_config(graphname)
    override = graph_llm.get("completion_service")
    if override:
        logger.debug(f"[get_completion_config] graph={graphname} using graph-specific overrides")
    result = _resolve_service_config(llm_config["completion_service"], override)

    if graphname:
        result["graphname"] = graphname

    return result


def get_embedding_config(graphname=None):
    """
    Return embedding_service config for the given graph.

    Resolution: merge graph-specific embedding_service overrides on top of
    global embedding_service. Graph configs only store overrides, so unchanged
    fields always inherit the latest global values.
    """
    graph_llm = _load_graph_llm_config(graphname)
    override = graph_llm.get("embedding_service")
    if override:
        logger.debug(f"[get_embedding_config] graph={graphname} using graph-specific overrides")
    result = _resolve_service_config(llm_config["embedding_service"], override)

    if graphname:
        result["graphname"] = graphname

    return result


def get_chat_config(graphname=None):
    """
    Return the chatbot LLM config for the given graph.

    Resolution chain:
      1. Start with global completion_service
      2. Merge graph-specific completion_service overrides (shared base for all services)
      3. Merge chat_service overrides (graph-specific > global > none)

    This ensures graph-level completion_service changes (e.g. prompt_path)
    propagate to the chatbot config as well.
    """
    graph_llm = _load_graph_llm_config(graphname)

    # Build per-graph base: global completion + graph completion overrides
    base = _resolve_service_config(
        llm_config["completion_service"],
        graph_llm.get("completion_service"),
    )

    # Find chat override: graph-specific > global > None
    chat_override = graph_llm.get("chat_service")
    if chat_override:
        logger.debug(f"[get_chat_config] graph={graphname} using graph-specific chat_service")
    elif "chat_service" in llm_config:
        chat_override = llm_config["chat_service"]
        logger.debug(f"[get_chat_config] graph={graphname} using global chat_service")
    else:
        logger.debug(f"[get_chat_config] graph={graphname} falling back to completion_service")

    result = _resolve_service_config(base, chat_override)

    if graphname:
        result["graphname"] = graphname

    return result


def get_multimodal_config(graphname=None):
    """
    Return the multimodal/vision config for the given graph.

    Resolution chain:
      1. Start with global completion_service
      2. Merge graph-specific completion_service overrides (shared base)
      3. Merge multimodal_service overrides (graph-specific > global)

    When no multimodal_service override exists ("inherit"), the completion
    config is returned as-is — the completion model is used for vision.
    """
    graph_llm = _load_graph_llm_config(graphname)

    # Build per-graph base: global completion + graph completion overrides
    base = _resolve_service_config(
        llm_config["completion_service"],
        graph_llm.get("completion_service"),
    )

    # Find multimodal override: graph-specific > global > None (inherit)
    mm_override = graph_llm.get("multimodal_service")
    if mm_override is None and "multimodal_service" in llm_config:
        mm_override = llm_config["multimodal_service"]

    return _resolve_service_config(base, mm_override)


def get_graphrag_config(graphname=None):
    """
    Return graphrag_config for the given graph.

    Resolution: merge graph-specific graphrag_config overrides on top of
    global graphrag_config. Graph configs only store overrides, so unchanged
    fields always inherit the latest global values.
    """
    graph_cfg = _load_graph_config(graphname)
    override = graph_cfg.get("graphrag_config")
    if not override:
        return graphrag_config
    # Merge: global as base, graph overrides on top (simple dict merge, no auth logic)
    result = graphrag_config.copy()
    result.update(override)
    return result


PATH_PREFIX = os.getenv("PATH_PREFIX", "")
PRODUCTION = os.getenv("PRODUCTION", "false").lower() == "true"

if not PATH_PREFIX.startswith("/") and len(PATH_PREFIX) != 0:
    PATH_PREFIX = f"/{PATH_PREFIX}"
if PATH_PREFIX.endswith("/"):
    PATH_PREFIX = PATH_PREFIX[:-1]

if SERVER_CONFIG is None:
    raise Exception("SERVER_CONFIG environment variable not set")

if SERVER_CONFIG[-5:] != ".json":
    try:
        server_config = json.loads(str(SERVER_CONFIG))
    except Exception as e:
        raise Exception(
            "SERVER_CONFIG environment variable must be a .json file or a JSON string, failed with error: "
            + str(e)
        )
else:
    with open(SERVER_CONFIG, "r") as f:
        server_config = json.load(f)

db_config = server_config.get("db_config")
llm_config = server_config.get("llm_config")
graphrag_config = server_config.get("graphrag_config")

if db_config is None:
    raise Exception("db_config is not found in SERVER_CONFIG")
if llm_config is None:
    raise Exception("llm_config is not found in SERVER_CONFIG")

# Inject authentication_configuration into service configs so they have everything they need.
# Rule: service-level (lower) auth keys take precedence; missing keys fall back to top-level (upper).
if "authentication_configuration" in llm_config:
    for svc_key in ["completion_service", "embedding_service", "multimodal_service", "chat_service"]:
        if svc_key in llm_config:
            svc = llm_config[svc_key]
            if "authentication_configuration" not in svc:
                svc["authentication_configuration"] = llm_config["authentication_configuration"].copy()
            else:
                # Merge: top-level as base, service-level on top (service-level wins)
                merged = llm_config["authentication_configuration"].copy()
                merged.update(svc["authentication_configuration"])
                svc["authentication_configuration"] = merged

# Inject top-level region_name into service configs if missing
if "region_name" in llm_config:
    for svc_key in ["completion_service", "embedding_service", "multimodal_service", "chat_service"]:
        if svc_key in llm_config and "region_name" not in llm_config[svc_key]:
            llm_config[svc_key]["region_name"] = llm_config["region_name"]

# Inject top-level prompt_path into LLM-prompted service configs if
# missing. Embedding service is excluded — embedding models never load
# prompt files. Per-service entries on disk are accepted for backward
# compat but never written by the UI.
if "prompt_path" in llm_config:
    for svc_key in ["completion_service", "multimodal_service", "chat_service"]:
        if svc_key in llm_config and "prompt_path" not in llm_config[svc_key]:
            llm_config[svc_key]["prompt_path"] = llm_config["prompt_path"]

_comp = llm_config.get("completion_service")
if _comp is None:
    raise Exception("completion_service is not found in llm_config")
if "llm_service" not in _comp:
    raise Exception("llm_service is not found in completion_service")
if "llm_model" not in _comp:
    raise Exception("llm_model is not found in completion_service")

# Log which model will be used for chatbot and ECC/GraphRAG
if "chat_service" in llm_config:
    chat_svc = llm_config["chat_service"]
    logger.info(f"[CHATBOT] Using chat_service: {chat_svc.get('llm_model', 'N/A')} (Provider: {chat_svc.get('llm_service', _comp['llm_service'])})")
    logger.info(f"[ECC] Using completion_service: {_comp['llm_model']} (Provider: {_comp['llm_service']})")
else:
    logger.info(f"[CHATBOT] Using completion_service llm_model: {_comp['llm_model']} (Provider: {_comp['llm_service']})")
    logger.info(f"[ECC] Using completion_service: {_comp['llm_model']} (Provider: {_comp['llm_service']})")

_emb = llm_config.get("embedding_service")
if _emb is None:
    raise Exception("embedding_service is not found in llm_config")
if "embedding_model_service" not in _emb:
    raise Exception("embedding_model_service is not found in embedding_service")
if "model_name" not in _emb:
    raise Exception("model_name is not found in embedding_service")
embedding_dimension = _emb.get("dimensions", 1536)

# Log which embedding model will be used
logger.info(f"[EMBEDDING] Using model: {_emb.get('model_name', 'N/A')} (Provider: {_emb.get('embedding_model_service', 'N/A')})")

# Get context window size from llm_config
# <=0 means unlimited tokens (no truncation), otherwise use the specified limit
if "token_limit" in llm_config:
    if "token_limit" not in _comp:
        _comp["token_limit"] = llm_config["token_limit"]
    if "token_limit" not in _emb:
        _emb["token_limit"] = llm_config["token_limit"]

# Log multimodal_service config (optional, for vision/image tasks).
_mm_config = get_multimodal_config()
if _mm_config:
    logger.info(f"[MULTIMODAL] Using model: {_mm_config.get('llm_model', 'N/A')} (Provider: {_mm_config.get('llm_service', 'N/A')})")

if graphrag_config is None:
    graphrag_config = {"reuse_embedding": True}
if "chunker" not in graphrag_config:
    graphrag_config["chunker"] = "semantic"
if "extractor" not in graphrag_config:
    graphrag_config["extractor"] = "llm"
if "tg_memory_schema_on_startup" not in graphrag_config:
    graphrag_config["tg_memory_schema_on_startup"] = True
# ``retrieval_include_entity`` is resolved at install time
# (see ``common.db.retriever_render.resolve_include_entity``).

reuse_embedding = graphrag_config.get("reuse_embedding", True)
doc_process_switch = graphrag_config.get("doc_process_switch", True)
entity_extraction_switch = graphrag_config.get("entity_extraction_switch", doc_process_switch)
community_detection_switch = graphrag_config.get("community_detection_switch", entity_extraction_switch)

if "model_name" not in llm_config or "model_name" not in llm_config["embedding_service"]:
    if "model_name" not in llm_config:
        llm_config["model_name"] = llm_config["embedding_service"]["model_name"]
    else:
        llm_config["embedding_service"]["model_name"] = llm_config["model_name"]

if llm_config["embedding_service"]["embedding_model_service"].lower() == "openai":
    embedding_service = OpenAI_Embedding(llm_config["embedding_service"])
elif llm_config["embedding_service"]["embedding_model_service"].lower() == "azure":
    embedding_service = AzureOpenAI_Ada002(llm_config["embedding_service"])
elif llm_config["embedding_service"]["embedding_model_service"].lower() == "vertexai":
    embedding_service = VertexAI_PaLM_Embedding(llm_config["embedding_service"])
elif llm_config["embedding_service"]["embedding_model_service"].lower() == "genai":
    embedding_service = GenAI_Embedding(llm_config["embedding_service"])
elif llm_config["embedding_service"]["embedding_model_service"].lower() == "bedrock":
    embedding_service = AWS_Bedrock_Embedding(llm_config["embedding_service"])
elif llm_config["embedding_service"]["embedding_model_service"].lower() == "ollama":
    embedding_service = Ollama_Embedding(llm_config["embedding_service"])
else:
    raise Exception("Embedding service not implemented")

def get_embedding_service():
    """Return the current embedding service instance.

    Use this instead of importing ``embedding_service`` directly so
    consumers always read the latest instance after a config reload.
    """
    return embedding_service


def get_llm_service(service_config: dict) -> LLM_Model:
    """
    Instantiate an LLM provider from a flat service config dict.

    The config must contain ``llm_service`` at the top level.
    Use ``get_completion_config()`` or ``get_chat_config()`` to obtain
    the appropriate config for ECC or chatbot callers respectively.
    """
    service_name = service_config["llm_service"].lower()
    if service_name == "openai":
        return OpenAI(service_config)
    elif service_name == "azure":
        return AzureOpenAI(service_config)
    elif service_name == "sagemaker":
        return AWS_SageMaker_Endpoint(service_config)
    elif service_name == "vertexai":
        return GoogleVertexAI(service_config)
    elif service_name == "genai":
        return GoogleGenAI(service_config)
    elif service_name == "bedrock":
        return AWSBedrock(service_config)
    elif service_name == "groq":
        return Groq(service_config)
    elif service_name == "ollama":
        return Ollama(service_config)
    elif service_name == "huggingface":
        return HuggingFaceEndpoint(service_config)
    elif service_name == "watsonx":
        return IBMWatsonX(service_config)
    else:
        raise Exception(f"LLM service '{service_name}' not supported")


# Module-level ``embedding_store`` is the back-compat default for
# direct importers (``from common.config import embedding_store``).
# It's populated by the background init thread below.
#
# ``_embedding_stores`` is the per-graph cache used by chatbot
# retrievers via ``get_embedding_store(graphname=...)``. Each entry
# has its own ``TigerGraphConnection`` bound to that graphname for
# its lifetime — no in-place ``set_graphname`` mutation — so
# concurrent chat across different graphs can't race over a shared
# connection.
embedding_store = None
_embedding_store_ready = threading.Event()
_embedding_stores: dict = {}
_embedding_stores_lock = threading.Lock()
service_status["embedding_store"] = {
    "status": "initializing",
    "error": "Embedding store is still initializing",
}


def _build_embedding_store(graphname: str = "") -> TigerGraphEmbeddingStore:
    """Construct a fresh ``TigerGraphEmbeddingStore`` bound to *graphname*.

    Uses the live globals (``db_config`` for the connection and
    ``embedding_service`` for the model) so the result reflects the
    current config.
    """
    conn = TigerGraphConnection(
        host=db_config.get("hostname", "http://tigergraph"),
        username=db_config.get("username", "tigergraph"),
        password=db_config.get("password", "tigergraph"),
        gsPort=db_config.get("gsPort", "14240"),
        restppPort=db_config.get("restppPort", "9000"),
        graphname=graphname or db_config.get("graphname", ""),
        apiToken=db_config.get("apiToken", ""),
    )
    if not db_config.get("apiToken") and db_config.get("getToken"):
        conn.getToken()

    store = TigerGraphEmbeddingStore(
        conn,
        embedding_service,
        support_ai_instance=True,
    )
    if graphname:
        # Runs the GDS check and per-graph vector-query install.
        store.set_graphname(graphname)
    return store


def _init_embedding_store():
    """Background thread target. Builds the default embedding store
    without blocking module import — TigerGraph may be slow on first
    connect, and we don't want app startup to wait on it.
    """
    global embedding_store
    try:
        embedding_store = _build_embedding_store()
        service_status["embedding_store"] = {"status": "ok", "error": None}
    except Exception as e:
        service_status["embedding_store"] = {"status": "error", "error": str(e)}
        logger.error(f"Failed to initialize embedding store: {e}")
    finally:
        _embedding_store_ready.set()


def get_embedding_store(graphname: str | None = None, timeout: float = 0):
    """Return an embedding store.

    Args:
        graphname: When supplied, returns a per-graph instance built
            and cached on first request (each cache entry has its own
            connection bound to *graphname* for its lifetime).
        timeout: Seconds to wait for the default-store init when
            *graphname* is not supplied. Default 0 (non-blocking —
            raises immediately if still initializing).

    Raises:
        RuntimeError: if not yet ready, timed out, or initialization failed.
    """
    if graphname:
        with _embedding_stores_lock:
            cached = _embedding_stores.get(graphname)
            if cached is not None:
                return cached
        # Build outside the lock so first-time setup for one graph
        # doesn't serialize first-time setup for another.
        store = _build_embedding_store(graphname)
        with _embedding_stores_lock:
            existing = _embedding_stores.get(graphname)
            if existing is not None:
                return existing  # racing thread won
            _embedding_stores[graphname] = store
            return store

    if not _embedding_store_ready.wait(timeout=timeout):
        raise RuntimeError(
            "Embedding store is still initializing. Please try again shortly."
        )
    if embedding_store is None:
        error = service_status.get("embedding_store", {}).get("error", "Unknown error")
        raise RuntimeError(f"Embedding store failed to initialize: {error}")
    return embedding_store


def reset_embedding_store() -> None:
    """Drop the per-graph cache and the default store, then re-run the
    background init so a config reload picks up the new
    ``embedding_service`` and ``db_config``. Callers should swap the
    inputs before calling. No-op when ``INIT_EMBED_STORE`` is disabled
    (e.g. ECC).
    """
    global embedding_store
    if os.getenv("INIT_EMBED_STORE", "true") != "true":
        return
    with _embedding_stores_lock:
        _embedding_stores.clear()
    embedding_store = None
    _embedding_store_ready.clear()
    service_status["embedding_store"] = {
        "status": "initializing",
        "error": "Embedding store is still initializing",
    }
    threading.Thread(target=_init_embedding_store, daemon=True).start()


if os.getenv("INIT_EMBED_STORE", "true") == "true":
    threading.Thread(target=_init_embedding_store, daemon=True).start()


def reload_llm_config(new_llm_config: dict = None):
    """
    Reload LLM configuration and reinitialize services.
    
    Args:
        new_llm_config: If provided, saves this config to file first. 
                       If None, just reloads from existing file.
    
    Returns:
        dict: Status of reload operation
    """
    global llm_config, embedding_service

    try:
        with _config_file_lock:
            # If new config provided, save it first
            if new_llm_config is not None:
                with open(SERVER_CONFIG, "r") as f:
                    server_config = json.load(f)

                server_config["llm_config"] = new_llm_config

                temp_file = f"{SERVER_CONFIG}.tmp"
                with open(temp_file, "w") as f:
                    json.dump(server_config, f, indent=2)
                os.replace(temp_file, SERVER_CONFIG)

            # Read/reload from file
            with open(SERVER_CONFIG, "r") as f:
                server_config = json.load(f)

        # Validate before updating
        new_llm_config = server_config.get("llm_config")
        if new_llm_config is None:
            raise Exception("llm_config is not found in SERVER_CONFIG")

        # Inject authentication_configuration into service configs BEFORE updating globals.
        # Rule: service-level (lower) auth keys take precedence; missing keys fall back to top-level (upper).
        if "authentication_configuration" in new_llm_config:
            for svc_key in ["completion_service", "embedding_service", "multimodal_service", "chat_service"]:
                if svc_key in new_llm_config:
                    svc = new_llm_config[svc_key]
                    if "authentication_configuration" not in svc:
                        svc["authentication_configuration"] = new_llm_config["authentication_configuration"].copy()
                    else:
                        merged = new_llm_config["authentication_configuration"].copy()
                        merged.update(svc["authentication_configuration"])
                        svc["authentication_configuration"] = merged

        # Inject top-level region_name into service configs if missing
        if "region_name" in new_llm_config:
            for svc_key in ["completion_service", "embedding_service", "multimodal_service", "chat_service"]:
                if svc_key in new_llm_config and "region_name" not in new_llm_config[svc_key]:
                    new_llm_config[svc_key]["region_name"] = new_llm_config["region_name"]

        # Inject top-level prompt_path into LLM-prompted service configs
        # if missing. Embedding service is excluded — embedding models
        # never load prompt files.
        if "prompt_path" in new_llm_config:
            for svc_key in ["completion_service", "multimodal_service", "chat_service"]:
                if svc_key in new_llm_config and "prompt_path" not in new_llm_config[svc_key]:
                    new_llm_config[svc_key]["prompt_path"] = new_llm_config["prompt_path"]

        new_completion_config = new_llm_config.get("completion_service")
        new_embedding_config = new_llm_config.get("embedding_service")

        if new_completion_config is None:
            raise Exception("completion_service is not found in llm_config")
        if new_embedding_config is None:
            raise Exception("embedding_service is not found in llm_config")

        # Validate required fields before touching globals
        if "llm_service" not in new_completion_config:
            raise Exception("llm_service is not found in completion_service")
        if "llm_model" not in new_completion_config:
            raise Exception("llm_model is not found in completion_service")

        # Propagate top-level token_limit into service configs (same as startup)
        if "token_limit" in new_llm_config:
            if "token_limit" not in new_completion_config:
                new_completion_config["token_limit"] = new_llm_config["token_limit"]
            if "token_limit" not in new_embedding_config:
                new_embedding_config["token_limit"] = new_llm_config["token_limit"]

        # Update globals atomically: build complete new state, then swap in one step.
        # Using dict slice assignment avoids the clear()+update() window where readers
        # would see an empty dict.
        old_llm_keys = set(llm_config.keys())
        for k in old_llm_keys - set(new_llm_config.keys()):
            del llm_config[k]
        llm_config.update(new_llm_config)

        # Re-initialize embedding service
        if new_embedding_config["embedding_model_service"].lower() == "openai":
            embedding_service = OpenAI_Embedding(new_embedding_config)
        elif new_embedding_config["embedding_model_service"].lower() == "azure":
            embedding_service = AzureOpenAI_Ada002(new_embedding_config)
        elif new_embedding_config["embedding_model_service"].lower() == "vertexai":
            embedding_service = VertexAI_PaLM_Embedding(new_embedding_config)
        elif new_embedding_config["embedding_model_service"].lower() == "genai":
            embedding_service = GenAI_Embedding(new_embedding_config)
        elif new_embedding_config["embedding_model_service"].lower() == "bedrock":
            embedding_service = AWS_Bedrock_Embedding(new_embedding_config)
        elif new_embedding_config["embedding_model_service"].lower() == "ollama":
            embedding_service = Ollama_Embedding(new_embedding_config)
        else:
            raise Exception("Embedding service not implemented")

        # Clear per-graph cache + rebuild the default so callers don't
        # keep references to the old embedding service.
        reset_embedding_store()

        return {
            "status": "success",
            "message": "LLM configuration reloaded successfully"
        }

    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to reload LLM config: {str(e)}"
        }


def reload_db_config(new_db_config: dict = None):
    """
    Reload DB configuration from server_config.json and update in-memory config.
    
    Args:
        new_db_config: If provided, saves this config to file first.
                       If None, just reloads from existing file.
    
    Returns:
        dict: Status of reload operation
    """
    global db_config

    try:
        with _config_file_lock:
            if new_db_config is not None:
                with open(SERVER_CONFIG, "r") as f:
                    server_config = json.load(f)

                server_config["db_config"] = new_db_config

                temp_file = f"{SERVER_CONFIG}.tmp"
                with open(temp_file, "w") as f:
                    json.dump(server_config, f, indent=2)
                os.replace(temp_file, SERVER_CONFIG)

            with open(SERVER_CONFIG, "r") as f:
                server_config = json.load(f)

        new_db_config = server_config.get("db_config")
        if new_db_config is None:
            raise Exception("db_config is not found in SERVER_CONFIG")

        old_db_keys = set(db_config.keys())
        for k in old_db_keys - set(new_db_config.keys()):
            del db_config[k]
        db_config.update(new_db_config)

        # Clear per-graph cache + rebuild the default so callers don't
        # keep connections bound to the old credentials.
        reset_embedding_store()

        return {
            "status": "success",
            "message": "DB configuration reloaded successfully"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to reload DB config: {str(e)}"
        }


def reload_graphrag_config():
    """
    Reload GraphRAG configuration from server_config.json.
    Updates the in-memory graphrag_config dict to reflect changes immediately.
    
    Returns:
        dict: Status of reload operation
    """
    global graphrag_config

    try:
        with _config_file_lock:
            with open(SERVER_CONFIG, "r") as f:
                server_config = json.load(f)

        new_graphrag_config = server_config.get("graphrag_config")
        if new_graphrag_config is None:
            new_graphrag_config = {"reuse_embedding": True}
        
        # Set defaults (same as startup logic)
        if "chunker" not in new_graphrag_config:
            new_graphrag_config["chunker"] = "semantic"
        if "extractor" not in new_graphrag_config:
            new_graphrag_config["extractor"] = "llm"
        
        # Update graphrag_config in-place to preserve references in other modules
        old_graphrag_keys = set(graphrag_config.keys())
        for k in old_graphrag_keys - set(new_graphrag_config.keys()):
            del graphrag_config[k]
        graphrag_config.update(new_graphrag_config)
        
        logger.info(f"GraphRAG config reloaded: extractor={graphrag_config.get('extractor')}, chunker={graphrag_config.get('chunker')}, reuse_embedding={graphrag_config.get('reuse_embedding')}")
        
        return {
            "status": "success",
            "message": "GraphRAG configuration reloaded successfully"
        }
        
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to reload GraphRAG config: {str(e)}"
        }