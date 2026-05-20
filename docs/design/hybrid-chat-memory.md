# Hybrid Chat Memory

TigerGraph-backed conversation memory with rolling summary (LTM), recent verbatim turns (STM), and optional per-question routing.

## Configuration (`server_config.json` → `memory`)

| Key | Description |
|-----|-------------|
| `enabled` | Master switch |
| `mode` | Must be `"hybrid"` |
| `routing_enabled` | Rule-based stm / ltm / hybrid / none per question |
| `routing_fallback` | When rules are ambiguous (default `hybrid`) |
| `message_retention_count` | Max live `message` vertices per thread after prune (`0` = disabled) |
| `conversation_retention_days` | Reserved for TTL jobs (`0` = disabled) |
| `summarizer_max_messages_per_run` | Cap uncovered messages per compaction batch (`0` = all) |

Requires `graphrag_config.tg_memory_enabled`.

## Code layout

| Module | Role |
|--------|------|
| `common/memory/memory_router.py` | `decide_memory_route` (phrase + structural rules) |
| `common/memory/context_builder.py` | `build_agent_context`, compaction, `prune_message_window` |
| `common/memory/tg_memory.py` | TigerGraph read/write |
| `graphrag/app/routers/ui.py` | `run_agent` orchestration |

## Flow

1. User question → `build_agent_context(..., question=)` → optional route → agent.
2. After reply → `write_exchange_to_tg_memory` → `refresh_summary_async` (summary + prune).

See implementation doc sections in project chat / design review for worked examples (N=10 retention).
