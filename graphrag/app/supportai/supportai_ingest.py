from common.metrics.tg_proxy import TigerGraphConnectionProxy
from common.storage.azure_blob_store import AzureBlobStore
from common.storage.google_blob_store import GoogleBlobStore
from common.storage.s3_blob_store import S3BlobStore
from common.py_schemas import BatchDocumentIngest, Document, DocumentChunk, KnowledgeGraph
from typing import List, Union
import json
import logging
import re
from datetime import datetime
from common.status import Status, IngestionProgress
from common.extractors import LLMEntityRelationshipExtractor

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser

logger = logging.getLogger(__name__)


def _process_id(v_id: str) -> str:
    has_func = re.compile(r"(.*)\(").findall(v_id)
    if has_func:
        v_id = has_func[0]
    v_id = v_id.replace(" ", "_").lower().replace("/", "_").replace("(", "").replace(")", "")
    if v_id in ("''", '""'):
        return ""
    return v_id


class BaseIngestion:
    def __init__(
        self,
        embedding_service,
        llm_service,
        conn: TigerGraphConnectionProxy,
        status: Status,
    ):
        self.embedding_service = embedding_service
        self.llm_service = llm_service
        self.conn = conn
        self.status = status

    def chunk_documents(self, documents, chunker, chunker_params):
        for doc in documents:
            doc.document_chunks = self.chunk_document(doc, chunker, chunker_params)

    def chunk_document(self, document, chunker, chunker_params):
        if chunker.lower() == "regex":
            from common.chunkers.regex_chunker import RegexChunker

            chunker = RegexChunker(chunker_params["pattern"])
        elif chunker.lower() == "characters":
            from common.chunkers.character_chunker import CharacterChunker

            chunker = CharacterChunker(
                chunker_params.get("chunk_size", 0), chunker_params.get("overlap_size", -1)
            )
        elif chunker.lower() == "semantic":
            from common.chunkers.semantic_chunker import SemanticChunker

            chunker = SemanticChunker(
                self.embedding_service,
                chunker_params.get("breakpoint_threshold_type", "percentile"),
                chunker_params.get("breakpoint_threshold_amount", 0.95),
            )
        elif chunker.lower() == "html":
            from common.chunkers.html_chunker import HTMLChunker

            chunker = HTMLChunker(
                chunk_size=chunker_params.get("chunk_size", 0),
                overlap_size=chunker_params.get("overlap_size", -1),
                headers=chunker_params.get("headers", None),
            )
        elif chunker.lower() == "markdown":
            from common.chunkers.markdown_chunker import MarkdownChunker

            chunker = MarkdownChunker(
                chunk_size=chunker_params.get("chunk_size", 0),
                overlap_size=chunker_params.get("overlap_size", -1)
            )
        else:
            raise ValueError(f"Chunker {chunker} not supported")

        chunks = chunker(document.text)
        chunks = [
            DocumentChunk(
                document_chunk_id=_process_id(f"{document.document_id}_chunk_{i}"), text=chunk
            )
            for i, chunk in enumerate(chunks)
        ]
        return chunks

    def document_er_extraction(self, document: Union[Document, DocumentChunk]):
        extractor = LLMEntityRelationshipExtractor(self.llm_service)
        return extractor.extract(document)

    def documents_er_extraction(self, documents: List[Document]):
        for doc in documents:
            self.document_er_extraction(doc)

    def upsert_documents(self, documents: List[Document]):
        for doc in documents:
            self.upsert_document(doc)

    def upsert_chunk(self, chunk: DocumentChunk, doc_id: str):
        now = datetime.now()
        date_added = now.strftime("%Y-%m-%d %H:%M:%S")
        chunk_id = chunk.document_chunk_id
        # ``doc_id`` is the caller's already-lowercased Document id
        # (lowercase-only, no normalization); chunk ids stay process_id-
        # normalized. Use it for the Document endpoints, not the chunk prefix.
        self.status.progress.chunk_failures[chunk_id] = []
        try:
            self.conn.upsertVertex(
                "DocumentChunk",
                chunk_id,
                attributes={
                    "embedding": chunk.chunk_embedding,
                    "date_added": date_added,
                    "idx": int(chunk_id.split("_")[-1]),
                },
            )
            self.conn.upsertVertex(
                "Content",
                chunk_id,
                attributes={"text": chunk.text, "date_added": date_added},
            )
            self.conn.upsertEdge(
                "DocumentChunk", chunk_id, "HAS_CONTENT", "Content", chunk_id
            )
            self.conn.upsertEdge(
                "Document", doc_id, "HAS_CHILD", "DocumentChunk", chunk_id
            )
            idx = int(chunk_id.split("_")[-1])
            if idx > 0:
                self.conn.upsertEdge(
                    "DocumentChunk",
                    chunk_id,
                    "IS_AFTER",
                    "DocumentChunk",
                    _process_id(f"{doc_id}_chunk_{idx - 1}"),
                )
        except Exception as e:
            logger.error(f"Failed to upsert chunk '{chunk_id}' (doc '{doc_id}'): {e}", exc_info=True)
            self.status.progress.chunk_failures[chunk_id].append(e)

        if chunk.entities != []:
            try:
                self.conn.upsertVertices(
                    "Entity",
                    [
                        (
                            _process_id(x["id"]),
                            {
                                "definition": x["definition"],
                                "date_added": date_added,
                            },
                        )
                        for x in chunk.entities
                    ],
                )
                self.conn.upsertEdges(
                    "DocumentChunk",
                    "CONTAINS_ENTITY",
                    "Entity",
                    [(chunk_id, _process_id(x["id"]), {}) for x in chunk.entities],
                )
            except Exception as e:
                logger.error(f"Failed to upsert entities for chunk '{chunk_id}': {e}", exc_info=True)
                self.status.progress.chunk_failures[chunk_id].append(e)

        if chunk.relationships != []:
            try:
                self.conn.upsertVertices(
                    "RelationshipType",
                    [
                        (
                            _process_id(x["source"] + ":" + x["type"] + ":" + x["target"]),
                            {
                                "definition": x["definition"],
                                "short_name": x["type"],
                                "date_added": date_added,
                            },
                        )
                        for x in chunk.relationships
                    ],
                )
                # IS_HEAD_OF / HAS_TAIL live at the meta-schema layer
                # (EntityType ↔ RelationshipType) — they are NOT written
                # per-relationship-instance here. The schema-aware ECC
                # path writes them when it knows the EntityType for the
                # source / target. Legacy supportai chunks without
                # entity_type info skip the meta-layer edges.
                self.conn.upsertEdges(
                    "DocumentChunk",
                    "MENTIONS_RELATIONSHIP",
                    "RelationshipType",
                    [
                        (
                            chunk_id,
                            _process_id(x["source"] + ":" + x["type"] + ":" + x["target"]),
                            {},
                        )
                        for x in chunk.relationships
                    ],
                )
            except Exception as e:
                logger.error(f"Failed to upsert relationships for chunk '{chunk_id}': {e}", exc_info=True)
                self.status.progress.chunk_failures[chunk_id].append(e)

    def upsert_document(self, document: Document):
        now = datetime.now()
        date_added = now.strftime("%Y-%m-%d %H:%M:%S")
        doc_id = document.document_id.lower()
        doc_collection = document.document_collection
        self.status.progress.doc_failures[doc_id] = []
        try:
            self.conn.upsertVertex(
                "Document",
                doc_id,
                attributes={"date_added": date_added},
            )
            self.conn.upsertVertex(
                "Content",
                doc_id,
                attributes={"text": document.text, "date_added": date_added},
            )
            self.conn.upsertEdge("Document", doc_id, "HAS_CONTENT", "Content", doc_id)
        except Exception as e:
            logger.error(f"Failed to upsert document '{doc_id}': {e}", exc_info=True)
            self.status.progress.doc_failures[doc_id].append(e)

        if document.entities != []:
            try:
                self.conn.upsertVertices(
                    "Entity",
                    [
                        (
                            _process_id(x["id"]),
                            {
                                "definition": x["definition"],
                                "date_added": date_added,
                            },
                        )
                        for x in document.entities
                    ],
                )
                self.conn.upsertEdges(
                    "Document",
                    "CONTAINS_ENTITY",
                    "Entity",
                    [(doc_id, _process_id(x["id"]), {}) for x in document.entities],
                )
            except Exception as e:
                logger.error(f"Failed to upsert entities for document '{doc_id}': {e}", exc_info=True)
                self.status.progress.doc_failures[doc_id].append(e)

        if document.relationships != []:
            try:
                self.conn.upsertVertices(
                    "RelationshipType",
                    [
                        (
                            _process_id(x["source"] + ":" + x["type"] + ":" + x["target"]),
                            {
                                "definition": x["definition"],
                                "short_name": x["type"],
                                "date_added": date_added,
                            },
                        )
                        for x in document.relationships
                    ],
                )
                # IS_HEAD_OF / HAS_TAIL are meta-schema edges between
                # EntityType and RelationshipType — see chunk path
                # comment above. Legacy document-level supportai ingest
                # writes only MENTIONS_RELATIONSHIP from Document to
                # RelationshipType.
                self.conn.upsertEdges(
                    "Document",
                    "MENTIONS_RELATIONSHIP",
                    "RelationshipType",
                    [
                        (doc_id, _process_id(x["source"] + ":" + x["type"] + ":" + x["target"]), {})
                        for x in document.relationships
                    ],
                )
            except Exception as e:
                logger.error(f"Failed to upsert relationships for document '{doc_id}': {e}", exc_info=True)
                self.status.progress.doc_failures[doc_id].append(e)


