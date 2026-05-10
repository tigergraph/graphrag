# Changelog

## [1.4.0]

### Added
- **Schema-aware initialization** at *Initialize Knowledge Graph* time, with three modes: skip schema, generate a draft from sample documents, or paste a GSQL schema. Drafts are reviewed in a form-mode editor before being applied as a single atomic schema-change job that never drops existing types.
- **Schema-aware extraction**: when an extracted entity or relationship matches a declared domain type or pair, ECC populates the domain vertex / edge directly. A configurable strict mode drops non-schema extractions instead of falling back to the raw `Entity` layer.
- **Typed-relationship metadata layer** (`EntityType` / `RelationshipType` vertices linked by `IS_HEAD_OF` / `HAS_TAIL`) carrying type names and human-readable definitions; available to retrievers and to the chat agent. The layer auto-fills from extracted free-text types when no domain types are declared (with case / suffix / plural deduplication), and is restricted to declared types only when a domain schema exists.
- **Customizable schema-extraction prompt** in the `/prompts` API alongside the existing chatbot, entity-relationship, community-summarization, and query-generation prompts. Per-graph overrides supported.
- **Schema definitions threaded into LLM prompts** for query generation (Cypher / GSQL) and entity-relationship extraction, so the model sees per-type descriptions alongside the schema rep.
- **JSONL caching shared between schema extraction and ingest** — files uploaded for schema extraction are reused by the ingest flow without re-conversion.
- **Parallel image description** during PDF processing (default 8 workers, env-overridable).
- **Async embedding-store initialization** — service startup no longer blocks on the TigerGraph connection; status surfaces as `initializing` / `ok` / `error`.
- **Auto retrieval method selection** — new "Auto" option in the chat dropdown picks among Similarity / Contextual / Hybrid / Community per question
  - Two-stage selector: deterministic regex rules cover common cases; LLM fallback handles the rest with a subset-aware prompt
  - Selection visible via a chip below each bot reply (method icon + label; reason and source in hover tooltip)
  - Manual method selection still works as override during the transition
- **Method selection telemetry** — Prometheus counter `llm_method_selection_total` with `selected_method` and `selection_source` labels
- **Out-of-corpus short-circuit** — when the chosen retriever returns no results, the system returns an honest "couldn't find relevant info" message instead of letting the LLM hallucinate from empty context
- **In-lane retrieval fallback** — when a chunk-based search method (similarity / contextual / hybrid) returns fewer than `top_k` chunks, the system tries a second method via a subset-aware fallback table (similarity → hybrid, contextual → hybrid, hybrid → community). Single retry, skipped for manual mode and community search.
- **Cross-lane fallback to vector search** — when `generate_function` or `generate_cypher` retries are exhausted (3 rewrite cycles), the system falls back to auto-selected vector search instead of going straight to the apology message. Forces auto-selection regardless of configured method, so even manual users get the best vector option in this recovery path. Toggleable per-graph via `graphrag_config.enable_router_fallback` (default `true`); also editable from the GraphRAG config page in the admin UI.
- **Trace Logs UI** — new admin page that captures and displays the full agent execution trace for each chat turn (per-node inputs/outputs, durations in seconds, citations, token usage by node)
  - Citations tab (now shown first), Token Overview tab, and a per-node detail view
  - Role-gated "View Trace" entry from the chat reply; superuser-only access on the trace endpoint
  - Per-user ownership check on `/ui/trace/{message_id}` and 30-day automatic cleanup of stored traces
  - Routed through nginx at `/trace`
- **Excel and CSV ingestion** — `.xlsx` / `.xls` / `.csv` accepted in document ingestion; the upload UI shows a clear warning when an unsupported file type is selected
  - Headerless Excel sheets preserve all rows; CSV extraction handles non-UTF-8 encodings without dropping content

