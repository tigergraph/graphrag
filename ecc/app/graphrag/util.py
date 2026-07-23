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

import asyncio
import base64
import json
import logging
import re
import traceback

import httpx
from graphrag import reusable_channel, workers
from pyTigerGraph import AsyncTigerGraphConnection

from common.config import (
    graphrag_config,
    db_config,
    embedding_service,
    get_llm_service,
    get_completion_config,
    get_graphrag_config,
)
from common.db.schema_utils import (
    gsql_output_error,
    is_structural_type,
    read_existing_schema_async,
    read_type_metadata_async,
)
from common.embeddings.base_embedding_store import EmbeddingStore
from common.embeddings.tigergraph_embedding_store import TigerGraphEmbeddingStore
from common.extractors import GraphExtractor, LLMEntityRelationshipExtractor
from common.extractors.BaseExtractor import BaseExtractor
from common.logs.logwriter import LogWriter

logger = logging.getLogger(__name__)

http_timeout = httpx.Timeout(15.0)

_default_concurrency = graphrag_config.get("default_concurrency", 10)
# Worker amplifier: processing workers (chunk, embed, extract, community) run at 2x
# the base concurrency since each worker is mostly waiting on I/O (LLM/embedding API calls).
_worker_concurrency = _default_concurrency * 2
tg_sem = asyncio.Semaphore(_default_concurrency)

# Canonical lists live in common.db.query_sets so SupportAI init, the ECC
# rebuild, and the Migration Assistant share one source of truth.
from common.db.query_sets import GRAPHRAG_REQUIRED_QUERIES, GRAPHRAG_COMMUNITY_QUERIES

COMMUNITY_QUERIES = GRAPHRAG_COMMUNITY_QUERIES
REQUIRED_QUERIES = GRAPHRAG_REQUIRED_QUERIES
load_q = reusable_channel.ReuseableChannel()

# will pause workers until the event is false
loading_event = asyncio.Event()
loading_event.set() # set the event to true to allow the workers to run

# Written as a community's description when summarization can't produce a real
# one. Non-empty so the layer-completion check passes and the rebuild finishes,
# and stable so it can be found and regenerated later.
COMMUNITY_SUMMARY_PLACEHOLDER = "[summary unavailable - regenerate]"
# Placeholder written by pre-2.0.1 builds; kept so re-summarization and progress
# checks recognize communities left behind by older graphs too.
LEGACY_SUMMARY_PLACEHOLDER = "Should ignore due to summary error."

def is_placeholder_summary(text: str) -> bool:
    """True if a community description is a placeholder needing regeneration:
    the current or legacy sentinel, or empty."""
    t = (text or "").strip()
    return t in ("", COMMUNITY_SUMMARY_PLACEHOLDER, LEGACY_SUMMARY_PLACEHOLDER)

async def install_queries(
    requried_queries: list[str],
    conn: AsyncTigerGraphConnection,
):
    installed_queries = [q.split("/")[-1] for q in await conn.getEndpoints(dynamic=True) if f"/{conn.graphname}/" in q]

    # ECC installs only queries that are MISSING from TG. Drift-based
    # reinstallation of already-present queries belongs to the Migration
    # Assistant, not the rebuild — doing it here would reinstall every query on
    # every warm rebuild (slow, and stresses the install endpoint). For each
    # missing query we (re)create the body now; the install is batched below.
    to_install: list[str] = []
    for q in requried_queries:
        q_name = q.split("/")[-1]
        if q_name in installed_queries:
            continue
        res = await workers.install_query(conn, q, False)  # create body only
        if res["error"]:
            raise Exception(res["message"])
        to_install.append(q_name)

    if not to_install:
        logger.info("All required queries already installed and up to date.")
        return

    # Install ONLY the new/changed queries via the shared async-submit + poll
    # utility (see common.db.query_install for why pyTigerGraph's installQueries
    # is unsafe for large sets). The submit is quick and TG-semaphore-guarded;
    # the poll runs outside the semaphore so it never holds a slot for minutes.
    from common.db.query_install import submit_query_install_async, poll_query_install_async

    logger.info(f"Installing {len(to_install)} query(ies): {', '.join(sorted(to_install))}")
    async with tg_sem:
        request_id = await submit_query_install_async(conn, to_install)
    await poll_query_install_async(conn, request_id)
    logger.info("Required queries installed and verified.")


