# Jira Data Source Connector — Implementation Plan

Status: Implemented and validated locally; OAuth/ACL security remains planned  
Date: 24 September 2026  
Product: TigerGraph GraphRAG  
Source: Jira Cloud

---

## 1. Goal

An operator initializes or selects an existing graph, configures a Jira Cloud
site for that graph, installs the Jira schema, and runs
ingestion. GraphRAG can then answer:

- Structured questions such as “Which high-priority bugs are still open in
  PAY?” and “Who is assigned PAY-123?”
- Narrative questions such as “What did we decide about the checkout
  timeout?”

The connector writes into the graph that GraphRAG already queries. It does not
add a separate answer engine.

---

## 2. Design decisions

Each Jira issue is written in complementary structural and semantic forms:

1. POLE+O-aligned domain data: `JiraUser` is a Person subtype; `JiraIssue` and
   `JiraProject` are Object subtypes; `JiraComment` is an Event subtype.
2. One issue markdown `Document` containing issue metadata and description.
3. Deterministic `DocumentChunk` records written directly from each Jira
   comment, without an intermediate comment `Document`. Short comments produce
   one chunk; exceptionally long comments may produce multiple bounded chunks.
   Existing hybrid and similarity retrievers read these embeddings without a
   connector-specific retrieval path.

The connector maps Jira fields deterministically. It does not run LLM entity
extraction over Jira issue text. Status, project, assignee, reporter, parent,
and issue links already have authoritative structured values in Jira.

The entire connector lifecycle lives under **Data sources**: graph selection,
credentials, project scope, connection testing, schema migration, manual sync,
status, and errors. `Ingest to Knowledge Graph` remains the existing
file/cloud-document workflow and does not expose Jira a second time.

The connector never creates or replaces a graph. The operator first uses the
existing Initialize Knowledge Graph flow. Jira schema installation is an
explicit, additive, idempotent migration against that initialized graph.

### 2.1 Research basis: Glean

Glean uses one connector administration surface rather than duplicating Jira
under a generic upload page:

1. Admin console -> Connectors -> Add connector -> Jira Cloud.
2. The connector’s Setup tab owns authentication and source-specific settings.
3. Manage data owns project/custom-field inclusion rules.
4. The same connector detail shows crawl/index status and supports re-crawl.
5. Glean separates content crawls from identity/permission crawls and uses
   webhooks plus incremental/full crawls to keep the index current.
6. Indexed records use stable IDs, object types, searchable bodies, custom
   properties, source URLs, and document-level permissions.

Sources:

