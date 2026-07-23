# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>
# for the full license text.

from common.chunkers.base_chunker import BaseChunker
from common.chunkers.separators import TEXT_SEPARATORS
from langchain_text_splitters import RecursiveCharacterTextSplitter

_DEFAULT_CHUNK_SIZE = 2048


class RecursiveChunker(BaseChunker):
    def __init__(self, chunk_size=0, overlap_size=-1):
        self.chunk_size = chunk_size if chunk_size > 0 else _DEFAULT_CHUNK_SIZE
        self.overlap_size = overlap_size if overlap_size >= 0 else self.chunk_size // 8

    def chunk(self, input_string):
        text_splitter = RecursiveCharacterTextSplitter(
            separators=TEXT_SEPARATORS,
            chunk_size=self.chunk_size,
            chunk_overlap=self.overlap_size,
            length_function=len
        )
        return text_splitter.split_text(input_string)

    def __call__(self, input_string):
        return self.chunk(input_string)