async def init(
    conn: AsyncTigerGraphConnection,
) -> tuple[BaseExtractor, dict[str, EmbeddingStore]]:
    """Initialize extractors and embedding store.

    Returns:
        (extractor, embedding_store)
    """
    # install required queries
    logger.info("Installing queries needed for GraphRAG all together")
    await install_queries(REQUIRED_QUERIES, conn)

    # extractor
    graph_cfg = get_graphrag_config(conn.graphname)
    if graph_cfg.get("extractor") == "graphrag":
        extractor = GraphExtractor()
    elif graph_cfg.get("extractor") == "llm":
        # Read the live schema and pack it into the LLM-extractor
        # bundle. ``build_allowed_schema_async`` filters structural
        # types, reads attribute schemas + definitions, and renders the
        # prompt text in one pass — same shape that query-side tools
        # consume via ``render_schema_rep``.
        try:
            from common.db.schema_utils import build_allowed_schema_async, AllowedSchema
            allowed_schema = await build_allowed_schema_async(conn)
        except Exception as exc:
            logger.warning(f"Loading domain schema for extractor failed: {exc}")
            from common.db.schema_utils import AllowedSchema
            allowed_schema = AllowedSchema()

        # Strict mode (graphrag_config.strict_mode, default false):
        # when false, entities whose type doesn't match a domain VT
        # still land in the plain Entity vertex.
        strict_mode = bool(graph_cfg.get("strict_mode", False))

        extractor = LLMEntityRelationshipExtractor(
            get_llm_service(get_completion_config(conn.graphname)),
            allowed_schema=allowed_schema,
            strict_mode=strict_mode,
        )
    else:
        raise ValueError("Invalid extractor type")

    embedding_store = TigerGraphEmbeddingStore(
        conn,
        embedding_service,
        support_ai_instance=True,
    )
    embedding_store.set_graphname(conn.graphname)

    return extractor, embedding_store


def make_headers(conn: AsyncTigerGraphConnection):
    if conn.apiToken is None or conn.apiToken == "":
        tkn = base64.b64encode(f"{conn.username}:{conn.password}".encode()).decode()
        headers = {"Authorization": f"Basic {tkn}"}
    else:
        headers = {"Authorization": f"Bearer {conn.apiToken}"}

    return headers


async def stream_ids(
    conn: AsyncTigerGraphConnection, v_type: str, current_batch: int, ttl_batches: int
) -> dict[str, str | list[str]]:
    try:
        async with tg_sem:
            res = await conn.runInstalledQuery(
                "StreamIds",
                params={
                    "current_batch": current_batch,
                    "ttl_batches": ttl_batches,
                    "v_type": v_type,
                }
            )
        ids = res[0]["@@ids"]
        logger.debug(f"Fetched ids: {ids}")
        return {"error": False, "ids": ids}

    except Exception as e:
        exc = traceback.format_exc()
        LogWriter.error(f"/{conn.graphname}/query/StreamIds\nException Trace:\n{exc}")

        return {"error": True, "message": str(e)}


def map_attrs(attributes: dict):
    # map attrs
    attrs = {}
    for k, v in attributes.items():
        if isinstance(v, tuple):
            attrs[k] = {"value": v[0], "op": v[1]}
        elif isinstance(v, dict):
            attrs[k] = {
                "value": {"keylist": list(v.keys()), "valuelist": list(v.values())}
            }
        else:
            attrs[k] = {"value": v}
    return attrs


def process_id(v_id: str):
    has_func = re.compile(r"(.*)\(").findall(v_id)
    if len(has_func) > 0:
        v_id = has_func[0]
    v_id = v_id.replace(" ", "_").lower().replace("/", "_").replace("(", "").replace(")", "")
    if v_id == "''" or v_id == '""':
        return ""

    return v_id


# Suffixes the LLM commonly tacks onto type labels without adding
# semantic distinction. Stripped during meta-layer normalization so
# ``Company_Type``, ``Company_Class``, ``Company_Entity`` collapse onto
# the same canonical name.
_TYPE_SUFFIXES = ("_type", "_class", "_entity", "_data", "_info", "_record")


