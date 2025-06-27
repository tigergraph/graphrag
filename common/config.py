import json
import os

from fastapi.security import HTTPBasic
from pyTigerGraph import TigerGraphConnection

from common.embeddings.embedding_services import (
    AWS_Bedrock_Embedding,
    AzureOpenAI_Ada002,
    OpenAI_Embedding,
    VertexAI_PaLM_Embedding,
    GenAI_Embedding,
)
from common.embeddings.tigergraph_embedding_store import TigerGraphEmbeddingStore
from common.llm_services import (
    AWS_SageMaker_Endpoint,
    AWSBedrock,
    AzureOpenAI,
    GoogleVertexAI,
    GoogleGenAI,
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
SERVER_CONFIG = os.getenv("SERVER_CONFIG", "configs/server_config.json")
PATH_PREFIX = os.getenv("PATH_PREFIX", "")
PRODUCTION = os.getenv("PRODUCTION", "false").lower() == "true"

if not PATH_PREFIX.startswith("/") and len(PATH_PREFIX) != 0:
    PATH_PREFIX = f"/{PATH_PREFIX}"
if PATH_PREFIX.endswith("/"):
    PATH_PREFIX = PATH_PREFIX[:-1]

if SERVER_CONFIG is None:
    raise Exception("SERVER_CONFIG environment variable not set")

if SERVER_CONFIG[-5:] != ".json":
    try:
        server_config = json.loads(str(SERVER_CONFIG))
    except Exception as e:
        raise Exception(
            "SERVER_CONFIG environment variable must be a .json file or a JSON string, failed with error: "
            + str(e)
        )
else:
    with open(SERVER_CONFIG, "r") as f:
        server_config = json.load(f)

db_config = server_config.get("db_config")
llm_config = server_config.get("llm_config")
graphrag_config = server_config.get("graphrag_config")

if db_config is None:
    raise Exception("graphrag_config is not found in SERVER_CONFIG")
if llm_config is None:
    raise Exception("graphrag_config is not found in SERVER_CONFIG")

if graphrag_config is None:
    graphrag_config = {"reuse_embedding", true}
if "chunker" not in graphrag_config:
    graphrag_config["chunker"] = "semantic"
if "extractor" not in graphrag_config:
    graphrag_config["extractor"] = "llm"

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
elif llm_config["embedding_service"]["embedding_model_service"].lower() == "genai":
    embedding_service = GenAI_Embedding(llm_config["embedding_service"])
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
    elif llm_config["completion_service"]["llm_service"].lower() == "genai":
        return GoogleGenAI(llm_config["completion_service"])
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
    conn = TigerGraphConnection(
        host=db_config.get("hostname", "http://tigergraph"),
        username=db_config.get("username", "tigergraph"),
        password=db_config.get("password", "tigergraph"),
        gsPort=db_config.get("gsPort", "14240"),
        restppPort=db_config.get("restppPort", "9000"),
    )
    if db_config.get("getToken"):
        conn.getToken()

    embedding_store = TigerGraphEmbeddingStore(
        conn,
        embedding_service,
        support_ai_instance=True,
    )
    service_status["embedding_store"] = {"status": "ok", "error": None}
