# Copyright (c) 2025 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from common.chunkers.base_chunker import BaseChunker
from common.embeddings.embedding_services import EmbeddingModel
from langchain_experimental.text_splitter import (
    SemanticChunker as LangChainSemanticChunker,
)


class SemanticChunker(BaseChunker):
    def __init__(
        self,
        embedding_serivce: EmbeddingModel,
        breakpoint_threshold_type: str = "percentile",
        breakpoint_threshold_amount: float = 0.95,
    ):
        self.emb_model = embedding_serivce
        self.btt = breakpoint_threshold_type
        self.bta = breakpoint_threshold_amount

    def chunk(self, input_string):
        text_splitter = LangChainSemanticChunker(
            self.emb_model.embeddings,
            breakpoint_threshold_type=self.btt,
            breakpoint_threshold_amount=self.bta,
        )

        chunks = text_splitter.create_documents([input_string])

        return [x.page_content for x in chunks]

    def split_documents(self, input_docs, ):
        text_splitter = LangChainSemanticChunker(
            self.emb_model.embeddings,
            breakpoint_threshold_type=self.btt,
            breakpoint_threshold_amount=self.bta,
        )

        chunks = text_splitter.split_documents(input_docs)

        return chunks

    def __call__(self, input_string):
        return self.chunk(input_string)