def normalize_type_name(name: str) -> str:
    """Normalize an LLM-emitted vertex / edge type label so trivial
    variants collapse onto a single canonical id.

    Applies in order:

    1. ``process_id`` (lowercase, whitespace → ``_``, strip parens).
    2. Strip a single trailing semantic-suffix from
       :data:`_TYPE_SUFFIXES` (e.g. ``company_type`` → ``company``).
    3. Singularize trailing ``ies`` → ``y`` (``companies`` →
       ``company``); strip a single trailing ``s`` only when the
       preceding char is a consonant other than ``s``, ``i``, or ``u``
       (``reports`` → ``report``; preserves ``series``, ``status``,
       ``news``, ``business``).

    Used only for the EntityType / RelationshipType meta-layer in
    Case 1 (no domain types declared) — instance ids stay
    untouched. Synonym consolidation (``Company`` vs ``Corporation``)
    is out of scope for this deterministic pass.
    """
    base = process_id(name)
    if not base:
        return ""
    for suffix in _TYPE_SUFFIXES:
        if base.endswith(suffix) and len(base) > len(suffix):
            base = base[: -len(suffix)]
            break
    # Singularize defensively. Length thresholds keep short words
    # whose final ``s`` / ``ies`` is part of the singular stem
    # (``News``, ``Series``, ``Bus``, ``Status``, ``Yes``).
    if base.endswith("ies") and len(base) > 6:
        base = base[:-3] + "y"
    elif (
        base.endswith("s")
        and len(base) > 4
        and base[-2] not in "siu"
        and not base[-2].isdigit()
    ):
        base = base[:-1]
    return base


async def upsert_group(
    conn: AsyncTigerGraphConnection,
    vertices: list,
    edges: list,
):
    """Enqueue a bundle of related vertices + edges as a single load_q
    item, so the flusher batches them together and cancellation between
    individual upserts cannot leave half-applied state.

    ``vertices``: list of ``(vertex_type, vertex_id, attributes_dict)``
    ``edges``:    list of ``(src_v_type, src_v_id, edge_type, tgt_v_type,
                              tgt_v_id, attributes_dict)``

    A single ``load_q.put`` is one suspension point — either the whole
    bundle lands in the queue or nothing does.
    """
    packed_vertices = [
        (vt, str(v_id), map_attrs(attrs)) for vt, v_id, attrs in vertices
    ]
    packed_edges = [
        (s_t, str(s_id), e_t, t_t, str(t_id), map_attrs(attrs) if attrs else {})
        for (s_t, s_id, e_t, t_t, t_id, attrs) in edges
    ]
    await load_q.put(("group", {"vertices": packed_vertices, "edges": packed_edges}))


async def upsert_vertex(
    conn: AsyncTigerGraphConnection,
    vertex_type: str,
    vertex_id: str,
    attributes: dict,
):
    logger.debug(f"Upsert vertex: {vertex_id} as {vertex_type}")
    attrs = map_attrs(attributes)
    await load_q.put(("vertices", (vertex_type, vertex_id, attrs)))


def coerce_attrs_for_schema(
    props: dict,
    schema: dict,
) -> dict:
    """Coerce LLM-emitted properties to the declared TigerGraph types
    and drop anything not in *schema*.

    *props* — dict the LLM produced (values may be strings, numbers,
    bools depending on the model and the schema instruction).
    *schema* — ``{attr_name: tg_type}`` for the destination type
    (vertex or edge). ``tg_type`` is one of TG's primitive type names
    (case-insensitive): ``STRING``, ``INT``, ``UINT``, ``DOUBLE``,
    ``FLOAT``, ``BOOL``, ``DATETIME``.

    Behavior:
        * Attribute names are matched case-insensitively; the canonical
          schema spelling is used in the returned dict.
        * Values that can't be coerced (e.g. a non-numeric string for
          an INT field) are silently dropped — partial coverage is
          fine; a single bad value shouldn't reject the whole upsert.
        * Empty strings / ``None`` / sentinel values like ``"N/A"`` /
          ``"unknown"`` are dropped before coercion to avoid writing
          junk into typed columns.
    """
    if not props or not schema:
        return {}
    # Build a case-folded lookup once.
    schema_ci = {k.casefold(): k for k in schema.keys()}
    out: dict = {}
    for raw_name, raw_val in props.items():
        if not raw_name:
            continue
        canonical = schema_ci.get(str(raw_name).casefold())
        if not canonical:
            continue
        tg_type = (schema.get(canonical) or "").upper()
        coerced = _coerce_value(raw_val, tg_type)
        if coerced is not None:
            out[canonical] = coerced
    return out


