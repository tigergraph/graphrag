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

import enum
from typing import Dict, List, Optional, Union

from pydantic import BaseModel, Field


class NaturalLanguageQuery(BaseModel):
    query: str
    # Engine: "agentic" | "classic" | None (defer to graph config).
    mode: Optional[str] = None
    # Single menu value: agent style ("auto"|"planned"|"reactive") when agentic,
    # or retriever ("auto"|<name>) when classic.
    rag_method: Optional[str] = None
    # Optional response fields beyond the answer. None/empty -> answer only;
    # name fields (e.g. "query_sources") or "all" to include the supporting
    # sources / trace in the response.
    include_fields: Optional[List[str]] = Field(default=None)


class SupportAIQuestion(BaseModel):
    question: str
    method: str = "hybrid"
    method_params: dict = {}


class SupportAIMethod(enum.StrEnum):
    SUPPORTAI = enum.auto()
    GRAPHRAG = enum.auto()


class GSQLQueryInfo(BaseModel):
    function_header: str
    description: str
    docstring: str
    param_types: dict = {}
    graphname: str = "all"


class GSQLQueryList(BaseModel):
    queries: List[str]


class GraphRAGResponse(BaseModel):
    natural_language_response: str
    answered_question: bool
    response_type: str
    query_sources: Dict = None


# --- Agentic engine (v2.0 deep-thinking mode) ------------------------------

class PlanStep(BaseModel):
    """One step in an agentic plan DAG.

    ``kind`` is advisory; ``tool`` is the registry tool name actually run.
    ``arg_bindings`` maps an arg name to ``"<step_id>.<dotted.path>"`` and is
    resolved from earlier ``StepResult`` contexts just before the call — this
    is how a later structural/unstructured step consumes an earlier one.
    """
    id: str
    kind: str = "unstructured"   # schema | structural | unstructured | answer
    tool: str
    args: Dict = {}
    arg_bindings: Dict[str, str] = {}
    depends_on: List[str] = []
    rationale: str = ""


class Plan(BaseModel):
    steps: List[PlanStep] = []
    strategy: str = ""           # one-line, user-facing summary


class StepResult(BaseModel):
    step_id: str
    ok: bool
    summary: str = ""
    context: Optional[object] = None
    citations: List[Dict] = []


class BatchDocumentIngest(BaseModel):
    service: str
    service_params: dict
    chunker: str = None
    chunker_params: dict = None


class S3BatchDocumentIngest(BatchDocumentIngest):
    service: str = "s3"
    service_params: dict = {
        "bucket": str,
        "key": str,
        "type": str,
        "aws_access_key_id": str,
        "aws_secret_access_key": str,
    }


class GoogleBatchDocumentIngest(BatchDocumentIngest):
    service: str = "s3"
    service_params: dict = {
        "bucket": str,
        "key": str,
        "type": str,
        "google_credentials": str,
    }


class AzureBatchDocumentIngest(BatchDocumentIngest):
    service: str = "s3"
    service_params: dict = {
        "bucket": str,
        "key": str,
        "type": str,
        "azure_connection_string": str,
    }


class DocumentChunk(BaseModel):
    document_chunk_id: str
    text: str
    chunk_embedding: List[float] = None
    entities: List[Dict] = None
    relationships: List[Dict] = None
    # Set by the page- and structure-aware chunker (v2.0). None for chunks
    # written by the legacy char-count chunkers.
    chunk_kind: str = None
    page_no: int = None
    under_heading: str = None
    continues_from_page: int = None
    continues_to_page: int = None


class Document(BaseModel):
    document_id: str
    text: str
    document_embedding: List[float] = None
    document_chunks: List[DocumentChunk] = None
    entities: List[Dict] = None
    relationships: List[Dict] = None
    document_collection: str = None


class CreateVectorIndexConfig(BaseModel):
    index_name: str
    vertex_types: List[str]
    M: int = 20
    ef_construction: int = 128


class CreateIngestConfig(BaseModel):
    data_source: str
    data_source_config: Dict
    loader_config: Optional[Dict] = None  # Made optional - will auto-generate defaults
    file_format: str = "json"


class LoadingInfo(BaseModel):
    load_job_id: str
    data_source_id: Union[str, Dict]
    file_path: str


class QueryDeleteRequest(BaseModel):
    ids: Optional[Union[str, List[str]]]
    expr: Optional[str]


class QueryUpsertRequest(BaseModel):
    id: Optional[str]
    query_info: Optional[GSQLQueryInfo]


class MessageContext(BaseModel):
    # TODO: fix this to contain proper message context
    user: str
    content: str


class ReportQuestions(BaseModel):
    question: str
    reasoning: str


class ReportSection(BaseModel):
    section_name: str
    description: str
    questions: Optional[List[ReportQuestions]] = None
    graphrag_fortify: bool = True
    actions: Optional[List[str]] = None


class ReportCreationRequest(BaseModel):
    topic: str
    sections: Union[List[ReportSection], str] = None
    draft_iterations: int = 1
    persona: Optional[str] = None
    conversation_id: Optional[str] = None
    message_context: Optional[List[MessageContext]] = None


class Role(enum.StrEnum):
    SYSTEM = enum.auto()
    USER = enum.auto()


class Message(BaseModel):
    conversation_id: str
    message_id: str
    parent_id: Optional[str] = None
    model: Optional[str] = None
    content: Optional[str] = None
    answered_question: Optional[bool] = False
    response_type: Optional[str] = None
    query_sources: Optional[Dict] = None
    role: Optional[str] = None
    response_time: Optional[float] = None  # time in fractional seconds
    feedback: Optional[int] = None
    comment: Optional[str] = None


class ResponseType(enum.StrEnum):
    PROGRESS = enum.auto()
    MESSAGE = enum.auto()


class AgentProgess(BaseModel):
    content: str
    response_type: ResponseType
