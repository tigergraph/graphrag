# Changelog

## [1.4.0]

### Added
- **Auto retrieval method selection** — new "Auto" option in the chat dropdown picks among Similarity / Contextual / Hybrid / Community per question
  - Two-stage selector: deterministic regex rules cover common cases; LLM fallback handles the rest with a subset-aware prompt
  - Selection visible via a chip below each bot reply (method, reason, auto/manual)
  - Manual method selection still works as override during the transition
- **Method selection telemetry** — Prometheus counter `llm_method_selection_total` with `selected_method` and `selection_source` labels
- **Out-of-corpus short-circuit** — when the chosen retriever returns no results, the system returns an honest "couldn't find relevant info" message instead of letting the LLM hallucinate from empty context

## [1.3.1]

### Changed
- Upgraded `pyTigerGraph` dependency to `>=2.0.3`
- Improved ingestion statistics: loading job results now parsed for accurate document counts and rejected line tracking
- Clarified file preparation log message to distinguish JSONL copies from converted files

### Fixed
- **WebSocket chat endpoint no longer crashes on early client disconnect**
  - `WebSocketDisconnect` caught separately during auth and conversation ID phases
  - Prevents `ASGI application` error when client closes before sending credentials
- **Loading jobs auto-recreated before ingestion** if missing (e.g., after schema drop or reinitialization)
  - Checks for required loading job before JSONL ingestion loop
  - Recreates from GSQL template if not found; fails with clear error if recreation fails

## [1.3.0]

### Added
- **Admin configuration UI** with role-based access for DB, LLM, and GraphRAG settings
  - Separate pages for DB config, LLM provider config, and GraphRAG config
  - Graph admin role restriction via `ConfigScopeToggle`
  - `apiToken` auth option added to GraphDB config with conditional UI
- **Per-graph chatbot LLM override** (`chat_service` in `llm_config`) with inheritance from `completion_service`
  - Missing keys fall back to `completion_service` automatically
  - Graph admins can configure per graph via the UI
- **Secret masking** in configuration API responses
  - GET responses return masked values; backend substitutes on save/test
  - Credentials never reach the frontend
- **Session idle timeout** (1 hour) that auto-clears the session on inactivity
  - Session data moved from `localStorage` to `sessionStorage`; theme stays in `localStorage`
  - Timer pauses during long-running operations (ingest, rebuild)
- **Auth guard** on all UI routes
  - `RequireAuth` wrapper redirects unauthenticated users to login
  - SPA routing with `serve -s` and catch-all route
- **GraphRAG config UI fields**
  - Search parameters: `top_k`, `num_hops`, `num_seen_min`, `community_level`, `doc_only`
  - Advanced ingestion settings: `load_batch_size`, `upsert_delay`, `default_concurrency`
  - All chunker settings (chunk_size, overlap_size, method, threshold, pattern) shown and saved regardless of selected chunker
- **Multimodal inherit checkbox** in LLM config UI
  - "Use same model as completion service" option in both single and multi-provider modes
  - Amber warning when inheriting: "Ensure your completion model supports vision input"
- **`get_embedding_config()`** getter in `common/config.py` for parity with other service getters
- **Greeting detection** in agent router
  - Regex-based pattern matching for common greetings, farewells, and thanks
  - Responds directly without invoking query generation or search
- **Centralized LLM token usage tracking**
  - All LLM call sites (15+) migrated to `invoke_with_parser` / `ainvoke_with_parser`
  - Supports both structured (JSON) and plain text LLM responses
- **JSON parsing fallback** for LLM responses
  - Handles responses wrapped in preamble text or markdown code fences
  - Entity extraction uses a 3-tier fallback: direct parse, code fence extraction, regex extraction
- **Cypher/GSQL output validation** before query execution
  - Checks for required query keywords before wrapping in `INTERPRET OPENCYPHER QUERY`
  - Invalid output raises an error and retries instead of executing garbage queries
- **Retriever scoring** for all retriever types when `combine=False`
  - Scoring logic lifted from `CommunityRetriever` into `BaseRetriever`
  - Similarity, Hybrid, and Sibling retrievers now score and rank context chunks
- **User-customized prompts** persisted under `configs/` across container restarts
- **Unit tests** for LLM invocation and JSON parsing (13 test cases)

### Changed
- **All config consumers use `get_xxx_config(graphname)` getters** instead of direct `llm_config` access
  - `root.py`, `report-service/root.py`, `ecc/main.py`, `ui.py` migrated
  - Test connection and save endpoints use `_build_test_config()` overlay pattern
  - `_unmask_auth` resolves credentials via getters for correct per-graph resolution
- **Multimodal service inherits completion model directly** when not explicitly configured
  - Removed hardcoded `DEFAULT_MULTIMODAL_MODELS` that silently substituted different models
- **LLM config UI improvements**
  - Red asterisk markers on mandatory model name fields
  - Shared `LLM_PROVIDERS` constant replaces duplicate provider lists
  - State synced when toggling between single/multi-provider modes
  - Reordered sections: Completion → Chatbot → Multimodal → Embedding
- Config file writes are now atomic with file locking to prevent race conditions
  - `_config_file_lock` prevents concurrent overwrites
  - In-memory config updates use atomic dict replacement instead of clear-and-update
- Chat history messages display instantly without typewriter animation
  - History messages tagged with `response_type: "history"` to skip CSS animation
- Chatbot model selection uses `chat_service` config with `completion_service` fallback
  - Community summarization prompt loaded at call time instead of import time
- README config documentation updated for clarity and consistency
  - Parameter descriptions focus on purpose, not implementation details
  - `token_limit`, `default_concurrency`, and other parameters reworded
  - `multimodal_service` defaults corrected to show inheritance from `completion_service`
- `default_concurrency` replaces `tg_concurrency` in `graphrag_config`
  - Configurable per graph
- Wired up `default_mem_threshold` and `default_thread_limit` in database connection proxy

### Fixed
- **Bedrock multimodal connection test** — 1x1 test PNG rejected by Bedrock image validation; replaced with 20x20 PNG
- **Provider-aware image format** in multimodal test and `image_data_extractor`
  - GenAI/VertexAI require `image_url` format; Bedrock/Anthropic use `type:"image"` with source block
- **report-service/root.py** — `llm_config` used but never imported (NameError on health endpoint)
- **Null service values** stripped before config reload (null = inherit, key should be absent)
- Login page shows proper error messages based on HTTP status
  - 401/403: "Invalid credentials"; other errors: "Server error (N)"; network failure: "Unable to connect"
- SPA routing fixed with catch-all route to login page
- Rebuild dialog button no longer flickers between status labels
  - Polling stops once rebuild completes; final status message preserved
- Idle timer pauses during long-running operations (ingest, rebuild)
  - Uses pause/resume instead of repeated signal activity calls
- Bedrock model names no longer trigger token calculator warnings
  - Provider prefix and version suffix stripped before tiktoken lookup
- Config reload no longer clears in-memory state during concurrent requests
- Startup validation restored for `llm_service` and `llm_model`
- `HTTPException` properly re-raised in config and DB test endpoints
