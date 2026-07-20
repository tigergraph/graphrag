# Access Control (Tasks 2 & 3)

## Task 2: per-user isolation

### Identity is not the Basic-auth username

Three login shapes share the wire. Two of them do **not** carry the real user:

| Login | Username on the wire | Real identity |
|---|---|---|
| Username + password | `alice` | `alice` |
| API token | `__graphrag_token__` *(shared constant)* | Inside the token |
| GSQL secret | `__GSQL__secret` *(shared constant)* | The secret's owner |

Scoping by `creds.username` files **every API-token user's conversations under
one identity**, and lets any of them read any other's history. Every test where
users log in with a password still passes.

**Fix:** the principal is resolved through TigerGraph's own `SHOW USER`
(`_get_user_role_details`), and that resolved name (never the wire username) is
the `ChatUser` primary id everywhere.

**It fails closed.** If TigerGraph can't say who the caller is, the request is
refused. A fallback to the wire username would resolve straight back to the
sentinel, which is the bug being fixed.

Three pre-existing instances of this were found and fixed:
- `_chat_history_auth_header` fell back to `creds.username` on error
- The HTTP chat path passed raw `creds.username` as the trace owner
- The WebSocket path and `get_trace_log` both fell back to the sentinel, and
  since the trace reader compared owner against the same fallback, **sentinel
  matched sentinel**: every API-token user could read every other token user's
  traces

### Isolation enforcement

| Layer | Control |
|---|---|
| Query | Every scoped query takes `VERTEX<ChatUser>` (not a string) as its only entry point. There is no "give me everything" form |
| Repository | Principal bound at construction; no method accepts a user id |
| Endpoint | `/ui/user/{user_id}` rejects any `user_id` ≠ resolved caller with 403, before any query runs |
| Tool | No `chat_history__*` tool exposes a user or graph argument |

### Reads return empty, writes refuse

- **Read** of a conversation you don't own → empty result, identical to one that
  doesn't exist. A 403 that only fires for real ids lets someone probe which ids
  exist.
- **Write** to a conversation you don't own → refused. Silently allowing it would
  attach a second `OWNS_CONVERSATION` edge and hand the writer a copy of someone
  else's thread. The ownership probe is a primary-id lookup, which reveals only
  whether an id is taken.

### The assignment's scenario, concretely

1. Alice chats → identity resolved → `ChatUser("alice")` created on first write →
   messages attached under it.
2. Bob chats → his own disjoint subgraph.
3. Alice calls `GET /ui/user/alice` → endpoint resolves identity **from her
   credentials**, not the URL → returns only what's reachable from her `ChatUser`.
   Bob's data was never in the traversal.
4. Alice calls `GET /ui/user/bob` → **403** before any query runs.

---

## Task 3: agent restrictions

### Why scoped tools aren't enough

The obvious answer, "the agent's tools are scoped, so it's already solved," is
wrong, and this is the most important point in the submission.

GraphRAG's retrieval tooling lets the model generate and execute calls against
the live schema. That path:

- Reads the schema (`getVertexTypes()` / `getEdgeTypes()`) and hands it to the LLM
- Executes model-produced calls against the connection object
- Gates them by checking **the function name** against a registered set

So:

```
getVertices('ChatConversation')
```

is a well-formed call to `getVertices`, which is a **legitimately registered
function**. The name check passes. The arguments, where the type name actually
lives, were never inspected. It executes.

This doesn't go through the three scoped tools at all, so scoping them carefully
does nothing here. Not registering a "read all history" tool doesn't help
either: the model was never going to call a tool by that name; it uses the
general-purpose path with the type name as an argument.

**Task 1's schema decision creates Task 3's vulnerability.** Putting conversations
on the corpus graph is what earns the `RETRIEVED` edge. It's also what exposes
them to retrieval tooling. Same choice, both consequences.

### Where it's enforced

Six call sites read the schema. Patching each one is how one gets missed. Instead
the check sits at the single point every agent DB call already passes through:
`TigerGraphConnectionProxy` (`common/metrics/tg_proxy.py`), backed by
`common/chat_history/guard.py`.