_LLM_NULL_SENTINELS = frozenset({
    "", "n/a", "na", "none", "null", "unknown", "not specified",
    "not available", "not applicable", "tbd", "?",
})


# Primitive types accepted inside a TG DISCRIMINATOR(...) clause.
# Discriminator attrs must be present in every upsert; the worker
# fills missing values from ``_DISCRIMINATOR_FALLBACKS`` below.
_DISCRIMINATOR_TYPES = frozenset({"INT", "UINT", "STRING", "DATETIME"})

_DISCRIMINATOR_FALLBACKS: dict = {
    "INT": 0,
    "UINT": 0,
    "STRING": "",
    "DATETIME": "1970-01-01 00:00:00",
}


def coerce_edge_attrs_for_schema(
    props: dict,
    schema: dict,
) -> dict:
    """Coerce LLM-emitted properties for an edge upsert.

    Same matching + coercion as the vertex helper; additionally fills
    in default values for any discriminator-typed schema attribute
    the LLM did not provide, since TG requires every discriminator
    attribute to be present in each upsert.
    """
    if not schema:
        return {}
    coerced = coerce_attrs_for_schema(props or {}, schema)
    # Fill missing discriminator-typed attributes with type defaults.
    schema_ci = {k.casefold(): k for k in schema.keys()}
    for ci_name, canonical in schema_ci.items():
        if canonical in coerced:
            continue
        tg_type = (schema.get(canonical) or "").upper()
        if tg_type in _DISCRIMINATOR_TYPES:
            coerced[canonical] = _DISCRIMINATOR_FALLBACKS[tg_type]
    return coerced


def _coerce_value(value, tg_type: str):
    """Convert *value* to the TG type *tg_type*. Returns ``None`` when
    coercion would lose meaning (empty value, sentinel like ``"N/A"``,
    or a parse failure). Caller drops attrs that come back ``None``.
    """
    if value is None:
        return None
    # Quick string-sentinel filter — applies to every type.
    if isinstance(value, str):
        s = value.strip()
        if s.casefold() in _LLM_NULL_SENTINELS:
            return None

    try:
        if tg_type in ("INT", "UINT"):
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, (int, float)):
                v = int(value)
            else:
                # Strip thousand separators and surrounding whitespace.
                v = int(float(str(value).replace(",", "").strip()))
            if tg_type == "UINT" and v < 0:
                return None
            return v
        if tg_type in ("DOUBLE", "FLOAT"):
            if isinstance(value, bool):
                return float(value)
            if isinstance(value, (int, float)):
                return float(value)
            return float(str(value).replace(",", "").strip())
        if tg_type == "BOOL":
            if isinstance(value, bool):
                return value
            s = str(value).strip().casefold()
            if s in ("true", "yes", "y", "1"):
                return True
            if s in ("false", "no", "n", "0"):
                return False
            return None
        if tg_type == "DATETIME":
            # TG accepts 'YYYY-MM-DD HH:MM:SS' (space-separated).
            # Accept ISO-8601 with 'T' and normalize.
            s = str(value).strip()
            if not s:
                return None
            try:
                from dateutil import parser as _dt_parser  # type: ignore
                dt = _dt_parser.parse(s)
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                # Fall back to a few common formats without dateutil.
                from datetime import datetime as _dt
                for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S",
                            "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
                    try:
                        return _dt.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                return None
        # STRING (and anything we don't recognize): coerce to str.
        s = str(value).strip()
        return s or None
    except (ValueError, TypeError):
        return None


