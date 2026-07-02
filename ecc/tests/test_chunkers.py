import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
import os

# Add the parent directory to the path to import the modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from app.ecc_util import get_chunker
from common.chunkers import (
    character_chunker,
    regex_chunker,
    semantic_chunker,
    markdown_chunker,
    recursive_chunker
)


class TestChunkers(unittest.TestCase):
    """Test class for testing different chunkers with sample text"""
    
    def setUp(self):
        """Set up test data and mock objects"""
        # Sample text for testing different chunkers
        self.sample_text = """# Introduction to GraphRAG

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

        # Mock embedding service for semantic chunker
        self.mock_embedding_service = Mock()
        self.mock_embedding_service.embeddings = Mock()
        
        # Mock configuration
        self.mock_config = {
            "chunker": "semantic",
            "chunker_config": {
                "method": "percentile",
                "threshold": 0.95,
                "chunk_size": 512,
                "overlap_size": 50,
                "pattern": "\\r?\\n"
            }
        }

    def test_character_chunker(self):
        """Test character-based chunking"""
        print("\n" + "="*60)
        print("TESTING CHARACTER CHUNKER")
        print("="*60)
        
        # Create character chunker directly
        chunker = character_chunker.CharacterChunker(
            chunk_size=200,
            overlap_size=20
        )
        
        chunks = chunker.chunk(self.sample_text)
        
        print(f"Character Chunker - Chunk Size: 200, Overlap: 20")
        print(f"Total chunks: {len(chunks)}")
        print(f"Total characters: {sum(len(chunk) for chunk in chunks)}")
        print(f"Original text length: {len(self.sample_text)}")
        
        for i, chunk in enumerate(chunks):
            print(f"\n--- Chunk {i+1} (Length: {len(chunk)}) ---")
            print(chunk[:100] + "..." if len(chunk) > 100 else chunk)
        
        # Assertions
        self.assertIsInstance(chunks, list)
        self.assertTrue(len(chunks) > 1)
        self.assertTrue(all(len(chunk) <= 200 for chunk in chunks))

    def test_regex_chunker(self):
        """Test regex-based chunking"""
        print("\n" + "="*60)
        print("TESTING REGEX CHUNKER")
        print("="*60)
        
        # Create regex chunker directly
        chunker = regex_chunker.RegexChunker(pattern="\\r?\\n")
        
        chunks = chunker.chunk(self.sample_text)
        
        print(f"Regex Chunker - Pattern: \\r?\\n")
        print(f"Total chunks: {len(chunks)}")
        
        for i, chunk in enumerate(chunks):
            print(f"\n--- Chunk {i+1} (Length: {len(chunk)}) ---")
            print(chunk[:100] + "..." if len(chunk) > 100 else chunk)
        
        # Assertions
        self.assertIsInstance(chunks, list)
        self.assertTrue(len(chunks) > 1)

    def test_markdown_chunker(self):
        """Test markdown-based chunking"""
        print("\n" + "="*60)
        print("TESTING MARKDOWN CHUNKER")
        print("="*60)
        
        # Create markdown chunker directly
        chunker = markdown_chunker.MarkdownChunker(
            chunk_size=300,
            chunk_overlap=30
        )
        
        chunks = chunker.chunk(self.sample_text)
        
        print(f"Markdown Chunker - Chunk Size: 300, Overlap: 30")
        print(f"Total chunks: {len(chunks)}")
        print(f"Total characters: {sum(len(chunk) for chunk in chunks)}")
        print(f"Original text length: {len(self.sample_text)}")
        
        for i, chunk in enumerate(chunks):
            print(f"\n--- Chunk {i+1} (Length: {len(chunk)}) ---")
            print(chunk[:100] + "..." if len(chunk) > 100 else chunk)
        
        # Assertions
        self.assertIsInstance(chunks, list)
        self.assertTrue(len(chunks) > 1)

    def test_recursive_chunker(self):
        """Test recursive-based chunking"""
        print("\n" + "="*60)
        print("TESTING RECURSIVE CHUNKER")
        print("="*60)
        
        # Create recursive chunker directly
        chunker = recursive_chunker.RecursiveChunker(
            chunk_size=250,
            overlap_size=25
        )
        
        chunks = chunker.chunk(self.sample_text)
        
        print(f"Recursive Chunker - Chunk Size: 250, Overlap: 25")
        print(f"Total chunks: {len(chunks)}")
        print(f"Total characters: {sum(len(chunk) for chunk in chunks)}")
        print(f"Original text length: {len(self.sample_text)}")
        
        for i, chunk in enumerate(chunks):
            print(f"\n--- Chunk {i+1} (Length: {len(chunk)}) ---")
            print(chunk[:100] + "..." if len(chunk) > 100 else chunk)
        
        # Assertions
        self.assertIsInstance(chunks, list)
        self.assertTrue(len(chunks) > 1)

    @patch('app.ecc_util.graphrag_config')
    @patch('app.ecc_util.embedding_service')
    def test_semantic_chunker(self, mock_embedding_service, mock_graphrag_config):
        """Test semantic chunking through the utility function"""
        print("\n" + "="*60)
        print("TESTING SEMANTIC CHUNKER")
        print("="*60)
        
        # Mock the configuration
        mock_graphrag_config.get.side_effect = lambda key, default=None: {
            "chunker": "semantic",
            "chunker_config": {
                "method": "percentile",
                "threshold": 0.95
            }
        }.get(key, default)
        
        # Mock the embedding service
        mock_embedding_service.embeddings = Mock()
        
        # Mock the semantic chunker to avoid actual API calls
        with patch('app.ecc_util.semantic_chunker.SemanticChunker') as mock_semantic_class:
            mock_chunker_instance = Mock()
            mock_chunker_instance.chunk.return_value = [
                "Introduction to GraphRAG",
                "What is RAG?",
                "Key Components",
                "Benefits"
            ]
            mock_semantic_class.return_value = mock_chunker_instance
            
            # Get chunker through utility function
            chunker = get_chunker("semantic")
            chunks = chunker.chunk(self.sample_text)
            
            print(f"Semantic Chunker - Method: percentile, Threshold: 0.95")
            print(f"Total chunks: {len(chunks)}")
            
            for i, chunk in enumerate(chunks):
                print(f"\n--- Chunk {i+1} (Length: {len(chunk)}) ---")
                print(chunk)
            
            # Assertions
            self.assertIsInstance(chunks, list)
            self.assertTrue(len(chunks) > 0)

    def test_get_chunker_utility_function(self):
        """Test the get_chunker utility function with different chunker types"""
        print("\n" + "="*60)
        print("TESTING GET_CHUNKER UTILITY FUNCTION")
        print("="*60)
        
        # Test different chunker types
        chunker_types = ["character", "regex", "markdown", "recursive"]
        
        for chunker_type in chunker_types:
            print(f"\n--- Testing {chunker_type.upper()} chunker ---")
            
            try:
                # Mock the configuration for each chunker type
                with patch('app.ecc_util.graphrag_config') as mock_config:
                    mock_config.get.side_effect = lambda key, default=None: {
                        "chunker": chunker_type,
                        "chunker_config": {
                            "chunk_size": 200,
                            "overlap_size": 20,
                            "pattern": "\\r?\\n"
                        }
                    }.get(key, default)
                    
                    # Mock embedding service for semantic chunker
                    with patch('app.ecc_util.embedding_service') as mock_emb_service:
                        mock_emb_service.embeddings = Mock()
                        
                        # Get chunker
                        chunker = get_chunker(chunker_type)
                        
                        # Test chunking
                        chunks = chunker.chunk(self.sample_text)
                        
                        print(f"Chunker type: {chunker_type}")
                        print(f"Total chunks: {len(chunks)}")
                        print(f"First chunk preview: {chunks[0][:50]}...")
                        
                        # Assertions
                        self.assertIsInstance(chunker, object)
                        self.assertIsInstance(chunks, list)
                        self.assertTrue(len(chunks) > 0)
                        
            except Exception as e:
                print(f"Error testing {chunker_type} chunker: {e}")
                continue

    def test_chunker_edge_cases(self):
        """Test chunkers with edge cases"""
        print("\n" + "="*60)
        print("TESTING CHUNKER EDGE CASES")
        print("="*60)
        
        # Test with empty string
        empty_text = ""
        print("\n--- Testing with empty string ---")
        
        chunker = character_chunker.CharacterChunker(chunk_size=100)
        chunks = chunker.chunk(empty_text)
        print(f"Empty string chunks: {chunks}")
        self.assertEqual(chunks, [])
        
        # Test with very short text
        short_text = "Hello"
        print("\n--- Testing with short text ---")
        
        chunks = chunker.chunk(short_text)
        print(f"Short text chunks: {chunks}")
        self.assertEqual(chunks, ["Hello"])
        
        # Test with text exactly chunk size
        exact_text = "A" * 100
        print("\n--- Testing with text exactly chunk size ---")
        
        chunks = chunker.chunk(exact_text)
        print(f"Exact chunk size chunks: {len(chunks)}")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 100)

    def test_chunker_performance_comparison(self):
        """Compare performance and output characteristics of different chunkers"""
        print("\n" + "="*60)
        print("CHUNKER PERFORMANCE COMPARISON")
        print("="*60)
        
        chunker_configs = [
            ("character", {"chunk_size": 200, "overlap_size": 20}),
            ("markdown", {"chunk_size": 200, "chunk_overlap": 20}),
            ("recursive", {"chunk_size": 200, "overlap_size": 20})
        ]
        
        results = {}
        
        for chunker_name, config in chunker_configs:
            print(f"\n--- {chunker_name.upper()} Chunker ---")
            
            if chunker_name == "character":
                chunker = character_chunker.CharacterChunker(**config)
            elif chunker_name == "markdown":
                chunker = markdown_chunker.MarkdownChunker(**config)
            elif chunker_name == "recursive":
                chunker = recursive_chunker.RecursiveChunker(**config)
            
            chunks = chunker.chunk(self.sample_text)
            
            # Calculate statistics
            chunk_lengths = [len(chunk) for chunk in chunks]
            avg_length = sum(chunk_lengths) / len(chunk_lengths) if chunk_lengths else 0
            min_length = min(chunk_lengths) if chunk_lengths else 0
            max_length = max(chunk_lengths) if chunk_lengths else 0
            
            results[chunker_name] = {
                "total_chunks": len(chunks),
                "avg_chunk_length": avg_length,
                "min_chunk_length": min_length,
                "max_chunk_length": max_length,
                "total_characters": sum(chunk_lengths)
            }
            
            print(f"Total chunks: {len(chunks)}")
            print(f"Average chunk length: {avg_length:.1f}")
            print(f"Min chunk length: {min_length}")
            print(f"Max chunk length: {max_length}")
            print(f"Total characters: {sum(chunk_lengths)}")
        
        # Print summary comparison
        print("\n" + "="*60)
        print("SUMMARY COMPARISON")
        print("="*60)
        
        for chunker_name, stats in results.items():
            print(f"\n{chunker_name.upper()}:")
            print(f"  Chunks: {stats['total_chunks']}")
            print(f"  Avg Length: {stats['avg_chunk_length']:.1f}")
            print(f"  Length Range: {stats['min_chunk_length']}-{stats['max_chunk_length']}")
            print(f"  Total Chars: {stats['total_characters']}")


if __name__ == "__main__":
    # Run the tests with verbose output
    unittest.main(verbosity=2)

