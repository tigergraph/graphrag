# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

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