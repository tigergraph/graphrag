# TigerGraph chat history

GraphRAG stores conversations, messages, feedback, and redacted execution
traces in a dedicated TigerGraph graph named `GraphRAGChatHistory`. The
operational graph is deliberately excluded from normal graph discovery and
from knowledge-graph selection.

## Security model

- The authenticated TigerGraph username is the immutable history principal.
- Every user query starts from that principal's `OWNS_CONVERSATION` edge.
- Conversations remain bound to the knowledge graph on which they were
  created. Inaccessible, deleted, unowned, and graph-mismatched conversation
  IDs are reported as not found before an agent runs.
- Trace access requires both conversation ownership and the TigerGraph
  `superuser` role.
- Agent history tools are bound to the current principal and graph. Their
  schemas expose no username, credential, or graph-selection argument.
- Runtime and bootstrap credentials are read only from environment variables
  or Kubernetes Secret references. They must not be stored in
  `server_config.json`.
- Trace payloads redact credentials and authorization fields, omit raw
  document bodies, and enforce configured size and retention limits.

Stored messages are untrusted data. History returned to an agent is clearly
marked as stored content and must never be interpreted as agent instructions.

## Bootstrap

Bootstrap is idempotent: it creates the graph/schema if absent, installs the
principal-scoped query set, creates a graph-local runtime role, grants only
the required query execution permissions, and optionally assigns the role to
an existing runtime user.

```bash
export SERVER_CONFIG=configs/server_config.json
export CHAT_HISTORY_GRAPH=GraphRAGChatHistory
export CHAT_HISTORY_BOOTSTRAP_USERNAME=tigergraph
export CHAT_HISTORY_BOOTSTRAP_PASSWORD='admin-password'
export CHAT_HISTORY_RUNTIME_USERNAME=graphrag_history
python -m common.chat_history.bootstrap
```

The runtime user must already exist. Run bootstrap with a TigerGraph account
that can create graphs, install queries, create roles, and grant roles.
Bootstrap credentials are not used by the API process.

Configure the API with either a token:

```bash
export CHAT_HISTORY_TG_TOKEN='runtime-token'
```

or a username and password:

```bash
export CHAT_HISTORY_TG_USERNAME=graphrag_history
export CHAT_HISTORY_TG_PASSWORD='runtime-password'
```

Useful optional settings include:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `CHAT_HISTORY_RETENTION_DAYS` | `30` | Trace retention period |
| `CHAT_HISTORY_DEFAULT_PAGE_SIZE` | `50` | REST history page size |
| `CHAT_HISTORY_MAX_PAGE_SIZE` | `200` | Maximum REST page size |
| `CHAT_HISTORY_AGENT_PAGE_SIZE` | `20` | Maximum agent-tool page size |
| `CHAT_HISTORY_MAX_MESSAGE_BYTES` | `262144` | Per-message UTF-8 limit |
| `CHAT_HISTORY_MAX_TRACE_BYTES` | `524288` | Redacted trace JSON limit |
| `CHAT_HISTORY_WORKERS` | `8` | Shared TigerGraph I/O worker bound |
| `CHAT_HISTORY_TIMEOUT_SECONDS` | `10` | Installed-query timeout |

The supplied `docker-compose.yml` waits for TigerGraph health, runs
`history-bootstrap`, then starts the GraphRAG API. Kubernetes deployments
must create a `graphrag-chat-history` Secret with the keys referenced by the
manifest and wait for the bootstrap Job to complete before serving traffic.

### TigerGraph Savanna

Use `docker-compose.savanna.yml` when TigerGraph runs in a Savanna workspace.
This Compose file starts only GraphRAG, ECC, the history jobs, the UI, and
Nginx; it does not run the memory-heavy local TigerGraph image.

Keep all credentials in the current shell environment. Do not put them in
either JSON configuration file or commit them to an environment file:

```bash
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
export TIGERGRAPH_HOSTNAME='your-workspace.i.tgcloud.io'

export TIGERGRAPH_BOOTSTRAP_USERNAME='graphrag_bootstrap'
export TIGERGRAPH_BOOTSTRAP_PASSWORD='bootstrap-password'

export TIGERGRAPH_SERVICE_USERNAME='service-user'
export TIGERGRAPH_SERVICE_PASSWORD='service-password'

export CHAT_HISTORY_RUNTIME_USERNAME='graphrag_history'
export CHAT_HISTORY_RUNTIME_PASSWORD='history-password'

export OPENAI_API_KEY='openai-key'

docker-compose -f docker-compose.savanna.yml up -d --build
docker-compose -f docker-compose.savanna.yml ps -a
```

