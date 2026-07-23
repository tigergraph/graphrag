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
from langchain_text_splitters.markdown import ExperimentalMarkdownSyntaxTextSplitter
from langchain_text_splitters import RecursiveCharacterTextSplitter

# When chunk_size is not configured, cap any heading-section that exceeds this
# so that form-based PDFs (tables/bold but no # headings) are not left as a
# single multi-thousand-character chunk.
_DEFAULT_CHUNK_SIZE = 2048


class MarkdownChunker(BaseChunker):

    def __init__(
        self,
        chunk_size: int = 0,
        overlap_size: int = -1
    ):
        self.chunk_size = chunk_size if chunk_size > 0 else _DEFAULT_CHUNK_SIZE
        self.overlap_size = overlap_size if overlap_size >= 0 else self.chunk_size // 8

    def chunk(self, input_string):
        md_splitter = ExperimentalMarkdownSyntaxTextSplitter()

        # ExperimentalMarkdownSyntaxTextSplitter splits on # headings only.
        # Documents without headings (e.g. form PDFs with tables/bold but no #)
        # are returned as a single section, so a recursive fallback is always
        # applied when any section exceeds the configured (or default) limit.
        initial_chunks = [x.page_content for x in md_splitter.split_text(input_string)]

        if any(len(chunk) > self.chunk_size for chunk in initial_chunks):
            recursive_splitter = RecursiveCharacterTextSplitter(
                separators=TEXT_SEPARATORS,
                chunk_size=self.chunk_size,
                chunk_overlap=self.overlap_size,
            )
            md_chunks = []
            for chunk in initial_chunks:
                if len(chunk) > self.chunk_size:
                    md_chunks.extend(recursive_splitter.split_text(chunk))
                else:
                    md_chunks.append(chunk)
            return md_chunks

        return initial_chunks

    def __call__(self, input_string):
        return self.chunk(input_string)
