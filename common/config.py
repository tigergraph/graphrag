import json
import os

from fastapi.security import HTTPBasic
from pymilvus.exceptions import MilvusException
from pyTigerGraph import TigerGraphConnection

from common.embeddings.embedding_services import (
    AWS_Bedrock_Embedding,
    AzureOpenAI_Ada002,
    OpenAI_Embedding,
    VertexAI_PaLM_Embedding,
)
from common.embeddings.milvus_embedding_store import MilvusEmbeddingStore
from common.embeddings.tigergraph_embedding_store import TigerGraphEmbeddingStore
from common.llm_services import (
    AWS_SageMaker_Endpoint,
    AWSBedrock,
    AzureOpenAI,
    GoogleVertexAI,
    Groq,
    HuggingFaceEndpoint,
    LLM_Model,
    Ollama,
    OpenAI,
    IBMWatsonX
)
from common.logs.logwriter import LogWriter
from common.session import SessionHandler
from common.status import StatusManager

security = HTTPBasic()
session_handler = SessionHandler()
status_manager = StatusManager()
service_status = {}

# Configs
LLM_SERVICE = os.getenv("LLM_CONFIG", "configs/llm_config.json")
DB_CONFIG = os.getenv("DB_CONFIG", "configs/db_config.json")
MILVUS_CONFIG = os.getenv("MILVUS_CONFIG", "configs/milvus_config.json")
DOC_PROCESSING_CONFIG = os.getenv(
    "DOC_PROCESSING_CONFIG", "configs/doc_processing_config.json"
)
PATH_PREFIX = os.getenv("PATH_PREFIX", "")
PRODUCTION = os.getenv("PRODUCTION", "false").lower() == "true"

if not PATH_PREFIX.startswith("/") and len(PATH_PREFIX) != 0:
    PATH_PREFIX = f"/{PATH_PREFIX}"
if PATH_PREFIX.endswith("/"):
    PATH_PREFIX = PATH_PREFIX[:-1]

if LLM_SERVICE is None:
    raise Exception("LLM_CONFIG environment variable not set")
if DB_CONFIG is None:
    raise Exception("DB_CONFIG environment variable not set")

if LLM_SERVICE[-5:] != ".json":
    try:
        llm_config = json.loads(LLM_SERVICE)
    except Exception as e:
        raise Exception(
            "LLM_CONFIG environment variable must be a .json file or a JSON string, failed with error: "
            + str(e)
        )
else:
    with open(LLM_SERVICE, "r") as f:
        llm_config = json.load(f)

if DB_CONFIG[-5:] != ".json":
    try:
        db_config = json.loads(str(DB_CONFIG))
    except Exception as e:
        raise Exception(
            "DB_CONFIG environment variable must be a .json file or a JSON string, failed with error: "
            + str(e)
        )
else:
    with open(DB_CONFIG, "r") as f:
        db_config = json.load(f)

embedding_store_type = db_config.get("embedding_store", "tigergraph")
reuse_embedding = db_config.get("reuse_embedding", True)

if MILVUS_CONFIG is None or (
    MILVUS_CONFIG.endswith(".json") and not os.path.exists(MILVUS_CONFIG)
):
    milvus_config = {"host": "localhost", "port": "19530", "enabled": "false"}
elif MILVUS_CONFIG.endswith(".json"):
    with open(MILVUS_CONFIG, "r") as f:
        milvus_config = json.load(f)
else:
    try:
        milvus_config = json.loads(str(MILVUS_CONFIG))
    except json.JSONDecodeError as e:
        raise Exception(
            "MILVUS_CONFIG must be a .json file or a JSON string, failed with error: "
            + str(e)
        )

if "model_name" not in llm_config or "model_name" not in llm_config["embedding_service"]:
    if "model_name" not in llm_config:
        llm_config["model_name"] = llm_config["embedding_service"]["model_name"]
    else:
        llm_config["embedding_service"]["model_name"] = llm_config["model_name"]

if llm_config["embedding_service"]["embedding_model_service"].lower() == "openai":
    embedding_service = OpenAI_Embedding(llm_config["embedding_service"])
elif llm_config["embedding_service"]["embedding_model_service"].lower() == "azure":
    embedding_service = AzureOpenAI_Ada002(llm_config["embedding_service"])
elif llm_config["embedding_service"]["embedding_model_service"].lower() == "vertexai":
    embedding_service = VertexAI_PaLM_Embedding(llm_config["embedding_service"])
elif llm_config["embedding_service"]["embedding_model_service"].lower() == "bedrock":
    embedding_service = AWS_Bedrock_Embedding(llm_config["embedding_service"])
else:
    raise Exception("Embedding service not implemented")

