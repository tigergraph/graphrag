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

import logging
import traceback
import json
from time import sleep, time
from collections import defaultdict
from typing import Iterable, List, Optional, Tuple

import Levenshtein as lev
from asyncer import asyncify
from langchain_core.documents.base import Document

from common.embeddings.base_embedding_store import EmbeddingStore
from common.embeddings.embedding_services import EmbeddingModel
from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter
from common.metrics.prometheus_metrics import metrics

from pyTigerGraph import TigerGraphConnection

logger = logging.getLogger(__name__)


class TigerGraphEmbeddingStore(EmbeddingStore):
    def __init__(
        self,
        conn: TigerGraphConnection,
        embedding_service: EmbeddingModel,
        support_ai_instance: bool = False,
        default_vector_attribute: str = "embedding",
    ):
        self.embedding_service = embedding_service
        self.support_ai_instance = support_ai_instance
        self.default_vector_attribute = default_vector_attribute
        self.vector_attr_cache = {}

        if isinstance(conn.apiToken, tuple):
            token = conn.apiToken[0]
        elif isinstance(conn.apiToken, str):
            token = conn.apiToken
        self.conn = TigerGraphConnection(
                host=conn.host,
                username=conn.username,
                password=conn.password,
                graphname=conn.graphname,
                restppPort=conn.restppPort,
                gsPort=conn.gsPort,
                tgCloud=conn.tgCloud,
                certPath=conn.certPath,
                sslPort = conn.sslPort,
                jwtToken = conn.jwtToken,
                apiToken = token,
             )

        tg_version = self.conn.getVer()
        ver = tg_version.split(".")
        if int(ver[0]) >= 4 and int(ver[1]) >= 2:
            self._ensure_gds_installed()
            if self.conn.graphname and not self.conn.graphname == "MyGraph":
                current_schema = self.conn.gsql(f"USE GRAPH {self.conn.graphname}\n ls")
                if "(Dimension=" in current_schema:
                    self.install_vector_queries()
            logger.info(f"TigerGraph embedding store is initialized with graph {self.conn.graphname}")
        else:
            raise Exception(f"Current TigerGraph version {ver} does not support vector feature!")

    def _ensure_gds_installed(self) -> None:
        """Install the gds package only if the gds.vector sub-package
        isn't already present.

        Probes via ``SHOW PACKAGE gds`` whose output looks like
        ``Packages "gds":\\n  - Sub-Packages:\\n    - vector\\n`` when
        the sub-package is installed (verified empirically). Checking
        the sub-package (rather than just the top-level ``gds``) also
        catches a partial-install state where ``gds`` is present but
        ``gds.vector`` isn't.

        The probe is a fast catalog read (~60ms) compared to
        ``install function gds.**`` which takes a global catalog
        write lock for the duration of the install scan (~3 minutes
        against a remote TG). Skipping the install when the package
        is present avoids that lock on every container restart.

        Falls through to the install on any probe failure — better to
        occasionally re-install than to skip an install that's
        actually missing.
        """
        try:
            sub_packages = self.conn.gsql("SHOW PACKAGE gds")
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                f"GDS-presence probe failed: {exc}. Falling through to install."
            )
            sub_packages = ""
        # The expected installed output is:
        #   'Packages "gds":\n  - Sub-Packages:\n    - vector\n'
        # When gds isn't installed at all, ``SHOW PACKAGE gds`` returns
        # an error message instead, so this check fails closed.
        if "- vector" in sub_packages:
            logger.info("GDS library already installed; skipping install.")
            return
        logger.info("Installing GDS library")
        q_res = self.conn.gsql(
            """USE GLOBAL\nimport package gds\ninstall function gds.**"""
        )
        logger.info(f"Done installing GDS library with status {q_res}")

    def install_vector_queries(self):
        logger.info(f"Installing vector queries")
        vector_queries = [
            "vertices_have_embedding",
            "check_embedding_exists",
            "get_vertices_with_vector",
            "get_topk_similar",
            "get_topk_closest",
        ]

        installed_queries = [q.split("/")[-1] for q in self.conn.getEndpoints(dynamic=True) if f"/{self.conn.graphname}/" in q]
        need_install = False
        for q_name in vector_queries:
            if q_name not in installed_queries:
                with open(f"common/gsql/vector/{q_name}.gsql", "r") as f:
                    q_body = f.read()
                q_res = self.conn.gsql(
                    """USE GRAPH {}\nBEGIN\n{}\nEND\n""".format(
                        self.conn.graphname, q_body
                    )
                )
                need_install = True
                logger.info(f"Done creating vector query {q_name} with status {q_res}")
        if need_install:
            logger.info(f"Installing vector queries all together")
            query_res = self.conn.gsql(
                """USE GRAPH {}\nINSTALL QUERY ALL\n""".format(
                    self.conn.graphname
                )
            )
            logger.info(f"Done installing vector queries with status {query_res}")
        else:
            logger.info(f"All vector queries already installed, skipping.")

    def set_graphname(self, graphname):
        self.conn.graphname = graphname
        self.vector_attr_cache = {}
        if self.conn.apiToken or self.conn.jwtToken:
            self.conn.getToken()
        # Re-verify GDS presence on every graphname switch. Cheap when
        # the package is already installed (one LS call) and recovers
        # from a mid-flight DROP ALL or admin-side wipe before the
        # per-graph vector-query install below tries to reference
        # missing gds.vector.* UDFs.
        self._ensure_gds_installed()
        if self.conn.graphname and not self.conn.graphname == "MyGraph":
            current_schema = self.conn.gsql(f"USE GRAPH {self.conn.graphname}\n ls")
            if "(Dimension=" in current_schema:
                self.install_vector_queries()

    def set_connection(self, conn):
        if isinstance(conn.apiToken, tuple):
            token = conn.apiToken[0]
        elif isinstance(conn.apiToken, str):
            token = conn.apiToken
        self.conn = TigerGraphConnection(
                host=conn.host,
                username=conn.username,
                password=conn.password,
                graphname=conn.graphname,
                restppPort=conn.restppPort,
                gsPort=conn.gsPort,
                tgCloud=conn.tgCloud,
                certPath=conn.certPath,
                sslPort = conn.sslPort,
                jwtToken = conn.jwtToken,
                apiToken = token,
             )

        self.vector_attr_cache = {}
        self.install_vector_queries()

    def refreshvector_attr_cache(self):
        """Parse the graph schema to discover which vertex types have which
        vector attributes.  Populates ``self.vector_attr_cache`` as
        ``{vertex_type: {attr_name, ...}, ...}``.

        The ``ls`` output contains a section like::

            Vector Embeddings:
            - Person:
              - embedding(Dimension=1536, ...)
            - DocumentChunk:
              - embedding(Dimension=1536, ...)
              - summary_vec(Dimension=768, ...)
        """
        self.vector_attr_cache = {}
        try:
            schema = self.conn.gsql(
                f"USE GRAPH {self.conn.graphname}\n ls"
            )
            in_vector_section = False
            current_vtype = None
            for line in schema.splitlines():
                stripped = line.strip()
                if stripped.startswith("Vector Embeddings:"):
                    in_vector_section = True
                    continue
                if in_vector_section:
                    if stripped == "" or (
                        not stripped.startswith("-") and ":" not in stripped
                    ):
                        break
                    if stripped.startswith("- ") and stripped.endswith(":"):
                        current_vtype = stripped[2:-1].strip()
                        self.vector_attr_cache.setdefault(current_vtype, set())
                    elif current_vtype and "(" in stripped:
                        attr_name = stripped.lstrip("- ").split("(")[0].strip()
                        if attr_name:
                            self.vector_attr_cache[current_vtype].add(attr_name)
        except Exception as e:
            logger.warning(f"Failed to refresh vector attribute cache: {e}")

    def has_vector_attribute(self, vertex_type: str, vector_attribute: str) -> bool:
        """Check whether *vertex_type* has a vector attribute named
        *vector_attribute* according to the cached schema.  The cache is
        refreshed automatically on the first call or after
        ``set_graphname`` / ``set_connection``."""
        if not self.vector_attr_cache:
            self.refreshvector_attr_cache()
        attrs = self.vector_attr_cache.get(vertex_type)
        if attrs is None:
            return False
        return vector_attribute in attrs

    def map_attrs(self, attributes: Iterable[Tuple[str, List[float]]]):
        attrs = {}
        for (k, v) in attributes:
            attrs[k] = {"value": v}
        return attrs

    # Markers an embedding provider raises when input exceeds its context
    # window. Match by substring so we don't depend on a specific SDK
    # exception class (langchain wraps Bedrock / OpenAI / Anthropic errors
    # heterogeneously). Anything else is treated as a transient failure
    # and propagated.
    _EMBED_OVERFLOW_MARKERS = (
        "Too many input tokens",
        "Max input tokens",
        "input too long",
        "maximum context length",
        "context length",
        "InvalidRequestError",
        "ValidationException",
    )

    @classmethod
    def _is_embed_overflow(cls, err: Exception) -> bool:
        msg = str(err)
        return any(m.lower() in msg.lower() for m in cls._EMBED_OVERFLOW_MARKERS)

    @staticmethod
    def _truncation_candidates(text: str) -> List[str]:
        """Yield progressively shorter prefixes when full text overflows.

        The 75/50/25% schedule handles the common case (a chunk slightly
        over the limit) without dropping useful tail content when one
        smaller step would have fit. Final fallback is a hard prefix of
        ~3000 chars which is safely below any modern embedding cap.
        """
        if len(text) <= 4000:
            return [text]
        return [text, text[: len(text) * 75 // 100], text[: len(text) // 2], text[:3000]]

    def _embed_sync_with_truncation_retry(self, text: str, v_id):
        """Sync embed with input-overflow fallback. See aadd_embeddings's
        truncation gatekeeper for the rationale."""
        last_err = None
        for i, candidate in enumerate(self._truncation_candidates(text)):
            try:
                return self.embedding_service.embed_query(candidate)
            except Exception as e:
                last_err = e
                if not self._is_embed_overflow(e):
                    break
                LogWriter.warning(
                    f"Embed for {v_id} overflowed at len={len(candidate)} "
                    f"(attempt {i + 1}); retrying with shorter prefix"
                )
        LogWriter.error(f"Failed to embed {v_id} after truncation: {last_err}")
        return None

    async def _embed_with_truncation_retry(self, text: str, v_id):
        """Async embed with input-overflow fallback.

        Bedrock Titan and similar providers reject inputs over their
        token cap with a 400 error. Chunks larger than expected can
        happen even with chunker-side guards (model-specific token
        counts vary). Rather than abandoning the whole batch on one
        bad chunk, truncate the offending text to progressively
        shorter prefixes until it fits. The persisted chunk text is
        unchanged; only the embedding represents the prefix. A chunk
        for which no truncation level fits is left without an
        embedding — the similarity_search GSQL skips empty vectors.
        """
        last_err = None
        for i, candidate in enumerate(self._truncation_candidates(text)):
            try:
                return await self.embedding_service.aembed_query(candidate)
            except Exception as e:
                last_err = e
                if not self._is_embed_overflow(e):
                    break
                LogWriter.warning(
                    f"Embed for {v_id} overflowed at len={len(candidate)} "
                    f"(attempt {i + 1}); retrying with shorter prefix"
                )
        LogWriter.error(f"Failed to embed {v_id} after truncation: {last_err}")
        return None

    def add_embeddings(
        self,
        embeddings: Iterable[Tuple[Tuple[str, str], List[float]]],
        metadatas: List[dict] = None,
    ):
        """Add Embeddings.
        Add embeddings to the Embedding store.
        Args:
            embeddings (Iterable[Tuple[str, List[float]]]):
                Iterable of content and embedding of the document.
        """
        batch = None
        try:
            LogWriter.info(
                f"request_id={req_id_cv.get()} TigerGraph ENTRY add_embeddings()"
            )

            start_time = time()
            batch = {
                "vertices": defaultdict(dict[str, any]),
            }

            skipped = []
            vec_attrs_used = set()
            for i, (text, _) in enumerate(embeddings):
                (v_id, v_type) = metadatas[i].get("vertex_id")
                vec_attr = metadatas[i].get(
                    "vector_attribute", self.default_vector_attribute
                )
                if not self.has_vector_attribute(v_type, vec_attr):
                    if (v_type, vec_attr) not in skipped:
                        LogWriter.warning(
                            f"Skipping vertex type '{v_type}': "
                            f"no vector attribute '{vec_attr}' in schema."
                        )
                        skipped.append((v_type, vec_attr))
                    continue
                vec_attrs_used.add(vec_attr)
                embedding = self._embed_sync_with_truncation_retry(text, v_id)
                if embedding is None:
                    continue
                attr = self.map_attrs([(vec_attr, embedding)])
                batch["vertices"][v_type][v_id] = attr

            if not any(batch["vertices"].values()):
                LogWriter.warning("No embeddings to upsert after vector attribute checks.")
                return

            data = json.dumps(batch)
            added = self.conn.upsertData(data)

            duration = time() - start_time

            LogWriter.info(f"request_id={req_id_cv.get()} TigerGraph EXIT add_embeddings()")

            if added:
                success_message = f"Document registered with id: {added[0]}"
                LogWriter.info(success_message)
                return success_message
            else:
                error_message = f"Failed to register document {added}"
                LogWriter.error(error_message)
                raise Exception(error_message)

        except Exception as e:
            v_types = list(batch["vertices"].keys()) if batch else "unknown"
            vec_names = vec_attrs_used if vec_attrs_used else {self.default_vector_attribute}
            error_message = (
                f"An error occurred while registering document: {str(e)}. "
                f"Vertex type(s) in batch: {v_types}. "
                f"Vector attribute(s) used: {vec_names}. "
                f"Ensure these vertex types have the expected vector attribute."
            )
            LogWriter.error(error_message)

    async def aadd_embeddings(
        self,
        embeddings: Iterable[Tuple[str, List[float]]],
        metadatas: List[dict] = None,
    ):
        """Add Embeddings.
        Add embeddings to the Embedding store.
        Args:
            embeddings (Iterable[Tuple[str, List[float]]]):
                Iterable of content and embedding of the document.
        """
        batch = None
        try:
            LogWriter.info(
                f"request_id={req_id_cv.get()} TigerGraph ENTRY aadd_embeddings()"
            )

            start_time = time()
            batch = {
                "vertices": defaultdict(dict[str, any]),
            }

            skipped = []
            vec_attrs_used = set()
            for i, (text, _) in enumerate(embeddings):
                (v_id, v_type) = metadatas[i].get("vertex_id")
                vec_attr = metadatas[i].get(
                    "vector_attribute", self.default_vector_attribute
                )
                if not self.has_vector_attribute(v_type, vec_attr):
                    if (v_type, vec_attr) not in skipped:
                        LogWriter.warning(
                            f"Skipping vertex type '{v_type}': "
                            f"no vector attribute '{vec_attr}' in schema."
                        )
                        skipped.append((v_type, vec_attr))
                    continue
                vec_attrs_used.add(vec_attr)
                embedding = await self._embed_with_truncation_retry(text, v_id)
                if embedding is None:
                    # No truncation level worked. Leave this vertex without an
                    # embedding so similarity_search can skip it (the GSQL
                    # query filters on v.embedding.size() > 0) — better than
                    # abandoning the entire batch on one bad chunk.
                    continue
                attr = self.map_attrs([(vec_attr, embedding)])
                batch["vertices"][v_type][v_id] = attr

            if not any(batch["vertices"].values()):
                LogWriter.warning("No embeddings to upsert after vector attribute checks.")
                return

            data = json.dumps(batch)
            added = self.conn.upsertData(data)

            duration = time() - start_time

            LogWriter.info(f"request_id={req_id_cv.get()} TigerGraph EXIT aadd_embeddings()")

            if added:
                success_message = f"Document {metadatas} registered with status: {added}"
                LogWriter.info(success_message)
                return success_message
            else:
                error_message = f"Failed to register document {metadatas} with status {added}"
                LogWriter.error(error_message)
                raise Exception(error_message)

        except Exception as e:
            v_types = list(batch["vertices"].keys()) if batch else "unknown"
            vec_names = vec_attrs_used if vec_attrs_used else {self.default_vector_attribute}
            error_message = (
                f"An error occurred while registering document: {str(e)}. "
                f"Vertex type(s) in batch: {v_types}. "
                f"Vector attribute(s) used: {vec_names}. "
                f"Ensure these vertex types have the expected vector attribute."
            )
            LogWriter.error(error_message)

    def has_embeddings(
        self,
        v_ids: Iterable[Tuple[str, str]]
    ):
        ret = True
        try:
            for (v_id, v_type) in v_ids:
                if not self.has_vector_attribute(v_type, self.default_vector_attribute):
                    logger.info(
                        f"Vertex type '{v_type}' has no vector attribute "
                        f"'{self.default_vector_attribute}', treating as no embedding."
                    )
                    return False
                res = self.conn.runInstalledQuery(
                    "check_embedding_exists",
                    params={
                        "vertex_type": v_type,
                        "vertex_id": v_id,
                    }
                )
                # v_ids carry user-content-derived identifiers; demote.
                logger.debug(f"Return result {res} for has_embeddings({v_ids})")
                found = False
                if "results" in res[0]:
                    for v in res[0]["results"]:
                        if v["v_id"] == v_id:
                            found = True
                            break
                ret = ret and found 
        except Exception as e:
            logger.info(f"Exception {str(e)} when running has_embeddings({v_type}, {v_id}), return False")
            ret = False
        return ret

    def check_embedding_rebuilt(
        self,
        v_type: str
    ):
        if not self.has_vector_attribute(v_type, self.default_vector_attribute):
            logger.info(
                f"Vertex type '{v_type}' has no vector attribute "
                f"'{self.default_vector_attribute}', skipping rebuild check."
            )
            return False
        try:
            res = self.conn.runInstalledQuery(
                "vertices_have_embedding",
                params={
                    "vertex_type": v_type,
                }
            )
            logger.info(f"Return result {res} for all_has_embeddings({v_type})")
            return res[0]["all_have_embedding"]
        except Exception as e:
            logger.info(f"Exception {str(e)} when running check_embedding_rebuilt({v_type}), return False")
        return False

    def remove_embeddings(
        self, ids: Optional[List[str]] = None, expr: Optional[str] = None
    ):
        #TBD
        return

    def retrieve_similar(self, query_embedding, top_k=10, filter_expr: str = None, vertex_types: List[str] = ["DocumentChunk"]):
        res = self.retrieve_similar_with_score(query_embedding, top_k=top_k, filter_expr=filter_expr, vertex_types=vertex_types)
        similar = [x[0] for x in res]
        return similar

    def retrieve_similar_with_score(self, query_embedding, top_k=10, similarity_threshold=0.90, filter_expr: str = None, vertex_types: List[str] = ["DocumentChunk"]):
        """Retireve Similar.
        Retrieve similar embeddings from the vector store given a query embedding.
        Args:
            query_embedding (List[float]):
                The embedding to search with.
            top_k (int, optional):
                The number of documents to return. Defaults to 10.
        Returns:
            https://api.python.langchain.com/en/latest/documents/langchain_core.documents.base.Document.html#langchain_core.documents.base.Document
            Document results for search.
        """
        try:
            logger.info(f"Fetch {top_k} similar entries from {vertex_types} with filter {filter_expr}")

            start_time = time()
            # GSQL declares STRING expr=""; pyTigerGraph rejects null.
            verts = self.conn.runInstalledQuery(
                "get_topk_similar",
                params={
                    "vertex_types": vertex_types,
                    "query_vector": query_embedding,
                    "top_k": top_k*2,
                    "expr": filter_expr or "",
                }
            )
            end_time = time()
            # logger.info(f"Got {top_k} similar entries: {verts}")
            similar = []
            for r in verts:
                if "results" in r:
                    for v in r["results"]:
                        document = Document(
                            page_content="",
                            metadata={"vertex_id": v["v_id"], "vertex_type": v["v_type"]}
                        )
                        similar.append((document, v["score"]))

            similar.sort(key=lambda x: x[1], reverse=True)
            i = 0
            for i in range(len(similar)):
                if similar[i][1] < similarity_threshold:
                    break
            if i <= top_k:
                return similar[:top_k]
            else:
                return similar[:i]
        except Exception as e:
            error_message = f"An error occurred while retrieving docuements: {str(e)}"
            LogWriter.error(error_message)
            raise e

    def add_connection_parameters(self, query_params: dict) -> dict:
        """Add Connection Parameters.
        Add connection parameters to the query parameters.
        Args:
            query_params (dict):
                Dictionary containing the parameters for the GSQL query.
        Returns:
            A dictionary containing the connection parameters.
        """
        # Nothing needed for TG
        return query_params

    async def aget_k_closest(
        self, vertex: Tuple[str, str], k=10, threshold_similarity=0.90, edit_dist_threshold_pct=0.75
    ) -> list[Document]:
        logger.info(f"Fetch {k} closest entries for {vertex} with threshold {threshold_similarity}")
        # Get all vectors with this ID
        (v_id, v_type) = vertex
        verts = self.conn.runInstalledQuery(
            "get_topk_closest",
            params={
                "vertex_type": v_type,
                "vertex_id": v_id,
                "k": k,
                "threshold": threshold_similarity,
            }
        )
        result = []
        for r in verts:
            if "results" in r:
                for v in r["results"]:
                    result.append(v["v_id"])
        logger.info(f"Returning {result}")
        return set(result)

    def list_registered_documents(
        self,
        graphname: str = None,
        only_custom: bool = False,
        output_fields: List[str] = ["*"],
    ):
        if only_custom and graphname:
            pass
        elif only_custom:
            pass
        elif graphname:
            pass
        else:
            pass
        return []

    def query(self, expr: str, output_fields: List[str]):
        """Get output fields with expression

        Args:
            expr: Expression - E.g: "pk > 0"

        Returns:
            List of output fields' contents
        """

        return []

    def __del__(self):
        logger.info("TigerGraphEmbeddingStore destructed.")
