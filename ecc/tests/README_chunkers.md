# Chunker Testing

This directory contains comprehensive tests for testing different text chunkers used in the GraphRAG ECC (Eventual Consistency Checker) application.

## Files

- `test_chunkers.py` - Full test suite with unittest framework
- `test_chunkers_demo.py` - Simple demo script that can be run directly
- `README_chunkers.md` - This file

## What are Chunkers?

Chunkers are components that break down large text documents into smaller, manageable pieces (chunks) for processing by AI models. Different chunking strategies are useful for different types of content and use cases.

## Available Chunkers

1. **Character Chunker** - Splits text by character count with optional overlap
2. **Regex Chunker** - Splits text using regular expression patterns
3. **Markdown Chunker** - Splits text while preserving markdown structure
4. **Recursive Chunker** - Intelligently splits text using multiple separators
5. **Semantic Chunker** - Splits text based on semantic similarity (requires embedding service)

## Running the Tests

### Option 1: Run the Demo Script (Recommended for quick testing)

```bash
cd graphrag/ecc/tests/app
python test_chunkers_demo.py
```

This will run all chunkers with sample text and show you exactly what chunks are produced by each one.

### Option 2: Run the Full Test Suite

```bash
cd graphrag/ecc/tests/app
python -m unittest test_chunkers.py -v
```

### Option 3: Run Specific Test Methods

```bash
cd graphrag/ecc/tests/app
python -m unittest test_chunkers.TestChunkers.test_character_chunker -v
python -m unittest test_chunkers.TestChunkers.test_markdown_chunker -v
```

## Sample Output

The tests will show you:

- **Total number of chunks** produced by each chunker
- **Individual chunk content** with length information
- **Configuration parameters** used (chunk size, overlap, patterns)
- **Performance comparison** between different chunkers
- **Edge case handling** (empty strings, short text, etc.)

Example output:
```
============================================================
1. CHARACTER CHUNKER
============================================================
Chunk size: 150, Overlap: 15
Total chunks: 8
Total characters: 1089

--- Chunk 1 (Length: 150) ---
# Introduction to GraphRAG

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

This framework provides a robust foundation for building enterprise-grade RAG applications.
...
```

## Test Coverage

The test suite covers:

- **Basic functionality** of each chunker
- **Different configurations** (chunk sizes, overlap sizes, patterns)
- **Edge cases** (empty strings, short text, exact chunk sizes)
- **Performance comparison** between chunkers
- **Integration** with the `get_chunker` utility function
- **Error handling** and validation

## Customizing Tests

### Adding New Test Cases

To add new test cases, edit `test_chunkers.py` and add new test methods:

```python
def test_my_custom_scenario(self):
    """Test a custom scenario"""
    # Your test code here
    pass
```

### Testing with Different Text

To test with different sample text, modify the `sample_text` variable in the `setUp` method or create new test methods with different text samples.

### Testing Different Configurations

Modify the chunker configurations in the test methods to test different parameters:

```python
chunker = character_chunker.CharacterChunker(
    chunk_size=500,  # Different chunk size
    overlap_size=50   # Different overlap
)
```

## Troubleshooting

### Import Errors

If you encounter import errors, ensure you're running from the correct directory and that the Python path includes the necessary modules.

### Mock Errors

The semantic chunker tests use mocks to avoid actual API calls. If you encounter mock-related errors, check that the mock setup is correct.

### Configuration Issues

Some chunkers require specific configuration. Check the chunker-specific test methods for proper configuration examples.

## Contributing

When adding new chunkers or modifying existing ones:

1. Add corresponding tests to `test_chunkers.py`
2. Update the demo script if needed
3. Ensure all tests pass
4. Update this README with new information

## Dependencies

The tests require:
- Python 3.7+
- unittest (built-in)
- mock (built-in in Python 3.3+)
- Access to the GraphRAG common modules

