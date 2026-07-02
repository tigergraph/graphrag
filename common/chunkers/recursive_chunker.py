# Copyright (c) 2024-2026 TigerGraph, Inc.
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