async def upsert_batch(
    conn: AsyncTigerGraphConnection,
    data: str,
    expected_vertices: int | None = None,
    expected_edges: int | None = None,
    batch_seq: int | None = None,
):
    """Send a batched JSON upsert to TG and log the response.

    ``expected_vertices`` / ``expected_edges`` are the pre-send batch
    counts, logged alongside the TG response so an "expected != accepted
    + skipped" gap is visible.

    ``batch_seq`` is the per-batch counter from the flusher; echoed on
    every log line this function emits.
    """
    seq_tag = f"#{batch_seq}" if batch_seq is not None else ""
    async with tg_sem:
        try:
            res = await conn.upsertData(data)
            acc_v = (res or {}).get("accepted_vertices", 0) if isinstance(res, dict) else 0
            sk_v  = (res or {}).get("skipped_vertices", 0) if isinstance(res, dict) else 0
            acc_e = (res or {}).get("accepted_edges", 0) if isinstance(res, dict) else 0
            sk_e  = (res or {}).get("skipped_edges", 0) if isinstance(res, dict) else 0
            ev = expected_vertices if expected_vertices is not None else "?"
            ee = expected_edges if expected_edges is not None else "?"
            untracked_v = (expected_vertices - acc_v - sk_v) if expected_vertices is not None else None
            untracked_e = (expected_edges - acc_e - sk_e) if expected_edges is not None else None
            vfit = ("OK" if untracked_v == 0 else f"GAP={untracked_v}") if untracked_v is not None else ""
            efit = ("OK" if untracked_e == 0 else f"GAP={untracked_e}") if untracked_e is not None else ""
            logger.info(
                f"Upsert res {seq_tag}: vertices sent={ev} accepted={acc_v} skipped={sk_v} {vfit} | "
                f"edges sent={ee} accepted={acc_e} skipped={sk_e} {efit}"
            )
            # Diagnostic: TG can silently skip vertices/edges (schema
            # mismatch, primary-id conflict, etc.) and only surface a
            # count. When that happens, check which of the sent vertex
            # ids actually landed in TG so the missing ones can be
            # identified.
            if sk_v or sk_e:
                try:
                    payload = json.loads(data)
                except Exception:
                    payload = {}
                v_section = payload.get("vertices", {}) or {}
                logger.warning(
                    f"[SKIP-DIAG {seq_tag}] batch had skipped_vertices={sk_v} "
                    f"skipped_edges={sk_e}; sent vertex types: "
                    f"{ {vt: len(vids) for vt, vids in v_section.items()} }"
                )
                for vt, vids in v_section.items():
                    sent = list(vids.keys())[:200]
                    missing = []
                    for vid in sent:
                        try:
                            found = await conn.getVerticesById(vt, vid)
                            if not found:
                                missing.append(vid)
                        except Exception:
                            missing.append(vid)
                    if missing:
                        logger.warning(
                            f"[SKIP-DIAG {seq_tag}] type={vt}: {len(missing)}/{len(sent)} "
                            f"missing after upsert"
                        )
                        logger.debug(
                            f"[SKIP-DIAG {seq_tag}] type={vt} first 10 missing ids: {missing[:10]}"
                        )
        except Exception as e:
            err = traceback.format_exc()
            logger.error(f"Upsert err {seq_tag}:\n{err}")
            logger.debug(f"Upsert err {seq_tag} payload: {data}")
            return {"error": True, "message": str(e)}


async def check_vertex_exists(conn, v_id: str):
    async with tg_sem:
        try:
            res = await conn.getVerticesById("Entity", v_id, select="description")

        except Exception as e:
            if "is not a valid vertex id" not in str(e):
                err = traceback.format_exc()
                logger.error(f"Check err:\n{err}")
            return {"error": True, "message": str(e)}

        return {"error": False, "resp": res}


async def upsert_edge(
    conn: AsyncTigerGraphConnection,
    src_v_type: str,
    src_v_id: str,
    edge_type: str,
    tgt_v_type: str,
    tgt_v_id: str,
    attributes: dict = None,
):
    if attributes is None:
        attrs = {}
    else:
        attrs = map_attrs(attributes)
    logger.debug(f"Upsert edge: {src_v_id} -[{edge_type}]-> {tgt_v_id}")
    await load_q.put(
        (
            "edges",
            (
                src_v_type,
                src_v_id,
                edge_type,
                tgt_v_type,
                tgt_v_id,
                attrs,
            ),
        )
    )