| # | Control | Stops |
|---|---|---|
| 1 | `getVertexTypes` / `getEdgeTypes` / `getSchema` have chat types stripped | Accidental discovery |
| 2 | **Any typed data call whose arguments name a chat type is refused**, recursively through nested dicts/lists | `getVertices('ChatConversation')` |
| 3 | `runInstalledQuery` with a `Chat_` prefix is refused | Direct invocation of the installed queries |
| 4 | `Chat_*` queries installed but never registered in the discovery embedding store | Tool-selection surfacing them |

**Layer 2 is the one that actually holds.** Layer 1 is obscurity: a model that
has read a million chat applications can guess `ChatConversation` without being
shown it. Layer 4 is free but fragile: it lives in agent tooling and lapses the
moment someone registers these queries to make them "discoverable." Only the
argument-content check inspects what the call is actually doing.

Refusals raise `ChatHistoryAccessDenied` rather than returning empty: the attempt
is logged and auditable, and the model gets an error it can't mistake for
"no results found."

### Reverse edges

No chat edge declares a `REVERSE_EDGE`. It's tempting to assume that's what
prevents walking backward from a shared `DocumentChunk` into someone else's trace.
**It isn't.** This was tested, not assumed:

```
C:ch -(RETRIEVED>)- ChatTraceStep:s     → rejected at type-check (unsatisfiable FROM)
C:ch -(<RETRIEVED)- ChatTraceStep:s     → RETURNS BOB'S STEP
C:ch -(:e)-         ChatTraceStep:s     → RETURNS BOB'S STEP
```

GSQL traverses a directed edge backward whether or not a reverse edge is declared.
`WITH REVERSE_EDGE=` creates a *named* reverse type; it does not gate reverse
traversal. The path `shared DocumentChunk → Bob's TraceStep → Trace → Message →
Conversation` exists in the graph and the engine will walk it.

**So isolation rests on which queries are allowed to exist**, not on graph shape:
only reviewed queries are installed, every one seeds from `ChatUser`, and there is
no ad-hoc query path to chat data. TigerGraph's installed-query model is what
makes that meaningful: the query set is a fixed, reviewed API surface.

Reverse edges are still omitted, for the smaller honest reason: a declared
`reverse_RETRIEVED` would appear in `getEdgeTypes()`, which feeds the schema
description handed to the LLM. Defense in depth, not a guarantee.

### Prompt-level behavior

The system prompt for both engines states that history belongs only to the current
user, that no tool exists for another user's or another graph's history, and,
specifically, that the model must **not** phrase the limit as "no data found."
That phrasing implies other users have no history, when the real reason is the
assistant was never granted access.

The prompt is instruction, not enforcement. It shapes how the refusal reads; the
proxy is what makes it true.

### Admin override

`chat_config.conversationAccessRoles` (superuser / globaldesigner) can still see
all feedback, through `AdminFeedbackRepository` and its own query, deliberately
not a flag on the scoped repository. Role is checked by the caller before
construction; the class holds no credentials to check them itself.

`/ui/trace/{message_id}` requires **both** superuser role and ownership: a
superuser cannot read another user's trace by role alone. Both failures return the
same 404.

**Trace roles, precisely.** Writing a trace has no role check at all: every
chat turn is traced for whichever principal produced it, superuser or not
(`_save_trace_log` in `ui.py`, fired unconditionally on the response path).
Reading one is the narrow case:

- The role check is `{"superuser"}` only. `globaldesigner` qualifies for the
  cross-user feedback view above but **not** for trace reads — the two admin
  surfaces use different role sets on purpose.
- Superuser role and ownership are both required, not either. A superuser
  reading another user's `message_id` still gets nothing back, because the
  trace is reached by walking out of the *caller's* `ChatUser` — the role
  check only decides whether that walk is allowed to run at all.
- Net effect: `/ui/trace/{message_id}` isn't "admins can view any trace" and
  it isn't "users can view their own" either. It's "superusers can view their
  own" — a diagnostic surface for admins investigating their own sessions, not
  a general trace viewer.
