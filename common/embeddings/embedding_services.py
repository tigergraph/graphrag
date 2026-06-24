import logging
import os
import time
from typing import List

from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_ollama import OllamaEmbeddings

from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter
from common.metrics.prometheus_metrics import metrics
from common.utils.token_calculator import get_token_calculator

logger = logging.getLogger(__name__)


class EmbeddingModel(Embeddings):
    """EmbeddingModel.
    Implements connections to the desired embedding API.
    """

    def __init__(self, config: dict, model_name: str):
        """Initialize an EmbeddingModel
        Read JSON config file and export the details as environment variables.
        """
        if "authentication_configuration" in config:
            for auth_detail in config["authentication_configuration"].keys():
                os.environ[auth_detail] = config["authentication_configuration"][
                    auth_detail
                ]
        self.embeddings = None
        self.model_name = model_name
        self.dimensions = config.get("dimensions", 1536)
        self.token_calculator = get_token_calculator(token_limit=config.get("token_limit", 8192), model_name=model_name)
        LogWriter.info(
            f"request_id={req_id_cv.get()} instantiated AI model_name={model_name} with dimensions={self.dimensions}"
        )

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Embed Documents.
        Generate embeddings for a list of documents.

        Args:
            texts (List[str]):
                List of documents to embed.
        Returns:
            Nested lists of floats that contain embeddings.
        """
        start_time = time.time()
        metrics.llm_inprogress_requests.labels(self.model_name).inc()

        try:
            LogWriter.info(f"request_id={req_id_cv.get()} ENTRY embed_documents()")

            if not self.token_calculator.is_unlimited_tokens():
                max_context_tokens = self.token_calculator.get_max_context_tokens()
                if any(len(text) > max_context_tokens for text in texts):
                    if any(self.token_calculator.count_tokens(text) > max_context_tokens for text in texts):
                        texts = [self.token_calculator.truncate_to_token_limit(text, max_context_tokens) for text in texts]

            if isinstance(self.embeddings, GoogleGenerativeAIEmbeddings):
                docs = self.embeddings.embed_documents(texts, output_dimensionality=self.dimensions)
            else:
                docs = self.embeddings.embed_documents(texts)
            LogWriter.info(f"request_id={req_id_cv.get()} EXIT embed_documents()")
            metrics.llm_success_response_total.labels(self.model_name).inc()
            return docs
        except Exception as e:
            metrics.llm_query_error_total.labels(self.model_name).inc()
            raise e
        finally:
            metrics.llm_request_total.labels(self.model_name).inc()
            metrics.llm_inprogress_requests.labels(self.model_name).dec()
            duration = time.time() - start_time
            metrics.llm_request_duration_seconds.labels(self.model_name).observe(
                duration
            )

    def embed_query(self, question: str) -> List[float]:
        """Embed Query.
        Embed a string.

        Args:
            question (str):
                A string to embed.
        """
        start_time = time.time()
        metrics.llm_inprogress_requests.labels(self.model_name).inc()

        try:
            LogWriter.info(f"request_id={req_id_cv.get()} ENTRY embed_query()")
            logger.debug_pii(
                f"request_id={req_id_cv.get()} embed_query() embedding question={question}"
            )

            if not self.token_calculator.is_unlimited_tokens():
                max_context_tokens = self.token_calculator.get_max_context_tokens()
                if len(question) > max_context_tokens:
                    if self.token_calculator.count_tokens(question) > max_context_tokens:
                        question = self.token_calculator.truncate_to_token_limit(question, max_context_tokens)

            if isinstance(self.embeddings, GoogleGenerativeAIEmbeddings):
                query_embedding = self.embeddings.embed_query(question, output_dimensionality=self.dimensions)
            else:
                query_embedding = self.embeddings.embed_query(question)
            LogWriter.info(f"request_id={req_id_cv.get()} EXIT embed_query()")
            metrics.llm_success_response_total.labels(self.model_name).inc()
            return query_embedding
        except Exception as e:
            metrics.llm_query_error_total.labels(self.model_name).inc()
            raise e
        finally:
            metrics.llm_request_total.labels(self.model_name).inc()
            metrics.llm_inprogress_requests.labels(self.model_name).dec()
            duration = time.time() - start_time
            metrics.llm_request_duration_seconds.labels(self.model_name).observe(
                duration
            )

    async def aembed_query(self, question: str) -> List[float]:
        """Embed Query Async.
        Embed a string.

        Args:
            question (str):
                A string to embed.
        """
        # start_time = time.time()
        # metrics.llm_inprogress_requests.labels(self.model_name).inc()

        # try:
        LogWriter.info(f"request_id={req_id_cv.get()} ENTRY aembed_query()")
        logger.debug_pii(f"aembed_query() embedding question={question}")
        if not self.token_calculator.is_unlimited_tokens():
            max_context_tokens = self.token_calculator.get_max_context_tokens()
            if len(question) > max_context_tokens:
                if self.token_calculator.count_tokens(question) > max_context_tokens:
                    question = self.token_calculator.truncate_to_token_limit(question, max_context_tokens)

        if isinstance(self.embeddings, GoogleGenerativeAIEmbeddings):
            query_embedding = await self.embeddings.aembed_query(question, output_dimensionality=self.dimensions)
        else:
            query_embedding = await self.embeddings.aembed_query(question)
        LogWriter.info(f"request_id={req_id_cv.get()} EXIT aembed_query()")
        # metrics.llm_success_response_total.labels(self.model_name).inc()
        return query_embedding
        # except Exception as e:
        #     # metrics.llm_query_error_total.labels(self.model_name).inc()
        #     raise e
        # finally:
        #     metrics.llm_request_total.labels(self.model_name).inc()
        #     metrics.llm_inprogress_requests.labels(self.model_name).dec()
        #     duration = time.time() - start_time
        #     metrics.llm_request_duration_seconds.labels(self.model_name).observe(
        #         duration
        #     )


class AzureOpenAI_Ada002(EmbeddingModel):
    """Azure OpenAI Ada-002 Embedding Model"""

    def __init__(self, config):
        super().__init__(config, model_name=config.get("model_name", "text-embedding-3-small"))
        from langchain_openai import AzureOpenAIEmbeddings

        self.embeddings = AzureOpenAIEmbeddings(model=self.model_name, dimensions=self.dimensions, deployment=config["azure_deployment"])


class OpenAI_Embedding(EmbeddingModel):
    """OpenAI Embedding Model"""

    def __init__(self, config):
        super().__init__(
            config, model_name=config.get("model_name", "text-embedding-3-small")
        )

        self.embeddings = OpenAIEmbeddings(model=self.model_name, base_url=config.get("base_url"))


class VertexAI_PaLM_Embedding(EmbeddingModel):
    """VertexAI PaLM Embedding Model"""

    def __init__(self, config):
        super().__init__(config, model_name=config.get("model_name", "VertexAI PaLM"))
        from langchain_google_vertexai import VertexAIEmbeddings

        self.embeddings = VertexAIEmbeddings(model=self.model_name)


class GenAI_Embedding(EmbeddingModel):
    """Google GenAI Embedding Model"""

    def __init__(self, config):
        super().__init__(config, model_name=config.get("model_name", "gemini-embedding-exp-03-07"))

        self.embeddings = GoogleGenerativeAIEmbeddings(model=self.model_name)


class AWS_Bedrock_Embedding(EmbeddingModel):
    """AWS Bedrock Embedding Model"""

    def __init__(self, config):
        import boto3, botocore
        from langchain_aws import BedrockEmbeddings

        super().__init__(config=config, model_name=config.get("model_name", "amazon.titan-embed-text-v1"))

        boto3_config = config.get("boto3_config", {})
        client_config = botocore.config.Config(
            max_pool_connections=boto3_config.get("max_pool_connections", 50),
            read_timeout=boto3_config.get("read_timeout", 300),
            retries={"max_attempts": boto3_config.get("retries", 5)},
        )

        client = boto3.client(
            "bedrock-runtime",
            region_name=config.get("region_name", "us-east-1"),
            config=client_config,
            aws_access_key_id=config["authentication_configuration"][
                "AWS_ACCESS_KEY_ID"
            ],
            aws_secret_access_key=config["authentication_configuration"][
                "AWS_SECRET_ACCESS_KEY"
            ],
        )
        self.embeddings = BedrockEmbeddings(client=client, model_id=self.model_name)


class Ollama_Embedding(EmbeddingModel):
    """Ollama Embedding Model"""

    def __init__(self, config):
        from langchain_ollama import OllamaEmbeddings

        super().__init__(config=config, model_name=config.get("model_name", "llama3"))

        # Get Ollama configuration from config
        base_url = config.get("base_url", "http://localhost:11434")

        self.embeddings = OllamaEmbeddings(
            model=self.model_name,
            base_url=base_url
        )