async def get_commuinty_children(conn, i: int, c: str):
    async with tg_sem:
        try:
            resp = await conn.runInstalledQuery(
                "get_community_children",
                # 1-tuple form for VERTEX<T> params; plain value
                # is deprecated in current pyTigerGraph.
                params={"comm": (c,), "iter": i}
            )
        except:
            logger.error(f"Get Children err:\n{traceback.format_exc()}")

    descrs = []
    try:
        res = resp[0]["children"]
    except Exception as e:
        logger.error(f"Get Children err:\n{e}")
        res = []
    for d in res:
        desc = d["attributes"]["description"]
        # if it's the entity iteration
        if i == 1:
            # filter out empty strings
            desc = list(filter(lambda x: len(x) > 0, desc))
            # if there are no descriptions, make it the v_id
            if len(desc) == 0:
                desc.append(d["v_id"])
            descrs.extend(desc)
        else:
            descrs.append(desc)

    return descrs


async def community_desc_progress(conn, i: int):
    """Return (all_have_desc, described_count, total_count) for layer i, or
    None if the progress query itself failed. `described_count` lets callers
    detect forward progress and bail if summarization stalls."""
    resp = None
    try:
        async with tg_sem:
            resp = await conn.runInstalledQuery(
                "communities_have_desc",
                params={"iter": i},
            )
    except Exception as e:
        logger.error(f"Check Vert Desc err:\n{e}")
        return None

    try:
        all_have = resp[0]["all_have_desc"]
        described = resp[1]["described"]
        total = resp[1]["total"]
    except Exception as e:
        logger.error(f"Check Vert Desc parse err:\n{e}")
        return None

    logger.info(f"layer {i} progress: {described}/{total} described")
    return all_have, described, total

async def check_embedding_rebuilt(conn, v_type: str):
    try:
        async with tg_sem:
            resp = await conn.runInstalledQuery(
                "vertices_have_embedding",
                params={
                    "vertex_type": v_type,
                }
            )
    except Exception as e:
        logger.error(f"Check embedding rebuilt err:\n{e}")

    res = resp[0]["all_have_embedding"]
    logger.info(resp)

    return res


async def graphrag_mirror_communities(
    conn: AsyncTigerGraphConnection,
    domain_vts: list[str],
) -> int:
    """Mirror Entity → Community memberships onto domain-VT instances
    that share the same id. Returns the number of mirror edges written.
    """
    if not domain_vts:
        return 0

    async with tg_sem:
        try:
            res = await conn.runInstalledQuery(
                "graphrag_stream_entity_community_pairs",
                params={},
                sizeLimit=1000000000,
            )
        except Exception as e:
            logger.error(f"stream entity-community pairs failed: {e}")
            return 0

    pairs = (res[0] if res else {}).get("pairs", []) or []
    if not pairs:
        return 0

    valid_ids_by_vt: dict[str, set[str]] = {}
    for vt in domain_vts:
        try:
            async with tg_sem:
                r = await conn.runInstalledQuery(
                    "graphrag_stream_all_ids",
                    params={"v_type": vt},
                    sizeLimit=1000000000,
                )
        except Exception as e:
            logger.warning(f"stream_all_ids({vt}) failed: {e}")
            valid_ids_by_vt[vt] = set()
            continue
        ids = set((r[0] if r else {}).get("@@ids", []) or [])
        valid_ids_by_vt[vt] = ids

    written = 0
    chunk_size = 5000
    for vt, valid_ids in valid_ids_by_vt.items():
        if not valid_ids:
            continue
        edges = [
            (p["entity_id"], p["community_id"])
            for p in pairs
            if isinstance(p, dict)
            and p.get("entity_id") in valid_ids
            and p.get("community_id")
        ]
        if not edges:
            continue
        for i in range(0, len(edges), chunk_size):
            chunk = edges[i:i + chunk_size]
            async with tg_sem:
                try:
                    await conn.upsertEdges(
                        sourceVertexType=vt,
                        edgeType="IN_COMMUNITY",
                        targetVertexType="Community",
                        edges=chunk,
                    )
                    written += len(chunk)
                except Exception as e:
                    logger.error(
                        f"upsertEdges IN_COMMUNITY for {vt} (chunk size "
                        f"{len(chunk)}) failed: {e}"
                    )

    logger.info(
        f"graphrag_mirror_communities: wrote {written} mirror "
        f"IN_COMMUNITY edges across {len(domain_vts)} domain VT(s)"
    )
    return written