- [Glean: Get started with connectors](https://docs.glean.com/connectors/getting-started)
- [Glean: Jira Cloud connector](https://docs.glean.com/connectors/native/jira/)
- [Glean: Connector settings and visibility](https://docs.glean.com/connectors/connectors-settings-visibility)
- [Glean: Document model](https://developers.glean.com/api-info/indexing/documents/document-model)
- [Glean: Custom properties](https://developers.glean.com/api-info/indexing/datasource/custom-properties)

Glean configures a datasource schema (`objectDefinitions` and
`propertyDefinitions`) and then indexes stable documents carrying datasource,
object type, body, URL, custom properties, and permissions. GraphRAG requires
one additional connector-owned step: map that normalized Jira model to an
additive TigerGraph schema so structured graph retrieval can traverse issues,
projects, and users. That migration belongs inside the connector detail,
equivalent to Glean’s datasource-schema setup, not on the file-ingest page.

The implemented connector matches Glean’s connector lifecycle and
normalized content approach, but not its complete security model. Glean
indexes users, groups, memberships, and Jira permissions and enforces those
permissions at query time. GraphRAG currently uses one connector account plus a
project allow-list, so every GraphRAG user with graph access can retrieve the
indexed Jira data. Identity crawls and query-time ACL filtering remain Phase 5
work; they must not be simulated by adding more Jira business vertices to the
schema.

Jira access is scoped by the configured service account and project allow-list.
The current GraphRAG retrieval path does not enforce the chatting user’s Jira
permissions.

---

## 3. Existing GraphRAG workflow

```text
Operator uploads files or selects cloud storage
                         |
                         v
                Existing ingest API
                         |
                         v
       Issue Document -> DocumentChunk -> Content
       Comment -------> DocumentChunk -> Content
                         |
                         v
                  Existing retrievers
                         |
                         v
                       Answer
```

File ingest remains unchanged. Jira is not added as another
`CreateIngestConfig.data_source` because that API creates file loading jobs and
cannot write typed Jira edges such as `JIRA_ASSIGNED_TO`.

---

## 4. Target Jira workflow

```text
Initialize Knowledge Graph
          |
          v
Select existing initialized graph
          |
          v
Data sources -> Add Jira Cloud
          |
          +-- Configure and test credentials
          +-- Select projects and content settings
          +-- Inspect live graph schema
          |      |
          |      +-- missing/incomplete -> Install Jira schema elements
          |      +-- conflict ------> Stop with exact conflict
          |      +-- installed -----> Enable Ingest now
          |
          v
Ingest now: ingest Jira incrementally
          |
          +-----------------------------+
          |                             |
          v                             v
Typed Jira vertices/edges       Issue Documents       Direct comment chunks
                                      |                         |
                                      v                         v
                              Existing ECC rebuild      Existing embedding store
          |                             |
          +--------------+--------------+
                         |
                         v
              Existing GraphRAG retrievers
```

The connector ingestion and GraphRAG build use the existing per-graph lock.
File and cloud ingestion stay independent and continue to use their existing
Ingest to Knowledge Graph flow.

The sync API loads authoritative Jira vertices, edges, issue documents, and
directly embedded comment chunks, then returns `rebuild_required`. The Data
sources UI calls the existing
`POST /ui/{graph}/rebuild_graph` endpoint when changed documents or deletions
require a GraphRAG rebuild. ECC performs structure-aware chunking and embedding
for issue documents; comment chunks reuse the same embedding store during
connector sync and bypass ECC entity extraction.

Incremental ingestion orders tickets oldest-to-newest by Jira `updated` time.
After each search page has completed its graph and document writes, the
connector atomically persists that page’s highest `updated` timestamp. After an
unexpected shutdown, the next run resumes from that checkpoint minus a
120-second overlap. Upserts use Jira’s stable numeric issue id, making overlap
duplicates harmless. Newest-to-oldest ordering is intentionally avoided because
it could checkpoint past older tickets that have not yet been committed.

---

## 5. Repository changes

### 5.1 New backend modules

| Path | Responsibility |
| --- | --- |
| `graphrag/app/connectors/__init__.py` | Connector package |
| `graphrag/app/connectors/jira/config.py` | Validated Jira source configuration |
| `graphrag/app/connectors/jira/schema.py` | Predefined `SchemaProposal` |
| `graphrag/app/connectors/jira/client.py` | Jira REST API, pagination, retries |
| `graphrag/app/connectors/jira/adf.py` | Atlassian Document Format to markdown |
| `graphrag/app/connectors/jira/mapper.py` | Jira payload to graph records, issue documents, and direct comment chunks |
| `graphrag/app/connectors/jira/state.py` | Atomic graph-scoped config persistence |
| `graphrag/app/connectors/jira/sync.py` | Schema-gated incremental sync |
| `graphrag/app/routers/data_sources.py` | Authenticated data-source API |

### 5.2 Existing backend integration points

| Path | Required change |
| --- | --- |
| `graphrag/app/routers/__init__.py` | Export the data-source router |
| `graphrag/app/main.py` | Mount the data-source router |
| `ecc/app/graphrag/graph_rag.py` | Embed residual source-native chunks without duplicate LLM extraction |
| `ecc/app/graphrag/workers.py` | Skip duplicate LLM extraction and link each Jira chunk to its typed issue |
| `common/embeddings/tigergraph_embedding_store.py` | Retry transient embedding-provider failures with bounded backoff |
| `common/llm_services/base_llm.py` | Clarify business-key mapping and concise schema-driven structural lookup |
| `graphrag/app/tools/generate_function.py` | Permit the existing read-only pyTigerGraph function surface |
| `graphrag/app/tools/generate_cypher.py` | Normalize fenced model output safely |
| `graphrag/app/tools/graphrag_tools.py` | Recognize JSON-encoded empty results and improve Cypher retry feedback |
| `graphrag/app/tools/validation_utils.py` | Define the explicit read-only function allow-list |

### 5.3 Frontend changes

| Path | Required change |
| --- | --- |
| `graphrag-ui/src/pages/setup/DataSourcesConfig.tsx` | Single graph-scoped connector lifecycle: setup, schema, sync, status |
| `graphrag-ui/src/main.tsx` | Add `/setup/kg-admin/data-sources` |
| `graphrag-ui/src/pages/setup/SetupLayout.tsx` | Add Data sources navigation |

### 5.4 Existing code to reuse

| Existing code | Use |
| --- | --- |
| `common/db/schema_utils.py:apply_proposal` | Apply the Jira domain schema additively |
| `upsert_type_metadata` | Give query planning precise Jira type descriptions |
| `emit_structural_link_alters` | Allow `CONTAINS_ENTITY` to target `JiraIssue` |
| `common/chunkers` | Chunk issue markdown and unusually long comments |
| `graphrag/app/supportai/supportai_ingest.py` | Reuse the existing document-loading entry point |
| `common/config.py:get_embedding_store` | Reuse embedding and vector upsert behavior |
| `graphrag/app/tools/graphrag_tools.py` | Existing structured and unstructured answering; no connector-specific retrieval path |
| `common/utils/graph_locks.py` | Prevent sync from racing with ingest/schema operations |
| Existing UI auth and graph selection state | Do not create a second auth or graph-selection mechanism |

### 5.5 Code that stays unchanged

- Existing file and cloud storage ingest
- Existing vector and hybrid query implementations
- MCP server configuration
- Unrelated setup pages

---

## 6. Schema

The Jira schema adds four vertex types and nine edge types. Installation is
idempotent and adds only missing compatible elements.

A vertex is used only when questions need to traverse from one object to
another. Project, issue, user, and comment are those objects. Status, priority,
labels, components, fix versions, and dates remain issue attributes. Comments
are first-class Event records with separately embedded content. The Jira site
URL remains source configuration.

### 6.1 Schema picture

Read each line independently. The left side is the source vertex and the arrow
label is the edge type.

```text
JiraIssue  -- JIRA_BELONGS_TO -->  JiraProject

JiraIssue  -- JIRA_ASSIGNED_TO -->  JiraUser

JiraIssue  -- JIRA_REPORTED_BY -->  JiraUser

JiraIssue  -- JIRA_HAS_PARENT -->  JiraIssue

JiraIssue  -- JIRA_LINKS_TO -->  JiraIssue
              link_type = blocks | is blocked by | duplicates | relates to

JiraComment -- JIRA_COMMENT_ON --------> JiraIssue

JiraComment -- JIRA_COMMENTED_BY ------> JiraUser

JiraComment -- JIRA_COMMENT_REPLIES_TO -> JiraComment
               only when supplied explicitly by Jira

JiraComment -- JIRA_COMMENT_AFTER -----> JiraComment
               chronological order on one issue
```

Connector-owned edge names use a Jira prefix to avoid collisions with an
existing graph’s domain schema. This is mandatory for `JIRA_HAS_PARENT` and
`JIRA_LINKS_TO` because GraphRAG’s base schema already defines `HAS_PARENT`
and `LINKS_TO` for communities.

Issue descriptions and comment content reuse GraphRAG’s existing semantic
schema. Comments skip the unnecessary intermediate `Document`:

```text
issue Document ---- HAS_CHILD ----> issue DocumentChunk
      |                                      |
      +-- CONTAINS_ENTITY --> JiraIssue      +--> JiraIssue

JiraComment <---- CONTAINS_ENTITY ---- comment DocumentChunk
JiraIssue   <---- CONTAINS_ENTITY ----         |
                                                +-- HAS_CONTENT --> Content
```

`HAS_CHILD`, `HAS_CONTENT`, and `CONTAINS_ENTITY` already exist. Jira schema
application adds the Jira domain endpoint pairs to `CONTAINS_ENTITY`; this
design change adds no schema type.

### 6.2 Vertex types

All ids are prefixed with `jira:{cloudId}:` so different Jira sites cannot
collide. Numeric Jira ids are used because human issue keys can change when an
issue moves project.

| Vertex | Example id | Attributes |
| --- | --- | --- |
| `JiraProject` | `jira:abc:project:10001` | `project_key STRING`, `name STRING`, `url STRING` |
| `JiraIssue` | `jira:abc:issue:10422` | `issue_key STRING`, `summary STRING`, `issue_type STRING`, `status STRING`, `status_category STRING`, `priority STRING`, `resolution STRING`, `labels STRING`, `components STRING`, `fix_versions STRING`, `created DATETIME`, `updated DATETIME`, `due DATETIME`, `url STRING`, `story_points DOUBLE`, `content_hash STRING` |
| `JiraUser` | `jira:abc:user:557058:account` | `account_id STRING`, `display_name STRING` |
| `JiraComment` | `jira:abc:comment:9001` | `comment_id STRING`, `created DATETIME`, `updated DATETIME`, `visibility STRING`, `is_public BOOL`, `ontology_class STRING`, `content_hash STRING` |

`labels`, `components`, and `fix_versions` are comma-separated strings. They
do not require separate vertices for the current query set.

`status_category` contains `new`, `indeterminate`, or `done`, allowing
cross-project open/done filtering while retaining the project-specific status
name in `status`.

`content_hash` is sync bookkeeping. An unchanged hash skips document
re-chunking and re-embedding. The hash is advanced only after the replacement
document loads successfully, so a failed load is retried on the next sync.

`story_points` remains empty unless one numeric custom field is configured.

Email is not stored on `JiraUser`; Jira Cloud can hide it. `accountId` is the
stable user identity.

### 6.3 Edge types

| Edge | From | To | Extra attribute | Query purpose |
| --- | --- | --- | --- | --- |
| `JIRA_BELONGS_TO` | `JiraIssue` | `JiraProject` | | Project filtering |
| `JIRA_ASSIGNED_TO` | `JiraIssue` | `JiraUser` | | Current owner |
| `JIRA_REPORTED_BY` | `JiraIssue` | `JiraUser` | | Reporter |
| `JIRA_HAS_PARENT` | `JiraIssue` | `JiraIssue` | | Epic or subtask parent |
| `JIRA_LINKS_TO` | `JiraIssue` | `JiraIssue` | `link_type STRING` | Blocks, duplicates, relates to |
| `JIRA_COMMENT_ON` | `JiraComment` | `JiraIssue` | | Comment’s issue |
| `JIRA_COMMENTED_BY` | `JiraComment` | `JiraUser` | | Comment author |
| `JIRA_COMMENT_REPLIES_TO` | `JiraComment` | `JiraComment` | | Explicit source-provided parent comment |
| `JIRA_COMMENT_AFTER` | `JiraComment` | `JiraComment` | | Chronological order |

Current-state edges are replaced for each updated issue. This is necessary so
reassignment does not leave an old `JIRA_ASSIGNED_TO` edge.

Jira’s standard issue comments are normally a flat sequence. Reply edges are
never inferred; `JIRA_COMMENT_REPLIES_TO` is written only when the source
payload explicitly identifies a parent. `JIRA_COMMENT_AFTER` preserves the
available chronological order.

### 6.4 Type descriptions for query planning

| Type | Description |
| --- | --- |
| `JiraIssue` | A Jira work item. Filter by issue_key, status, status_category, priority, issue_type, resolution, labels, components, fix_versions, created, updated, and due |
| `JiraProject` | A Jira project. project_key is the short key such as PAY |
| `JiraUser` | An Atlassian account. account_id is identity and display_name is the human name |
| `JiraComment` | A POLE+O Event subtype representing one authored Jira comment with stable identity and timestamps |
| `JIRA_LINKS_TO` | Issue link whose link_type is blocks, is blocked by, duplicates, or relates to |
| `JIRA_HAS_PARENT` | Parent issue, including epic and subtask parent |
| `JIRA_ASSIGNED_TO` | Current assignee |
| `JIRA_REPORTED_BY` | User who reported the issue |
| `JIRA_BELONGS_TO` | Current project |

These descriptions are stored through `upsert_type_metadata` so the existing
question-to-schema mapper can choose the correct types and attributes.

### 6.5 Issue document

Document id: `jira:{cloudId}:issue-doc:{issueId}`

```text
# PAY-123: Login timeout on checkout
URL: https://acme.atlassian.net/browse/PAY-123
Project: PAY Payments
Type: Bug
Status: In Progress (indeterminate)
Priority: High
Assignee: Ada Lovelace
Reporter: Grace Hopper
Labels: checkout, auth
Components: API
Fix versions: 2.4
Updated: 2026-09-20T14:03:00Z

## Description
<ADF converted to markdown>

## Attachments
design.pdf, logs.txt
```

### 6.6 Comment event and direct chunk

Chunk id:
`jira:{cloudId}:comment:{commentId}:chunk:{index}:{contentDigest}`

```text
# Comment by Ada Lovelace on PAY-123: Login timeout on checkout
Issue: PAY-123
URL: https://acme.atlassian.net/browse/PAY-123
Comment ID: 9001
Author: Ada Lovelace
Created: 2026-09-18T11:02:00Z
Updated: 2026-09-18T11:02:00Z
Visibility: default

## Comment
<ADF converted to markdown>
```

The connector creates these chunks directly with content type `jira_comment`.
Structure-aware chunking emits one chunk for ordinary short comments and
bounded multiple chunks only for unusually long comments. For long comments,
log-dominated fenced or multiline blocks are replaced by an omission marker;
human explanation surrounding those logs is retained. Detection uses multiple
signals (timestamps, severity levels, stack traces, and structured log fields),
not length alone.

Every resulting chunk links to both `JiraComment` and `JiraIssue`. The connector
reuses the configured embedding store directly, so no Jira branch is added to
shared ECC processing and no duplicate LLM entity extraction occurs. The
comment content hash advances only after all replacement chunks and embeddings
succeed. Updating one comment therefore re-embeds that comment rather than the
entire issue. Removed comments have their vertex, direct chunks, content, edges,
and embeddings deleted during issue reconciliation. When legacy comment
Documents are detected, the connector performs one resumable full synchronization
to replace them with direct chunks, then returns to incremental checkpoints.

---

## 7. Jira API

Target Jira Cloud REST API v3 only.

Current authentication is HTTPS Basic authentication using an Atlassian account
email and API token.

| Endpoint | Use |
| --- | --- |
| `GET /rest/api/3/myself` | Test connection |
| `GET /_edge/tenant_info` | Resolve `cloudId` |
| `GET /rest/api/3/project/search` | Load all visible projects for the project picker |
| `GET /rest/api/3/project/{projectIdOrKey}` | Load one optional project directly by key |
| `POST /rest/api/3/search/jql` | Initial and incremental issue search |
| `GET /rest/api/3/issue/{id}/comment` | Fetch all comments if the embedded page is truncated |

Search rules:

- Use `/rest/api/3/search/jql`, not deprecated `/rest/api/3/search`.
- Use `nextPageToken`, not `startAt`, for issue search.
- Fetch search pages sequentially.
- Use `maxResults = 100`.
- Add `ORDER BY updated ASC, key ASC`.
- Combine the project allow-list with optional created-after, updated-after,
  and status-category filters. Additional JQL remains an advanced option.
- On HTTP 429, honor `Retry-After` and retry with bounded backoff.
- Advance the durable checkpoint only after every graph and document write for
  a page succeeds; do not advance it after a failed page or failed graph write.
- Reset the incremental checkpoint when scope filters change so a widened
  scope performs a full ingestion instead of skipping older matching issues.

Incremental JQL:

```text
project in (PAY, PLAT)
AND updated >= "{last successful checkpoint minus 120 seconds}"
ORDER BY updated ASC, key ASC
```

Jira search is not a stable snapshot. The overlap and idempotent upsert cover
updates near page boundaries.

The connector does not persist issue inventory on the local filesystem.
Consequently, incremental sync currently updates and adds records but does not
automatically remove issues deleted in Jira or moved outside the configured
scope. Production deletion reconciliation requires shared ownership state in
TigerGraph or another durable multi-replica state store.

---

## 8. Configuration and UI

### 8.1 UI locations

**Data sources** (`/setup/kg-admin/data-sources`) owns the selected graph’s
complete connector lifecycle:

1. Saved sources with enabled state, last ingestion, and last error.
2. Connection: display name, tenant site URL, Atlassian account email, API
   token, and connection status.
3. Enter an optional project key, then test the connection. Connection status
   is displayed next to that action.
4. Only after a successful test in the current editing session, load one
   requested project or every visible project, then select the projects for the
   graph. A direct project-key load replaces stale project results. Changing
   connection details invalidates verification and clears loaded results.
5. Configure optional created/updated dates and Jira status categories after
   project selection. Additional JQL and comment ingestion remain advanced
   scope settings.
6. Preview the approximate Jira ticket count using the unsaved project and
   filter scope, then save the scope.
7. Schema: inspect compatibility and install missing compatible elements.
8. Ingest/build actions: Ingest now, last completion, item count, and last error.
9. Connector lifecycle: edit, disable, or delete configuration.

**Ingest to Knowledge Graph** is unchanged. It remains responsible for local
files, cloud storage, and Amazon BDA. Jira does not appear there.

The UI requires an API token for every connection test and keeps that token
only in process memory for subsequent connector operations. UI-submitted tokens
are never persisted. If an administrator has already placed `api_token` in the
graph-scoped data-source configuration, saves preserve and may reuse it, but
the API never returns it to the browser.

### 8.2 Stored configuration

Path: `configs/graph_configs/{graph}/data_sources.json`

```json
{
  "sources": [
    {
      "id": "jira-acme",
      "type": "jira_cloud",
      "enabled": true,
      "display_name": "Acme Jira",
      "connection": {
        "site_url": "https://acme.atlassian.net",
        "email": "connector@example.com",
        "cloud_id": "abc123"
      },
      "scope": {
        "project_keys": ["PAY", "PLAT"],
        "created_after": "2026-01-01",
        "updated_after": null,
        "status_categories": ["new", "indeterminate"],
        "jql_extra": "",
        "include_comments": true
      },
      "sync": {
        "overlap_seconds": 120,
        "checkpoint": "2026-09-20T14:03:11.000+0000",
        "last_tested_at": "2026-09-20T13:50:00.000+0000",
        "last_started_at": null,
        "last_completed_at": null,
        "last_error": null,
        "last_issue_count": 0
      }
    }
  ]
}
```

Sync refuses to run with an empty project list.

Live TigerGraph metadata is authoritative: every status check validates
required attributes, edge direction, and endpoint pairs. No Jira schema
version number is stored.

### 8.3 API

| Method | Path | Behavior |
| --- | --- | --- |
| `GET` | `/ui/{graph}/data-sources` | List graph sources without secrets |
| `PUT` | `/ui/{graph}/data-sources/{id}` | Validate and atomically save |
| `DELETE` | `/ui/{graph}/data-sources/{id}` | Remove configuration only |
| `POST` | `/ui/{graph}/data-sources/{id}/test` | Test a freshly supplied token without persisting it |
| `GET` | `/ui/{graph}/data-sources/{id}/projects` | Return all visible projects, or one project when `project_key` is supplied |
| `POST` | `/ui/{graph}/data-sources/{id}/issues/count` | Preview Jira’s approximate count for an unsaved scope |
| `GET` | `/ui/{graph}/data-sources/jira/schema` | Inspect live Jira schema compatibility |
| `POST` | `/ui/{graph}/data-sources/jira/schema/install` | Add missing compatible Jira schema elements idempotently |
| `POST` | `/ui/{graph}/data-sources/{id}/sync` | Run schema-gated incremental Jira ingestion |
| `GET` | `/ui/{graph}/data-sources/{id}/sync/{run_id}` | Poll the in-memory status of a submitted sync |
| `POST` | `/ui/{graph}/rebuild_graph` | Existing endpoint used by the UI to start the ECC build |

---

## 9. Answer flow after ingestion

The implementation uses the existing GraphRAG agent and the request’s
TigerGraph connection. It does not yet enforce the chatting user’s Jira
permissions; Jira ACL-safe retrieval is Phase 5.

| Question | Existing path | Required data |
| --- | --- | --- |
| Which high-priority bugs are still open in PAY? | `structural_retrieve` | JiraIssue attributes and JIRA_BELONGS_TO |
| Who is assigned PAY-123? | `structural_retrieve` | issue_key and JIRA_ASSIGNED_TO |
| What does PAY-123 block? | `structural_retrieve` | JIRA_LINKS_TO.link_type |
| What did we decide about the checkout timeout? | `hybrid_search` | Issue document chunks |
| What status and owner does the issue discussing the timeout have? | hybrid plus structural, then `combine_context` | CONTAINS_ENTITY, status, JIRA_ASSIGNED_TO |
| What themes occur across payments bugs? | `hybrid_search` | Issue documents; community duplication is not part of the connector |

Jira keys are external identifiers, not TigerGraph primary vertex ids. For an
exact key such as `PAY-123`, the existing structural pipeline maps the value to
`JiraIssue.issue_key` and generates an exact attribute predicate. It must not
pass the key to `getVerticesById`; Jira primary ids are source-qualified,
stable ids based on the Jira cloud and numeric issue id.

For a numeric-only ticket reference such as `123`, the schema-driven
openCypher fallback uses delimiter-aware suffix matching and returns every
matching business key rather than inventing a project prefix. If more than one
project has that numeric suffix, the answer must expose the ambiguity.

The structural path first attempts an allowed read-only pyTigerGraph function.
JSON-encoded empty results such as `"[]"` are treated as empty so the existing
openCypher fallback can run. Generated Cypher is retried with execution errors
fed back to the generator. Full-key and numeric-suffix lookups have both been
verified against the development graph.

Hybrid retrieval is independent of structural ingestion. Issue descriptions
become searchable after ECC builds their document chunks. Comment chunks are
embedded during Jira synchronization and are searchable without passing
through ECC.

```text
Question
   |
   v
get_schema
   |
   +-- count/filter/key/traversal --> existing structural query path
   |
   +-- description/comments --> existing hybrid/vector retrieval
   |
   v
combine_context
   |
   v
Answer with issue key and Jira URL
```

---

## 10. Implementation phases

### Phase 1 — Core schema and mapping — Implemented

- Add validated source models.
- Add the three-vertex, five-edge schema proposal.
- Add ADF-to-markdown conversion.
- Map representative Jira payloads, including unassigned issues, parents,
  inward/outward links, hidden user email, missing priority, and custom story
  points.

Done when pure unit tests pass without TigerGraph or Jira.

### Phase 2 — Jira client and graph sync — Implemented

- Implement current enhanced-search pagination with `nextPageToken`.
- Implement bounded 429 retry.
- Inspect and validate the live Jira schema before every sync.
- Upsert structured records.
- Replace current-state edges.
- Load issue `Document`/`Content` records for ECC.
- Write bounded comment `DocumentChunk`/`Content` records directly and embed
  them with the existing store.
- Skip unchanged documents by `content_hash`.

Done when a mocked sync proves idempotency and a development graph shows both
the typed issue and linked chunks.

### Phase 3 — Schema migration, API, and UI — Implemented

- Add explicit schema status and install endpoints using existing additive
  schema utilities.
- Reject conflicts before applying a migration and gate sync on installed
  schema status.
- Add authenticated, graph-scoped CRUD/test/project/sync endpoints.
- Add masked secret handling and atomic config writes.
- Keep setup, schema migration, ingestion, build status, and re-ingestion under one
  Data sources connector detail.
- Do not add Jira to Ingest to Knowledge Graph.

### Phase 4 — Exit test — Partially completed locally

1. Configure a non-admin Jira service account.
2. Select one project.
3. Select the initialized graph under Data sources.
4. Install the Jira schema and confirm a second install is a no-op.
5. Run Ingest now and monitor ingestion/build status on the same page.
6. Ask a structured count question and compare it with Jira.
7. Ask a comment question and confirm the answer names and links the issue.

Current validation covers schema installation, typed ticket retrieval by full
and numeric-only keys, source document loading, deterministic chunk-to-issue
linkage, direct comment embedding, legacy cleanup, and resumable migration.

### Phase 5 — OAuth, identity, and permission enforcement — Planned

- Replace API-token crawling with confidential OAuth 2.0 (3LO) and rotating
  refresh tokens.
- Add self-service GraphRAG-user to Atlassian-account linking.
- Crawl accounts, groups, memberships, project permission schemes, project
  roles, issue-security schemes, and issue security levels.
- Split or omit role/group-restricted comments; never place them in a document
  with only issue-level ACLs.
- Activate permission crawls as validated generations and retain the previous
  generation until the replacement is complete.
- Pass the resolved GraphRAG identity into every retrieval path.
- Replace unrestricted Jira structural generation with fixed ACL-aware query
  surfaces. Filter semantic candidates before ranking, expansion, and LLM
  context construction.
- Deny all Jira results when account linking or permission state is missing,
  stale, incomplete, or unsupported.

---

## 11. Out of scope

- Jira Data Center and Server
- Forge and Connect authentication modes
- Sprints, boards, and backlog rank
- Changelog and worklogs
- Attachment content ingestion
- Shared cross-source Person identity
- LLM or ontology extraction over issue text
- Connector marketplace/plugin loading
- MCP as an ingestion path
- Webhooks and scheduled sync
- Jira Service Management SLA-specific entities

---

## 12. Risks and controls

| Risk | Control |
| --- | --- |
| Permission leakage | Known limitation: restrict graph access and connector scope; implement OAuth-linked identity, permission snapshots, and fail-closed retrieval in Phase 5 |
| Search pagination drift | Stable id upsert plus 120-second checkpoint overlap |
| Stale assignee or parent | Replace current-state outgoing edges for each updated issue |
| Re-embedding cost | Compare `content_hash` before rewriting chunks |
| Missing Jira embeddings | Embed direct comment chunks transactionally during sync; keep issue-document embedding in the existing ECC stage |
| Schema complexity | Keep only four Jira vertex types |
| Existing feature regressions | Isolated connector/router/page plus narrow, tested fixes to the shared structural-query path |