For a short-lived demonstration, the bootstrap user can also be supplied as
the service user. Production deployments should use a separate, least-
privileged service user for embedding-store operations. The runtime history
user remains separate and receives only the graph-local
`GraphRAGChatHistoryRuntime` role during bootstrap.

The workspace hostname is supplied without `https://`; Compose adds HTTPS and
uses Savanna's shared GSQL/REST++ port `443`. Open `http://localhost/` after
`graphrag` reports healthy. Log in with a TigerGraph database user, not the
Savanna organization account.

To inspect startup without exposing environment values:

```bash
docker-compose -f docker-compose.savanna.yml logs history-bootstrap
docker-compose -f docker-compose.savanna.yml logs --tail=100 graphrag
curl -fsS http://localhost:8000/health/history
```

Remove secrets from the shell when finished:

```bash
unset TIGERGRAPH_BOOTSTRAP_PASSWORD
unset TIGERGRAPH_SERVICE_PASSWORD
unset CHAT_HISTORY_RUNTIME_PASSWORD
unset OPENAI_API_KEY
```

## Retention

Expire traces once per day. The command is idempotent and safe to retry:

```bash
python -m common.chat_history.retention
```

Compose exposes this as the `history-retention` maintenance profile.
Kubernetes uses the `graphrag-history-retention` CronJob. Conversation
messages are retained; only expired trace and trace-step vertices are removed.

## SQLite migration

Migration requires a write pause. Do not dual-write.

1. Stop the old chat-history service and preserve `chats.db`.
2. Bootstrap the TigerGraph history schema and queries.
3. Preview the import:

   ```bash
   python -m common.chat_history.migrate_sqlite \
     --database /path/to/chats.db \
     --default-graph MyKnowledgeGraph \
     --dry-run
   ```

4. Run the import:

   ```bash
   python -m common.chat_history.migrate_sqlite \
     --database /path/to/chats.db \
     --default-graph MyKnowledgeGraph
   ```

The utility creates a timestamped SQLite backup by default, sorts messages
deterministically, skips soft-deleted rows, and uses stable source message IDs.
Re-running the same import reports replays instead of duplicating data.
`--no-backup` is available only when an independent verified backup exists.

Because legacy SQLite rows do not contain their creation graph, the required
`--default-graph` value applies to every imported conversation.

## API compatibility

Existing UI paths and message envelopes remain available:

- `GET /ui/user/{user_id}` accepts `graph_name`, `cursor`, and `limit` and
  returns the next cursor in `X-Next-Cursor`.
- `GET /ui/conversation/{conversation_id}` accepts the same pagination
  parameters.
- `POST /ui/feedback`, `DELETE /ui/conversation/{conversation_id}`, and the
  feedback admin endpoint retain their previous payload shapes.
- `GET /ui/trace/{message_id}` additionally enforces owner-plus-superuser
  authorization.
- REST query and WebSocket chat persist the user message before execution and
  persist the assistant response and redacted trace before returning it.

History persistence is fail closed. A TigerGraph history outage returns a
service error; it does not silently execute against an unverified
conversation or return an unpersisted answer.

## Agent tools

Agentic mode conditionally exposes these graph-bound tools:

- `history__list_my_conversations`
- `history__get_my_conversation`
- `history__search_my_messages`

They are available only when the request has an authenticated, graph-bound
history repository. Results are cursor-paginated and capped separately from
the REST API.

## Demonstration

1. Log in as two TigerGraph users with access to the same knowledge graph.
2. Create a conversation as the first user and continue it over REST and
   WebSocket.
3. Confirm the second user receives not found for the conversation ID.
4. Remove the first user's graph access and confirm continuation fails before
   model execution.
5. As the owner without `superuser`, confirm trace access is denied.
6. Add `superuser` to the owner and confirm the redacted trace is visible.
7. Ask agentic chat to list or search your previous conversations and inspect
   the tool schema to confirm no identity or graph arguments are present.
8. Restart GraphRAG and TigerGraph, then confirm the conversation remains
   available and pagination cursors still work.

## Rollback

1. Stop GraphRAG writes.
2. Preserve/export `GraphRAGChatHistory` and the pre-migration SQLite backup.
3. Deploy the previous application and chat-history images.
4. Restore the SQLite backup and resume the old service.

Messages created only after the TigerGraph cutover cannot be replayed by the
old SQLite service without a separate export/conversion step. Decide the
rollback point before enabling writes in production.
