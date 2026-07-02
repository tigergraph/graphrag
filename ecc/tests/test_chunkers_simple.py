#!/usr/bin/env python3
"""
Simple test script for testing different chunkers with sample text.
This version focuses on basic chunkers that don't require external dependencies.
"""

import sys
import os

# Add the parent directory to the path to import the modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

def test_character_chunker():
    """Test character-based chunking"""
    try:
        from common.chunkers.character_chunker import CharacterChunker
        
        print("\n" + "="*60)
        print("TESTING CHARACTER CHUNKER")
        print("="*60)
        
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
        
        # Create character chunker
        chunker = CharacterChunker(
            chunk_size=200,
            overlap_size=20
        )
        
        chunks = chunker.chunk(sample_text)
        
        print(f"Character Chunker - Chunk Size: 200, Overlap: 20")
        print(f"Total chunks: {len(chunks)}")
        print(f"Total characters: {sum(len(chunk) for chunk in chunks)}")
        print(f"Original text length: {len(sample_text)}")
        
        for i, chunk in enumerate(chunks):
            print(f"\n--- Chunk {i+1} (Length: {len(chunk)}) ---")
            print(chunk[:150] + "..." if len(chunk) > 150 else chunk)
        
        return True
        
    except Exception as e:
        print(f"Error testing character chunker: {e}")
        return False

def test_regex_chunker():
    """Test regex-based chunking"""
    try:
        from common.chunkers.regex_chunker import RegexChunker
        
        print("\n" + "="*60)
        print("TESTING REGEX CHUNKER")
        print("="*60)
        
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
        
        # Create regex chunker
        chunker = RegexChunker(pattern="\\r?\\n")
        
        chunks = chunker.chunk(sample_text)
        
        print(f"Regex Chunker - Pattern: \\r?\\n (split on newlines)")
        print(f"Total chunks: {len(chunks)}")
        
        for i, chunk in enumerate(chunks):
            if chunk.strip():  # Only show non-empty chunks
                print(f"\n--- Chunk {i+1} (Length: {len(chunk)}) ---")
                print(chunk.strip())
                if len(chunk) > 100:
                    print("...")
        
        return True
        
    except Exception as e:
        print(f"Error testing regex chunker: {e}")
        return False

def test_markdown_chunker():
    """Test markdown-based chunking"""
    try:
        from common.chunkers.markdown_chunker import MarkdownChunker
        
        print("\n" + "="*60)
        print("TESTING MARKDOWN CHUNKER")
        print("="*60)
        
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
        
        # Create markdown chunker
        chunker = MarkdownChunker(
            chunk_size=300,
            chunk_overlap=30
        )
        
        chunks = chunker.chunk(sample_text)
        
        print(f"Markdown Chunker - Chunk Size: 300, Overlap: 30")
        print(f"Total chunks: {len(chunks)}")
        print(f"Total characters: {sum(len(chunk) for chunk in chunks)}")
        print(f"Original text length: {len(sample_text)}")
        
        for i, chunk in enumerate(chunks):
            print(f"\n--- Chunk {i+1} (Length: {len(chunk)}) ---")
            print(chunk[:150] + "..." if len(chunk) > 150 else chunk)
        
        return True
        
    except Exception as e:
        print(f"Error testing markdown chunker: {e}")
        return False

def test_recursive_chunker():
    """Test recursive-based chunking"""
    try:
        from common.chunkers.recursive_chunker import RecursiveChunker
        
        print("\n" + "="*60)
        print("TESTING RECURSIVE CHUNKER")
        print("="*60)
        
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
        
        # Create recursive chunker
        chunker = RecursiveChunker(
            chunk_size=250,
            overlap_size=25
        )
        
        chunks = chunker.chunk(sample_text)
        
        print(f"Recursive Chunker - Chunk Size: 250, Overlap: 25")
        print(f"Total chunks: {len(chunks)}")
        print(f"Total characters: {sum(len(chunk) for chunk in chunks)}")
        print(f"Original text length: {len(sample_text)}")
        
        for i, chunk in enumerate(chunks):
            print(f"\n--- Chunk {i+1} (Length: {len(chunk)}) ---")
            print(chunk[:150] + "..." if len(chunk) > 150 else chunk)
        
        return True
        
    except Exception as e:
        print(f"Error testing recursive chunker: {e}")
        return False

def test_edge_cases():
    """Test chunkers with edge cases"""
    try:
        from common.chunkers.character_chunker import CharacterChunker
        
        print("\n" + "="*60)
        print("TESTING EDGE CASES")
        print("="*60)
        
        chunker = CharacterChunker(chunk_size=100)
        
        # Test with empty string
        empty_text = ""
        print("\n--- Testing with empty string ---")
        
        chunks = chunker.chunk(empty_text)
        print(f"Empty string chunks: {chunks}")
        
        # Test with very short text
        short_text = "Hello"
        print("\n--- Testing with short text ---")
        
        chunks = chunker.chunk(short_text)
        print(f"Short text chunks: {chunks}")
        
        # Test with text exactly chunk size
        exact_text = "A" * 100
        print("\n--- Testing with text exactly chunk size ---")
        
        chunks = chunker.chunk(exact_text)
        print(f"Exact chunk size chunks: {len(chunks)}")
        
        return True
        
    except Exception as e:
        print(f"Error testing edge cases: {e}")
        return False

def main():
    """Main function to run all tests"""
    print("=" * 80)
    print("SIMPLE CHUNKER TESTING")
    print("=" * 80)
    
    results = []
    
    # Test each chunker
    results.append(("Character Chunker", test_character_chunker()))
    results.append(("Regex Chunker", test_regex_chunker()))
    results.append(("Markdown Chunker", test_markdown_chunker()))
    results.append(("Recursive Chunker", test_recursive_chunker()))
    results.append(("Edge Cases", test_edge_cases()))
    
    # Print summary
    print("\n" + "=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)
    
    for test_name, success in results:
        status = "✓ PASS" if success else "✗ FAIL"
        print(f"{test_name}: {status}")
    
    passed = sum(1 for _, success in results if success)
    total = len(results)
    
    print(f"\nOverall: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All tests passed!")
    else:
        print("⚠️  Some tests failed. Check the output above for details.")

if __name__ == "__main__":
    main()

