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
