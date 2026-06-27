# Changelog

## [2.0.0]

### Added
- **Query responses can return just the answer.** The query endpoints return the answer alone by default and accept an option to include the supporting sources and trace when a caller needs them.

### Changed
- **Structured documents chunk more faithfully.** Markdown and HTML are split with a structure-aware chunker that keeps each section's heading context inside the chunk, rolls small sections up into their parent up to the size budget, and keeps tables intact — including tables nested inside lists — so retrieval and answers hold together on heading- and table-heavy documents.
- **Prompt customization is additive instead of a full rewrite.** The *Customize Prompts* page now exposes only an editable instructions-and-examples section; the underlying rules are fixed and no longer user-editable, so a customization can extend behavior without accidentally dropping required rules. Pre-existing full-prompt overrides are ignored until re-saved in the new form.
- **Retrieval matches table-heavy and numeric content more reliably.** Each chunk is embedded together with a compact summary of its topic, section, and key entities, so dense vectors carry that context explicitly — improving answers on documents where the raw text alone embeds poorly.

- **Query installation is more reliable on large graphs.** Graph queries install through a non-blocking request with status polling instead of one long call, so initialization no longer fails on a gateway timeout while queries compile.

### Fixed
- **A single oversized chunk no longer drops embeddings for the rest of a batch.** Embeddings that exceed the provider's input limit are retried at progressively shorter lengths, and a vertex that still doesn't fit is skipped individually instead of aborting the batch; similarity search ignores vertices without an embedding.
- **Large ingests no longer fail on oversized upsert batches.** Upserts are sized to the pending work so very large flushes are not rejected, and progress counts reflect distinct vertices and edges.
- **Schema lookups resolve correctly on asynchronous request paths.** The schema-version lookup is now awaited where it was previously used without awaiting.
- **Ingestion resumes after a transient database disconnect.** Files whose load hits a connection error are retried once the database is reachable again (bounded, so a persistent outage fails out rather than hanging), and any that still fail are named so re-running ingest reloads only those — already-loaded documents upsert idempotently.
- **Non-ASCII answers no longer break when context is large.** Retrieved context is measured against the model's input limit in the same form that is sent to it, so Japanese and other multi-byte content is no longer mis-sized and truncated incorrectly.

## [1.4.2]

### Added
- **Graph compatibility check and repair.** The *Knowledge Graph Admin* page can now scan an existing graph for installed GSQL queries whose body has drifted from the shipped version — or that are missing entirely — and repair them in place, without rebuilding the knowledge graph. This makes it safe to pick up query fixes from a GraphRAG upgrade on graphs that already hold data. Repair runs under the per-graph lock and is refused while a rebuild is in progress. New endpoints: `GET /ui/{graphname}/migration/status` and `POST /ui/{graphname}/migration/apply`.

### Changed
- **Documents whose filenames contain spaces or mixed case now ingest reliably.** Every ingest path normalizes vertex IDs the same way, so a document is stored under one consistent id instead of diverging between paths — it becomes retrievable and participates in entity extraction instead of being silently skipped or duplicated.
- **Interrupted knowledge-graph rebuilds recover cleanly on the next run.** Chunks left unfinished by a crashed or cancelled run are reconciled before new documents are processed, and each chunk is written together with its content in one step — so rebuilds no longer leave chunks without content or emit spurious "missing content" warnings.
- **Shipped query fixes apply automatically on existing graphs.** At initialization, any query whose installed body differs from the shipped version is re-created, so improvements in the bundled queries take effect after an upgrade without a manual reinstall.
- **The document ingestion dialog reports per-file upload failures.** When some uploads fail, the dialog names the affected files and the reason instead of failing the batch opaquely.

## [1.4.1]

### Added
- **Token login** — the sign-in page adds a "Use token login" option with a choice of API Token or Secret, alongside the default username / password. The signed-in username and roles are resolved from TigerGraph after login so the UI shows the real user.
- **Pre-flight upload conflict check** — a new endpoint reports which planned filenames already exist before the bytes are sent. The upload dialog uses it to prompt the user once with the conflicting names and offer Replace or Skip; large files no longer have to cross the wire twice when a collision is hit.

