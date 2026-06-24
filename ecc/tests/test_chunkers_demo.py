#!/usr/bin/env python3
"""
Demo script to test different chunkers with sample text.
This script can be run directly to see how different chunkers work.
"""

import sys
import os

# Add the parent directory to the path to import the modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from common.chunkers import (
    character_chunker,
    regex_chunker,
    semantic_chunker,
    markdown_chunker,
    recursive_chunker
)


def test_chunkers():
    """Test different chunkers with sample text and print results"""
    
    # Sample text for testing
    sample_text = """# Introduction to GraphRAG

GraphRAG is a powerful framework for building Retrieval-Augmented Generation (RAG) systems using graph databases.

## What is RAG?

Retrieval-Augmented Generation (RAG) is a technique that combines the power of large language models with external knowledge retrieval. It allows AI systems to access and use information that wasn't part of their training data.

## Key Components

1. **Document Ingestion**: Documents are processed and chunked into smaller pieces
2. **Embedding Generation**: Each chunk is converted into a vector representation
3. **Vector Storage**: Embeddings are stored in a vector database for efficient retrieval
4. **Query Processing**: User queries are processed and relevant chunks are retrieved
5. **Response Generation**: The LLM generates responses based on retrieved context

## Benefits

- Improved accuracy through access to current information
- Reduced hallucination by grounding responses in retrieved facts
- Scalable knowledge management
- Cost-effective compared to fine-tuning

This framework provides a robust foundation for building enterprise-grade RAG applications."""

    print("=" * 80)
    print("CHUNKER TESTING DEMO")
    print("=" * 80)
    print(f"Sample text length: {len(sample_text)} characters")
    print("=" * 80)

    # Test 1: Character Chunker
    print("\n" + "=" * 60)
    print("1. CHARACTER CHUNKER")
    print("=" * 60)
    
    char_chunker = character_chunker.CharacterChunker(
        chunk_size=150,
        overlap_size=15
    )
    
    char_chunks = char_chunker.chunk(sample_text)
    print(f"Chunk size: 150, Overlap: 15")
    print(f"Total chunks: {len(char_chunks)}")
    print(f"Total characters: {sum(len(chunk) for chunk in char_chunks)}")
    
    for i, chunk in enumerate(char_chunks):
        print(f"\n--- Chunk {i+1} (Length: {len(chunk)}) ---")
        print(chunk)
        if len(chunk) > 100:
            print("...")

    # Test 2: Regex Chunker
    print("\n" + "=" * 60)
    print("2. REGEX CHUNKER")
    print("=" * 60)
    
    regex_chunker_instance = regex_chunker.RegexChunker(pattern="\\r?\\n")
    regex_chunks = regex_chunker_instance.chunk(sample_text)
    
    print(f"Pattern: \\r?\\n (split on newlines)")
    print(f"Total chunks: {len(regex_chunks)}")
    
    for i, chunk in enumerate(regex_chunks):
        if chunk.strip():  # Only show non-empty chunks
            print(f"\n--- Chunk {i+1} (Length: {len(chunk)}) ---")
            print(chunk.strip())
            if len(chunk) > 100:
                print("...")

    # Test 3: Markdown Chunker
    print("\n" + "=" * 60)
    print("3. MARKDOWN CHUNKER")
    print("=" * 60)
    
    md_chunker = markdown_chunker.MarkdownChunker(
        chunk_size=200,
        chunk_overlap=20
    )
    
    md_chunks = md_chunker.chunk(sample_text)
    print(f"Chunk size: 200, Overlap: 20")
    print(f"Total chunks: {len(md_chunks)}")
    print(f"Total characters: {sum(len(chunk) for chunk in md_chunks)}")
    
    for i, chunk in enumerate(md_chunks):
        print(f"\n--- Chunk {i+1} (Length: {len(chunk)}) ---")
        print(chunk)
        if len(chunk) > 100:
            print("...")

    # Test 4: Recursive Chunker
    print("\n" + "=" * 60)
    print("4. RECURSIVE CHUNKER")
    print("=" * 60)
    
    rec_chunker = recursive_chunker.RecursiveChunker(
        chunk_size=180,
        overlap_size=18
    )
    
    rec_chunks = rec_chunker.chunk(sample_text)
    print(f"Chunk size: 180, Overlap: 18")
    print(f"Total chunks: {len(rec_chunks)}")
    print(f"Total characters: {sum(len(chunk) for chunk in rec_chunks)}")
    
    for i, chunk in enumerate(rec_chunks):
        print(f"\n--- Chunk {i+1} (Length: {len(chunk)}) ---")
        print(chunk)
        if len(chunk) > 100:
            print("...")

    # Test 5: Different configurations comparison
    print("\n" + "=" * 60)
    print("5. CONFIGURATION COMPARISON")
    print("=" * 60)
    
    configs = [
        {"chunk_size": 100, "overlap_size": 10},
        {"chunk_size": 200, "overlap_size": 20},
        {"chunk_size": 300, "overlap_size": 30}
    ]
    
    for config in configs:
        print(f"\n--- Character Chunker: {config} ---")
        chunker = character_chunker.CharacterChunker(**config)
        chunks = chunker.chunk(sample_text)
        
        chunk_lengths = [len(chunk) for chunk in chunks]
        avg_length = sum(chunk_lengths) / len(chunk_lengths) if chunk_lengths else 0
        
        print(f"  Total chunks: {len(chunks)}")
        print(f"  Average chunk length: {avg_length:.1f}")
        print(f"  Min chunk length: {min(chunk_lengths) if chunk_lengths else 0}")
        print(f"  Max chunk length: {max(chunk_lengths) if chunk_lengths else 0}")

    # Test 6: Edge cases
    print("\n" + "=" * 60)
    print("6. EDGE CASES")
    print("=" * 60)
    
    # Empty string
    empty_chunks = char_chunker.chunk("")
    print(f"Empty string: {empty_chunks}")
    
    # Very short text
    short_chunks = char_chunker.chunk("Hello")
    print(f"Short text 'Hello': {short_chunks}")
    
    # Text exactly chunk size
    exact_text = "A" * 150
    exact_chunks = char_chunker.chunk(exact_text)
    print(f"Text exactly 150 chars: {len(exact_chunks)} chunks")
    
    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Character chunks: {len(char_chunks)}")
    print(f"Regex chunks: {len(regex_chunks)}")
    print(f"Markdown chunks: {len(md_chunks)}")
    print(f"Recursive chunks: {len(rec_chunks)}")
    print("=" * 80)


if __name__ == "__main__":
    try:
        test_chunkers()
    except Exception as e:
        print(f"Error running chunker tests: {e}")
        import traceback
        traceback.print_exc()