class BatchIngestion(BaseIngestion):
    def __init__(
        self,
        embedding_service,
        llm_service,
        conn: TigerGraphConnectionProxy,
        status: Status,
    ):
        super().__init__(
            embedding_service=embedding_service,
            llm_service=llm_service,
            conn=conn,
            status=status,
        )

    def _ingest(self, documents: List[Document], chunker, chunker_params):
        self.chunk_documents(documents, chunker, chunker_params)
        self.status.progress.num_chunks_in_doc = {
            doc.document_id: len(doc.document_chunks) for doc in documents
        }
        for doc in documents:
            # Document id is lowercase-only; compute once per document.
            doc_id = doc.document_id.lower()
            res = self.document_er_extraction(doc)
            doc.entities = res["nodes"]
            doc.relationships = res["rels"]
            if doc.document_chunks:
                for chunk in doc.document_chunks:
                    chunk.chunk_embedding = self.embedding_service.embed_query(
                        chunk.text
                    )
                    res = self.document_er_extraction(chunk)
                    chunk.entities = res["nodes"]
                    chunk.relationships = res["rels"]
                    self.upsert_chunk(chunk, doc_id)
            self.upsert_document(doc)
            self.status.progress.num_docs_ingested += 1
        self.status.status = "complete"
        return self.status.to_dict()

    def ingest_blobs(self, doc_source: BatchDocumentIngest):
        if doc_source.service == "s3":
            blob_store = S3BlobStore(
                doc_source.service_params["aws_access_key_id"],
                doc_source.service_params["aws_secret_access_key"],
            )
        elif doc_source.service == "google":
            blob_store = GoogleBlobStore(
                doc_source.service_params["google_credentials"]
            )
        elif doc_source.service == "azure":
            blob_store = AzureBlobStore(
                doc_source.service_params["azure_connection_string"]
            )
        elif doc_source.service == "local":
            raise ValueError("Local service should use direct file processing, not blob store")
        else:
            raise ValueError(f"Service {doc_source.service} not supported")

        # get the list of documents
        documents = []
        if doc_source.service_params["type"].lower() == "file":
            content = blob_store.read_document(
                doc_source.service_params["bucket"], doc_source.service_params["key"]
            )
            doc = Document(document_id=doc_source.service_params["key"], text=content)
            documents = [doc]
        elif doc_source.service_params["type"].lower() == "directory":
            keys = blob_store.list_documents(
                doc_source.service_params["bucket"], doc_source.service_params["key"]
            )
            for key in keys:
                content = blob_store.read_document(
                    doc_source.service_params["bucket"], key
                )
                doc = Document(
                    document_id=key,
                    text=content,
                    document_collection=doc_source.service_params["bucket"]
                    + "_"
                    + doc_source.service_params["key"],
                )
                documents.append(doc)
        else:
            raise ValueError(f"Type {doc_source.service_params['type']} not supported")

        self.status.progress = IngestionProgress(num_docs=len(documents))
        return self._ingest(documents, doc_source.chunker, doc_source.chunker_params)