### Changed
- **Every request authenticates as the signed-in user**, end to end — graph operations, chat history, traces, and knowledge-graph rebuilds all run under the caller's identity (username / password, secret, or API token).
- **TigerGraph token handling is automatic** — an api token is obtained from the caller's credentials only when the database requires one, unless a static api token is configured. The `getToken` config option is no longer needed and is now ignored.
- **Sample documents are visible in the upload dialog after schema extraction.** Earlier, files used for schema extraction landed in a hidden per-request subdirectory and disappeared from the dialog. They now live alongside regular uploads, and overwriting one drops the cached extract so the next ingest sees the new bytes.
- **Schema extraction requires an explicit sample list.** The endpoint no longer treats a missing or empty `filenames` field as "use every JSONL in the temp folder," which silently mixed in stale samples from prior sessions. Callers must name each sample explicitly.
- **One schema extraction at a time per graph.** Concurrent attempts on the same graph are rejected with 409 instead of racing on the shared sample folder.
- **Document Ingestion dialog reflects server-side state on reopen.** Closing the dialog mid-conversion and reopening it no longer leaves the *Ingest* button incorrectly enabled. The dialog asks the server which operation, if any, currently holds the graph lock, and polls until that operation completes — so the button stays disabled, the uploaded files list re-populates, and the next upload doesn't collide with the prior conversion.
- **Conflict prompts use the app's styled dialog** instead of the browser default. Choosing *Cancel* now aborts the upload cleanly — the button and status message reset right away.
- **The vector store recovers without a container restart.** When the initial connection to TigerGraph fails (e.g. cold start, transient network blip), the service used to stay broken until the operator restarted the container — chat connections were rejected silently with WebSocket close 1013. The vector store now retries automatically in the background (10s → 30s → 60s → 120s → 300s backoff), and a new ``POST /ui/admin/retry_embedding_store`` lets superusers force a retry immediately after fixing the underlying issue.
- **Chat stays available when vector search is unavailable.** The chat WebSocket no longer closes hard with 1013 on vector-store failures. Instead it accepts the connection, surfaces a notice to the client, and lets graph-traversal questions answer normally — only questions that genuinely require a vector lookup fail, and they fail gracefully through the synthesizer.
- **PDF ingestion is faster on image-heavy documents.** Image-description workers now run with a larger parallel pool, and tiny decorative images skip the multimodal LLM entirely. On AWS Bedrock deployments the connection pool default is also raised so concurrent describe calls no longer queue behind a 20-connection cap.
- **Image description is tunable per graph or globally.** Two new `graphrag_config` keys — `extract_images` and `min_image_dim_px` — control whether the multimodal LLM is invoked on extracted images and the smallest image dimension that goes to the LLM (smaller images skip the call). Both are editable from the *GraphRAG Configuration* page in the UI, globally or per graph. Disabling does not alter the Image vertex type or loading job, so re-enabling later requires no schema change. The multimodal describe pass now reuses `default_concurrency` instead of a separate knob, so one setting tunes parallelism across the pipeline.
- **Community search falls back to hybrid search when it returns nothing or fails.** Auto-selected community queries that miss (no relevant community summaries) or hit a retriever error are now retried once with hybrid graph-hop search before returning a "couldn't find" answer. Manually-picked community search is unchanged.

