This folder contains license files for the libraries used in the GraphRAG project.


The GraphRAG project uses various open-source libraries, each with their own license. This folder contains the actual license files for these libraries to ensure compliance and transparency.


We have collected **34 license files** for the key libraries used in the GraphRAG project:

- `argon2-cffi-MIT` - MIT License
- `asyncer-MIT` - MIT License
- `azure-core-MIT` - MIT License
- `deepagents-MIT` - MIT License
- `docling-MIT` - MIT License
- `docling-ibm-models-MIT` - MIT License
- `document-figure-classifier-MIT` - MIT License (model weights)
- `fastapi-MIT` - MIT License
- `langchain-MIT` - MIT License
- `langgraph-MIT` - MIT License
- `openai-python-MIT` - MIT License
- `pydantic-MIT` - MIT License
- `pypdf-MIT` - MIT License
- `pytest-MIT` - MIT License
- `pyyaml-MIT` - MIT License
- `sqlalchemy-MIT` - MIT License
- `watchfiles-MIT` - MIT License

- `lxml-BSD` - BSD License
- `numpy-BSD` - BSD License
- `pandas-BSD` - BSD License
- `python-dotenv-BSD` - BSD License
- `scikit-learn-BSD` - BSD License
- `scipy-BSD` - BSD License
- `sentry-sdk-BSD` - BSD License
- `websockets-BSD` - BSD License

- `starlette-BSD-3-Clause` - BSD 3-Clause License
- `torch-BSD-3-Clause` - BSD 3-Clause License
- `torchvision-BSD-3-Clause` - BSD 3-Clause License

- `docling-layout-heron-Apache-2.0` - Apache License 2.0 (model weights)
- `rapidocr-Apache-2.0` - Apache License 2.0
- `rapidocr-models-Apache-2.0` - Apache License 2.0 (OCR model weights from PaddleOCR)

- `docling-models-CDLA-Permissive-2.0` - Community Data License Agreement – Permissive 2.0 (model weights)

- `pycryptodome-Public-Domain-BSD` - Public Domain/BSD

- `pymupdf4llm-AGPL-3.0.txt` - GNU AGPL 3.0

- **MIT License** (17 libraries and models): Very permissive, compatible with all other licenses
- **BSD License** (11 libraries, including BSD 3-Clause): Very permissive, compatible with all other licenses
- **Apache License 2.0** (3 libraries and models): Permissive, compatible with the GNU AGPL 3.0
- **CDLA-Permissive-2.0** (1 model): Permissive license for data and model weights
- **Public Domain/BSD** (1 library): Very permissive
- **GNU AGPL 3.0** (1 library): Copyleft, the same license as GraphRAG


✅ **All licenses are compatible** - The permissive licenses are compatible with each other and with the GNU AGPL 3.0 license of GraphRAG.

✅ **One AGPL dependency** - `pymupdf4llm` is licensed under the GNU AGPL 3.0, the same license as GraphRAG itself.


License files follow the pattern: `library_name-license_name`
- Library names use hyphens for multi-word libraries
- License names can be multipart (e.g., Apache-2.0, BSD-3-Clause)
- No file extensions
- Examples: `langchain-MIT`, `pandas-BSD`, `requests-Apache-2.0`


Some libraries could not be fetched due to:
- Repository structure changes
- Different branch names
- Repository access issues

The missing libraries are typically less critical and their licenses can be found on their respective GitHub repositories or PyPI pages.


These license files are provided for:
- **Compliance**: Ensuring proper attribution and license compliance
- **Transparency**: Making it clear what licenses govern the dependencies
- **Documentation**: Providing easy access to license terms for legal review


All license files were fetched directly from the official repositories of each library using the URLs specified in the license files themselves. The files for the Docling model weights, which the graphrag image includes, name the model and the license stated in its model card.

---

**Total License Files**: 34  
**Last Updated**: November 23, 2026  
**Naming Pattern**: library_name-license_name
