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

from typing import Optional, List, Tuple
import re
from common.chunkers.base_chunker import BaseChunker
from common.chunkers.separators import TEXT_SEPARATORS
from langchain_text_splitters import HTMLSectionSplitter
from langchain_text_splitters import RecursiveCharacterTextSplitter


_DEFAULT_CHUNK_SIZE = 2048


class HTMLChunker(BaseChunker):
    """
    HTML chunker that splits HTML content into chunks based on header tags.
    
    - Automatically detects which headers (h1-h6) are present in the HTML
    - Uses only the headers that exist in the document for optimal chunking
    - If custom headers are provided, uses those instead of auto-detection
    - Supports chunk_size / overlap_size: when chunk_size > 0, oversized
      header-based chunks are further split with RecursiveCharacterTextSplitter
    - When chunk_size is 0 (default), a fallback of 4096 is used so that
      headerless HTML documents are still split into reasonable chunks
    """

    def __init__(
        self,
        chunk_size: int = 0,
        overlap_size: int = -1,
        headers: Optional[List[Tuple[str, str]]] = None,
    ):
        self.chunk_size = chunk_size if chunk_size > 0 else _DEFAULT_CHUNK_SIZE
        self.overlap_size = overlap_size if overlap_size >= 0 else self.chunk_size // 8
        self.headers = headers

    def _detect_headers(self, html_content: str) -> List[Tuple[str, str]]:
        """
        Automatically detect which header tags (h1-h6) are present in the HTML.
        Returns a list of header tuples for headers that exist in the document.
        """
        # All possible headers in hierarchical order
        all_headers = [
            ("h1", "Header 1"),
            ("h2", "Header 2"),
            ("h3", "Header 3"),
            ("h4", "Header 4"),
            ("h5", "Header 5"),
            ("h6", "Header 6")
        ]
        
        # Detect which headers are actually present in the HTML
        detected_headers = []
        for tag, name in all_headers:
            # Use regex to find header tags (case insensitive)
            pattern = f'<{tag}[\\s>]'
            if re.search(pattern, html_content, re.IGNORECASE):
                detected_headers.append((tag, name))
        
        # If no headers detected, use h1-h3 as fallback
        if not detected_headers:
            detected_headers = [
                ("h1", "Header 1"),
                ("h2", "Header 2"),
                ("h3", "Header 3")
            ]
        
        return detected_headers

    def chunk(self, input_string: str) -> List[str]:
        # Use custom headers if provided, otherwise auto-detect from HTML
        if self.headers:
            headers_to_use = self.headers
        else:
            headers_to_use = self._detect_headers(input_string)
        
        # Use HTMLSectionSplitter with detected/provided headers
        splitter = HTMLSectionSplitter(headers_to_split_on=headers_to_use)
        docs = splitter.split_text(input_string)

        initial_chunks = [doc.page_content for doc in docs]

        if any(len(chunk) > self.chunk_size for chunk in initial_chunks):
            recursive_splitter = RecursiveCharacterTextSplitter(
                separators=TEXT_SEPARATORS,
                chunk_size=self.chunk_size,
                chunk_overlap=self.overlap_size,
            )
            final_chunks = []
            for chunk in initial_chunks:
                if len(chunk) > self.chunk_size:
                    final_chunks.extend(recursive_splitter.split_text(chunk))
                else:
                    final_chunks.append(chunk)
            return final_chunks

        return initial_chunks

    def __call__(self, input_string: str) -> List[str]:
        return self.chunk(input_string)