def get_llm_service(llm_config) -> LLM_Model:
    if llm_config["completion_service"]["llm_service"].lower() == "openai":
        return OpenAI(llm_config["completion_service"])
    elif llm_config["completion_service"]["llm_service"].lower() == "azure":
        return AzureOpenAI(llm_config["completion_service"])
    elif llm_config["completion_service"]["llm_service"].lower() == "sagemaker":
        return AWS_SageMaker_Endpoint(llm_config["completion_service"])
    elif llm_config["completion_service"]["llm_service"].lower() == "vertexai":
        return GoogleVertexAI(llm_config["completion_service"])
    elif llm_config["completion_service"]["llm_service"].lower() == "bedrock":
        return AWSBedrock(llm_config["completion_service"])
    elif llm_config["completion_service"]["llm_service"].lower() == "groq":
        return Groq(llm_config["completion_service"])
    elif llm_config["completion_service"]["llm_service"].lower() == "ollama":
        return Ollama(llm_config["completion_service"])
    elif llm_config["completion_service"]["llm_service"].lower() == "huggingface":
        return HuggingFaceEndpoint(llm_config["completion_service"])
    elif llm_config["completion_service"]["llm_service"].lower() == "watsonx":
        return IBMWatsonX(llm_config["completion_service"])
    else:
        raise Exception("LLM Completion Service Not Supported")

if os.getenv("INIT_EMBED_STORE", "true") == "true":
    if embedding_store_type == "milvus":
        LogWriter.info(
            f"Milvus enabled for host {milvus_config['host']} at port {milvus_config['port']}"
        )

        try:
            embedding_store = MilvusEmbeddingStore(
                embedding_service,
                host=milvus_config["host"],
                port=milvus_config["port"],
                collection_name="tg_inquiry_documents",
                metric_type=milvus_config.get("metric_type", "COSINE"),
                support_ai_instance=False,
                username=milvus_config.get("username", ""),
                password=milvus_config.get("password", ""),
                alias=milvus_config.get("alias", "default"),
            )
            service_status["embedding_store"] = {"status": "ok", "error": None}
        except MilvusException as e:
            embedding_store = None
            service_status["embedding_store"] = {"status": "milvus error", "error": str(e)}
            raise
        except Exception as e:
            embedding_store = None
            service_status["embedding_store"] = {"status": "embedding error", "error": str(e)}
            raise

        support_collection_name = milvus_config.get("collection_name", "tg_support_documents")
        LogWriter.info(
            f"Setting up Milvus embedding store for SupportAI with collection_name: {support_collection_name}"
        )
        try:
            supportai_embedding_store = MilvusEmbeddingStore(
                embedding_service,
                host=milvus_config["host"],
                port=milvus_config["port"],
                support_ai_instance=True,
                collection_name=support_collection_name,
                metric_type=milvus_config.get("metric_type", "COSINE"),
                username=milvus_config.get("username", ""),
                password=milvus_config.get("password", ""),
                vector_field=milvus_config.get("vector_field", "document_vector"),
                text_field=milvus_config.get("text_field", "document_content"),
                vertex_field=milvus_config.get("vertex_field", "vertex_id"),
                alias=milvus_config.get("alias", "default"),
            )
            service_status["supportai_embedding_store"] = {"status": "ok", "error": None}
        except MilvusException as e:
            supportai_embedding_store = None
            service_status["supportai_embedding_store"] = {"status": "milvus error", "error": str(e)}
            raise
        except Exception as e:
            supportai_embedding_store = None
            service_status["supportai_embedding_store"] = {"status": "embedding error", "error": str(e)}
            raise
    else:
        conn = TigerGraphConnection(
            host=db_config.get("hostname", "http://tigergraph"),
            username=db_config.get("username", "tigergraph"),
            password=db_config.get("password", "tigergraph"),
            gsPort=db_config.get("gsPort", "14240"),
            restppPort=db_config.get("restppPort", "9000"),
        )
        #conn.graphname = db_config.get("supportai_graphname", "tg_support_documents")
        if db_config.get("getToken"):
            conn.getToken()

        embedding_store = TigerGraphEmbeddingStore(
            conn,
            embedding_service,
            support_ai_instance=True,
        )
        supportai_embedding_store = embedding_store
        service_status["embedding_store"] = {"status": "ok", "error": None}
        service_status["supportai_embedding_store"] = {"status": "ok", "error": None}

if DOC_PROCESSING_CONFIG is None or (
    DOC_PROCESSING_CONFIG.endswith(".json")
    and not os.path.exists(DOC_PROCESSING_CONFIG)
):
    doc_processing_config = {
        "chunker": "semantic",
        "chunker_config": {"method": "percentile", "threshold": 0.90},
        "extractor": "llm",
        "extractor_config": {}
    }
elif DOC_PROCESSING_CONFIG.endswith(".json"):
    with open(DOC_PROCESSING_CONFIG, "r") as f:
        doc_processing_config = json.load(f)
else:
    doc_processing_config = json.loads(DOC_PROCESSING_CONFIG)
