import logging
import os
import time
from typing import List

from langchain.schema.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings
from langchain_google_genai import GoogleGenerativeAIEmbeddings

from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter
from common.metrics.prometheus_metrics import metrics

logger = logging.getLogger(__name__)


class EmbeddingModel(Embeddings):
    """EmbeddingModel.
    Implements connections to the desired embedding API.
    """

    def __init__(self, config: dict, model_name: str):
        """Initialize an EmbeddingModel
        Read JSON config file and export the details as environment variables.
        """
        for auth_detail in config["authentication_configuration"].keys():
            os.environ[auth_detail] = config["authentication_configuration"][
                auth_detail
            ]
        self.embeddings = None
        self.model_name = model_name
        LogWriter.info(
            f"request_id={req_id_cv.get()} instantiated AI model_name={model_name}"
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
        super().__init__(config, model_name=config.get("model_name", "OpenAI ada-002"))
        from langchain.embeddings import AzureOpenAIEmbeddings

        self.embeddings = AzureOpenAIEmbeddings(deployment=config["azure_deployment"])


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
        from langchain.embeddings import VertexAIEmbeddings

        self.embeddings = VertexAIEmbeddings(model_name=self.model_name)


class GenAI_Embedding(EmbeddingModel):
    """Google GenAI Embedding Model"""

    def __init__(self, config):
        super().__init__(config, model_name=config.get("model_name", "gemini-embedding-exp-03-07"))

        self.embeddings = GoogleGenerativeAIEmbeddings(model=self.model_name)
        self.dimensions = config.get("dimensions", 1536)



class AWS_Bedrock_Embedding(EmbeddingModel):
    """AWS Bedrock Embedding Model"""

    def __init__(self, config):
        import boto3
        from langchain_community.embeddings import BedrockEmbeddings

        super().__init__(config=config, model_name=config["model_name"])

        client = boto3.client(
            "bedrock-runtime",
            region_name="us-east-1",
            aws_access_key_id=config["authentication_configuration"][
                "AWS_ACCESS_KEY_ID"
            ],
            aws_secret_access_key=config["authentication_configuration"][
                "AWS_SECRET_ACCESS_KEY"
            ],
        )
        self.embeddings = BedrockEmbeddings(client=client)