### Changed
- **All customizable prompts now ship as in-code defaults**, packaged inside the LLM service. Provider prompt directories are kept (empty) for backward compatibility; per-graph and global overrides still win when present.
- **`prompt_path` is a top-level `llm_config` field**, applied across LLM-prompted services automatically. Per-service `prompt_path` entries are still honored on disk but no longer needed.
- **Permissive schema parser** accepts both `DIRECTED` and `UNDIRECTED` edges and rejects names that collide with GraphRAG structural types or GSQL keywords.
- **Server-side prompt validation**: `/prompts` POST rejects edits missing required placeholders and auto-escapes stray `{token}` occurrences in user content.
- **`apply_proposal` reports a real failure** when the GSQL server returns a known error marker, instead of falsely reporting success.
- **TigerGraph embedding store skips redundant GDS install** when the `gds.vector` package is already installed, eliminating multi-minute catalog-lock stalls on container restart.
- **TigerGraph version mismatch** raises a clear `ValueError` at ECC startup instead of leaving the embedding store undefined.
- **`check_embedding_store_status()`** in the inquiryai / supportai routers raises HTTP 503 instead of swallowing the exception.
- **Bedrock `max_tokens` is auto-defaulted** per model family (Claude 3.x = 4096, Sonnet 3.5+ / 4.x = 8192, Titan / Cohere / Llama at their published caps), so schema extraction and other large-output prompts no longer truncate at the langchain-aws built-in 1024 default. Explicit `model_kwargs.max_tokens` and the existing `token_limit` config field both override the auto-default.
- **Hybrid / similarity retrievers surface domain vertex types** in the LLM context with a `<TypeName>: <id>` label, so type-aware questions (e.g. "which companies …") receive properly grounded answers.
- **Community / hybrid retrievers walk domain edges and domain VTs directly** when a schema exists. The `Entity` layer becomes scaffolding for Louvain; community memberships are mirrored from `Entity` onto matching domain-VT instances after community detection so retrievers reach community context without traversing the legacy layer. New `graphrag_config.retrieval_include_entity` flag controls whether `Entity` stays visible to the chat agent — when unset, defaults to `false` for graphs with a domain schema (typed-purist) and `true` otherwise (no-op fallback).
- **`apply_proposal` re-installs retriever queries** against the live domain schema, idempotently. Identical bodies are TG no-ops; new domain types or a changed `retrieval_include_entity` value re-render the affected queries on the next apply call.
- **Transitional-graph detection at schema apply**: when a domain schema is added to a graph that already has Entity-layer data (typical v1.3.x → v1.4.0 upgrade applying a schema for the first time), `apply_proposal` forces `retrieval_include_entity=True` for the rendered queries so existing Entity rows stay reachable. The result payload carries a `transitional` block (`entity_count`, `new_domain_vts`, `recommendation`) for the init-graph dialog to surface a "your existing entities won't be auto-typed — re-ingest for full schema awareness" prompt. Once the user clears derived data and re-ingests (planned v1.5 admin endpoint), the auto-default flips back to typed-purist on the next apply call.
- **Empty function-call results now trigger retry** — `generate_function` now treats an empty result as a generation failure (symmetric with `generate_cypher`). Rewrite-and-retry kicks in, and after 3 cycles the cross-lane vector fallback runs. Previously, empty function results passed through to answer generation and risked hallucinated narratives around the emptiness.
- **Default chat retrieval method is now `auto`** instead of `hybridsearch`. Existing graphs that did not configure a method explicitly will route through the new selector after upgrade. Manual mode (and any explicitly-selected method in the chat dropdown) overrides the default unchanged.
- **Schema parser drops attribute names that collide with GSQL reserved words** (e.g. `count`, `min`, `max`). LLM-extracted schemas previously failed schema-change with `Encountered "," at line N` when an attribute named after a keyword reached TG; the offending attribute is now silently skipped at parse time.
- **`apply_proposal` runs schema changes in two phases** — phase 1 issues every `ADD VERTEX` / `ADD EDGE`, phase 2 issues every `ALTER EDGE ADD PAIR`. TG validates an entire `SCHEMA_CHANGE JOB` upfront, so an `ALTER` referencing a vertex type created in the same job aborted with a parser error. Splitting the job means phase 2 runs against a graph where the new types already exist. The result payload now exposes both phase names via a new `job_names` list; the legacy `job_name` key remains the first phase that ran.
- **Schema-extraction sample budget now scales with the configured LLM**. Previously hardcoded at 200 KB (~50K tokens), causing later files in multi-file uploads to be silently truncated. The budget is now resolved from `llm_config.token_limit` if set, otherwise from a per-model context-window table (Claude family 200K / Opus 4.7 1M, GPT-4o 128K, Gemini 1.5 1M, etc.). Unknown models fall back to a similar family default with a warning. Within the resolved budget, characters are distributed across uploaded files using equal-share-with-rollover so every file contributes — the first file no longer crowds out the rest.
- **`/initialize_graph` is now an async-job endpoint**. POST returns 202-style `{"status": "submitted"}` immediately and the long-running work (structural schema, optional domain schema apply, retriever installs) runs in a `BackgroundTask`. Clients poll `GET /ui/{graphname}/initialize_status` for `state` (`queued` / `running` / `completed` / `error`) and the final result. Previously the endpoint was fully synchronous; long inits (TG schema-change + retriever installs ≥ 5 minutes) tripped the browser's idle-response cutoff with `net::ERR_TIMED_OUT` even when the backend completed successfully.
- **New `GET /ui/list_graphs`**. Returns the live list of graphs the authenticated user has access to. UI clients (`KGAdmin`, `IngestGraph`, `Setup`) now seed `availableGraphs` from `sessionStorage` for instant render and then refresh from the live list, so a graph created mid-session is visible without a re-login.
- **Init / extract dialogs pause the idle timer for the duration of the long-running call**. The dialog used to log the user out after 60 minutes of "no activity" even while a backend init or schema extraction was in flight; the existing `pauseIdleTimer()` / `resumeIdleTimer()` pattern is now wired into `handleExtractSchema` and `handleInitializeGraph`.
- **Removed two dead vertex-type references from the retriever queries**. `Content_Similarity_Search` and `Content_Similarity_Vector_Search` referenced `Relationship` (never a vertex type — it's the `RELATIONSHIP` edge) and `Concept` (removed in an earlier release); both queries now save as draft with TYP-152 errors against any v1.4.0 graph. The IF-branch is reduced to `s.type == "Entity"` and the existing `Community` branch.
- **Retriever-install error detection no longer false-positives on TG's normal output**. `install_retrievers` and `install_retrievers_async` were doing a substring `"error" in output.lower()` check, which trips on every successful install (TG output contains literals like `0 errors`, `no warnings`). Both now delegate to the existing `gsql_output_error()` helper that matches actual error markers (`SEMANTIC ERROR`, `Failed to create`, transport-level failures).

> **Upgrading from a pre-release v1.4.0 build**: graphs that already
> have domain vertex types but were created before the multi-pair
> `IN_COMMUNITY` schema landed will see a "skipping community mirror
> for [...]: IN_COMMUNITY pair not on schema" warning during
> community detection. Re-run `/apply_proposal` with the existing
> schema once to backfill the missing pairs. v1.3.x graphs (no domain
> types) are unaffected — the mirror block is skipped entirely.

### Removed
- **`RELATIONSHIP_TYPE` edge** between `EntityType` vertices — superseded by `IS_HEAD_OF` + `HAS_TAIL` through `RelationshipType`.

### Configuration
- New `graphrag_config` keys: `schema_max_sample_files` (default 5), `schema_max_total_mb` (default 50), `strict_mode` (default false), `retrieval_include_entity` (auto: false when domain schema present, true otherwise), `enable_router_fallback` (default true).
- New env var: `PDF_IMAGE_CONCURRENCY` (default 8).
- `graphrag-ui` build now pins pnpm via `packageManager: "pnpm@9.15.0"` and ships an `.npmrc` allow-list for `@swc/core` / `esbuild` so the Docker image build does not trip pnpm 10's strict `[ERR_PNPM_IGNORED_BUILDS]` policy.

> Implementation-level details for v1.4.0 (parser internals, endpoint contracts, dialog state machine, prompt-resolution chain, schema-aware ECC worker logic, etc.) live in `dev/plans/graphrag/v1.4.0_implementation_notes.md`.

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