### Removed
- **A configured static `apiToken` no longer overrides per-user credentials.** It is used only for the service's background operations; interactive requests always authenticate as the signed-in user.

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
- **Suggested types in the Generate-from-samples dialog**. Two chip inputs ("Suggested Vertex Types", "Suggested Edge Types") let the user guide the schema extractor with structured hints. Vertex chips use `Name` or `Name: description`; edge chips additionally accept `Name (From -> To)` to pin direction. Hints render into a `## Suggested types` block injected before the prompt's `## Inputs` section, and the fully-rendered prompt is auto-saved as the graph's per-graph `schema_extraction.txt` override after a successful init — future re-extractions for the same graph reuse the same guidance.
- **Inline rejection of reserved names in suggested type chips**. New `GET /ui/schema_reserved_names` returns GSQL reserved words and GraphRAG structural type names; the dialog rejects suggestions that would collide with either, before the call is made. Previously such names were silently dropped by the downstream parser, leaving the user wondering why a suggested type didn't appear in the draft.
- **Schema Extraction prompt is now editable on the Customize Prompts page**, alongside the existing four prompts. Global and per-graph scope both work — the per-graph file is what the auto-save from the suggested-types flow writes.
- **Per-card collapse in the draft-schema review form**. Each vertex / edge card has a chevron toggle that hides everything except its name row; section headers expose "Collapse all vertex types" / "Collapse all edge types" buttons. Keeps a 30+ type proposal readable without losing edit access.
- **Multimodal image-description LLM calls are now distinguishable in the log**. `describe_image_with_llm` emits `multimodal_describe: image=<basename> model=<name>` before each call and a paired `done` line after, so the per-image vision calls can be filtered out of the chat-completions stream (a single PDF can produce hundreds of them).
- **Ingest no longer fails with `Data path not found: None`** when the "Ingest Documents into Knowledge Graph" button is clicked without first running the two-step ingest flow. The UI handler now calls `/create_ingest` first when the cached job state is empty, so the backend always receives a well-shaped configuration with the resolved JSONL temp folder.
- **Query Guidance** — a free-form, optional partial that the user edits on the Customize Prompts page. Empty by default; when configured, the rendered block is injected after the hard rules in `map_question_to_schema`, `generate_function`, `generate_cypher`, and `generate_gsql`. Length-capped at 8000 characters and brace-escaped server-side. The page is also reordered by graph lifecycle (setup → ingest → rebuild → query) and the now-redundant "Configured LLM Provider" field is removed.
- **Stop aborting graph rebuilds on TigerGraph's normal success line**. `ecc.app.graphrag.util.install_queries` and its supportai sibling were raising on the literal `"failed" in res.lower()` substring — TG's success line `succeeded: N, skipped: 0, failed: 0` tripped that check and rolled back the rebuild. Both now use the existing `gsql_output_error()` helper.
- **Image version stamped at build time**. Each image now carries the repo-root `VERSION` file plus a `/code/BUILD_DATE` written at build time; `GET /ui/version` aggregates the three components for support checks, and the Setup pages show a small "Version <x.y.z>" line at the bottom-center. Plain `docker compose build` works as-is; no env vars or helper scripts required.
- **Prompt-customization E2E test always reverts on failure**. The schema-extraction round-trip test could leak its `[E2E TEST EDIT — schema_extraction]` marker into `configs/prompts/schema_extraction.txt` whenever a mid-flight assertion failed; both the chatbot-response and schema-extraction tests now wrap their save-then-assert in `try/finally` (or `try/except`) so the revert always runs.
- **Knowledge Graph Setup cards centered**. The three setup cards (Initialize / Ingest / Refresh) drop from a 4-column grid at large breakpoints to a 3-column grid so they fill the row evenly instead of leaving an empty fourth column.
- **Per-stage progress for graph rebuild.** The refresh dialog now shows individual phases — chunking, entity extraction, community detection, domain-type update — with per-stage heartbeats so a long phase never looks stalled.
- **Reclassified data-bearing log lines from INFO to DEBUG.** Rebuild logs in steady state now carry only metadata and counts; lines that included entity names, chunk identifiers, or edge payloads drop to DEBUG. Typical INFO volume falls from a few thousand lines per rebuild to under 150.
- **Query-generation prompts see user-supplied type descriptions.** The descriptions / definitions a user attaches to vertex and edge types via Initialize Graph or Customize Prompts now reach every query-side LLM call, not just the cypher/gsql ones.
- **Graph picker stays in sync across dialogs.** Changing the selected graph in chat, refresh, ingest, or customize-prompts updates the others immediately — they no longer drift apart.
- **Chat WebSocket fails gracefully when the embedding store is unavailable.** Clients receive a structured error and a Try-Again-Later close code instead of an instant disconnect.
- **Rebuilds survive chunk-creation races.** A transient empty response when a freshly created chunk's content row hasn't flushed yet is now retried instead of aborting that chunk.
- **Images in chat render correctly.** Multi-line LLM image captions used to break the markdown image syntax so chat showed the raw markup; alt text is now sanitized on insert and the image-description prompt asks for a single content-focused paragraph (text, charts, tables, diagrams, logos — no layout or decorative styling).

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
