"""
Text extraction utilities for various file formats.
This module handles the extraction of text content from different document types.
"""
import os
import json
import logging
import base64
import io
import re
import tempfile
import threading
from pathlib import Path
import shutil
import asyncio
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# Global lock for pymupdf4llm calls (not thread-safe)
_pymupdf4llm_lock = threading.Lock()

# regex for markdown images: ![alt](path)
# [^)]+ (not [^)\s]+) so that paths containing spaces are captured correctly.
# pymupdf4llm can generate image filenames with spaces; the narrower \s exclusion
# caused extract_images() to silently return [] for those files, deleting the temp
# folder and leaving broken references in the markdown.
_md_pattern = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')

# Matches a ColN placeholder header cell produced by pymupdf4llm when it
# cannot detect a column header from the PDF structure (common in form PDFs).
_coln_pattern = re.compile(r'\bCol\d+\b')


def _clean_pdf_markdown(markdown: str) -> str:
    """Apply post-processing to markdown produced by pymupdf4llm for form PDFs.

    Two specific artefacts are fixed:

    1. **Duplicate table rows** — complex form PDFs (e.g. IRS forms) often have
       overlapping text layers (a rendered background layer plus a searchable text
       layer).  pymupdf4llm can emit the same row twice: once from the background
       layer (no formatting, missing spaces) and once from the text layer (bold,
       correct spacing).  The duplicate row that appears immediately after the
       original is removed; when the content is identical after stripping bold
       markers, the richer (longer) version is kept.

    2. **ColN placeholder headers** — pymupdf4llm uses "Col1", "Col2", … when it
       cannot derive a header from the PDF's column structure.  These are replaced
       with empty strings so the table is still valid markdown but does not expose
       internal artefacts to downstream consumers.
    """
    # --- Pass 1: remove ColN placeholders ---
    markdown = _coln_pattern.sub('', markdown)

    # --- Pass 2: deduplicate consecutive table rows ---
    lines = markdown.splitlines()
    cleaned: list[str] = []
    for line in lines:
        if cleaned and line.startswith('|') and cleaned[-1].startswith('|'):
            prev = cleaned[-1]
            norm_cur = re.sub(r'\*+', '', line).strip()
            norm_prev = re.sub(r'\*+', '', prev).strip()
            if norm_cur == norm_prev:
                if len(line) > len(prev):
                    cleaned[-1] = line
                continue
        cleaned.append(line)

    return '\n'.join(cleaned)


def extract_images(md_text):
    """
    Returns list of {"path": path, "image_id": image_id}
    image_id = basename without extension
    """
    images = []
    for m in _md_pattern.finditer(md_text):
        path = m.group(2)
        basename = os.path.basename(path)
        image_id = os.path.splitext(basename)[0]
        images.append({"path": path, "image_id": image_id})
    return images

def insert_description_by_id(md_text, image_id, description):
    """
    Replace the description for an image whose basename == image_id.
    """
    safe_desc = description.replace("[", "(").replace("]", ")")

    def repl(m):
        old_path = m.group(2)
        candidate_id = os.path.splitext(os.path.basename(old_path))[0]

        if candidate_id == image_id:
            return f'![{safe_desc}]({old_path})'

        return m.group(0)
    return _md_pattern.sub(repl, md_text)


def replace_path_with_tg_protocol(md_text, image_id, tg_reference):
    """
    Replace the file path for an image whose basename == image_id with tg:// protocol reference.
    tg_reference should be like 'Graphs_image_1'
    """
    def repl(m):
        old_path = m.group(2)
        candidate_id = os.path.splitext(os.path.basename(old_path))[0]

        if candidate_id == image_id:
            alt_text = m.group(1)
            return f'![{alt_text}](tg://{tg_reference})'

        return m.group(0)

    return _md_pattern.sub(repl, md_text)

class TextExtractor:
    """Class for handling text extraction from various file formats and cleanup."""

    def __init__(self):
        """Initialize the TextExtractor."""
        self.supported_extensions = {
            '.txt': 'text/plain',
            '.md': 'text/markdown',
            '.pdf': 'application/pdf',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            '.doc': 'application/msword',
            '.html': 'text/html',
            '.htm': 'text/html',
            '.json': 'application/json',
            '.csv': 'text/csv',
            '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            '.xls': 'application/vnd.ms-excel',
            '.xml': 'application/xml',
            '.jpeg': 'image/jpeg',
            '.jpg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.jsonl': 'application/x-jsonlines'
        }

    async def _process_file_async(self, file_path, graphname, temp_folder):
        """
        Async helper to process a single file.
        Runs in thread pool to avoid blocking on I/O operations.
        Creates one JSONL file per input file.

        Args:
            file_path: Absolute path to the input file to be processed (e.g., "C:/data/docs/report.pdf").
            graphname: Name of the knowledge graph this file belongs to, used for metadata tagging.
            temp_folder: Absolute path to the temporary directory where output JSONL files are written.
        """
        try:
            loop = asyncio.get_event_loop()

            doc_entries = await loop.run_in_executor(
                None,
                extract_text_from_file_with_images_as_docs,
                file_path,
                graphname
            )

            # Create one JSONL file per input file
            if doc_entries:
                # Use the original filename (stem) for the JSONL file
                file_stem = Path(file_path).stem
                jsonl_file = os.path.join(temp_folder, f"{file_stem}.jsonl")
                
                await loop.run_in_executor(
                    None,
                    self._write_to_jsonl,
                    jsonl_file,
                    doc_entries
                )
            
            # Return metadata only, documents already saved to JSONL
            return {
                'success': True,
                'file_path': str(file_path),
                'num_documents': len(doc_entries),
                'jsonl_file': f"{Path(file_path).stem}.jsonl"
            }

        except FileNotFoundError:
            return {'success': False, 'file_path': str(file_path), 'error': 'File not found'}
        except PermissionError:
            return {'success': False, 'file_path': str(file_path), 'error': 'Permission denied'}
        except Exception as e:
            logger.warning(f"Failed to process file {file_path}: {e}")
            return {'success': False, 'file_path': str(file_path), 'error': str(e)}
    
    def _write_to_jsonl(self, jsonl_file, doc_entries):
        """
        Write document entries to a JSONL file (one file per input file).
        Each document is written as a separate line.
        """
        with open(jsonl_file, 'w', encoding='utf-8') as f:
            for doc_data in doc_entries:
                json_line = json.dumps(doc_data, ensure_ascii=False)
                f.write(json_line + '\n')

    async def _process_folder_async(self, folder_path, graphname, temp_folder, max_concurrent=10):
        """
        Async version of process_folder for parallel file processing.
        Creates one JSONL file per input file.
        """
        logger.info(f"Processing local folder ASYNC: {folder_path} for graph: {graphname} (max_concurrent={max_concurrent})")

        folder_path_obj = Path(folder_path)

        if not folder_path_obj.exists():
            raise Exception(f"Folder path does not exist: {folder_path}")

        if not folder_path_obj.is_dir():
            raise Exception(f"Path is not a directory: {folder_path}")

        # Create temp folder for JSONL files
        os.makedirs(temp_folder, exist_ok=True)
        logger.info(f"Saving processed documents to: {temp_folder}")

        def safe_walk(path):
            try:
                for item in path.iterdir():
                    if item.name.startswith(('.', '~', '$')) or 'BROMIUM' in item.name.upper():
                        continue
                    if item.is_file():
                        yield item
                    elif item.is_dir():
                        yield from safe_walk(item)
            except (PermissionError, OSError) as e:
                logger.warning(f"Cannot access directory {path}: {e}")

        files_to_process = []
        jsonl_files_copied = []
        for file_path in safe_walk(folder_path_obj):
            if file_path.is_file():
                if file_path.name.startswith(('.', '~', '$')) or 'BROMIUM' in file_path.name.upper():
                    continue
                file_ext = file_path.suffix.lower()
                if file_ext == '.jsonl':
                    dest = os.path.join(temp_folder, file_path.name)
                    shutil.copy2(str(file_path), dest)
                    num_lines = sum(1 for _ in open(dest, 'r', encoding='utf-8'))
                    jsonl_files_copied.append({
                        'file_path': str(file_path),
                        'num_documents': num_lines,
                        'jsonl_file': file_path.name,
                        'status': 'success'
                    })
                    logger.info(f"Copied JSONL file directly: {file_path.name} ({num_lines} documents)")
                elif file_ext in self.supported_extensions:
                    files_to_process.append(file_path)

        logger.info(f"Found {len(files_to_process)} files to process, {len(jsonl_files_copied)} JSONL files copied directly")

        semaphore = asyncio.Semaphore(max_concurrent)

        async def process_with_semaphore(file_path):
            async with semaphore:
                return await self._process_file_async(file_path, graphname, temp_folder)

        tasks = [process_with_semaphore(fp) for fp in files_to_process]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        processed_files_info = list(jsonl_files_copied)
        total_docs = sum(f['num_documents'] for f in jsonl_files_copied)

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"File processing failed with exception: {result}")
                continue

            if result.get('success'):
                num_docs = result.get('num_documents', 0)
                total_docs += num_docs
                
                processed_files_info.append({
                    'file_path': result['file_path'],
                    'num_documents': num_docs,
                    'jsonl_file': result.get('jsonl_file'),
                    'status': 'success'
                })
            else:
                processed_files_info.append({
                    'file_path': result['file_path'],
                    'status': 'failed',
                    'error': result.get('error', 'Unknown error')
                })

        logger.info(f"Processed {len(processed_files_info)} files, extracted {total_docs} total documents")
        logger.info(f"Created {len([f for f in processed_files_info if f.get('status') == 'success'])} JSONL files in {temp_folder}")

        return {
            'statusCode': 200,
            'message': f'Processed {len(processed_files_info)} files, {total_docs} documents',
            'files': processed_files_info,
            'num_documents': total_docs,
            'temp_folder': temp_folder
        }

    def process_folder(self, folder_path, graphname, temp_folder):
        """
        Process local folder with multiple file formats and extract text content.
        Uses async processing internally for parallel file handling.
        Creates one JSONL file per input file.
        
        Args:
            folder_path: Path to the folder containing files to process
            graphname: Name of the graph (for context)
            temp_folder: Path to save processed documents as JSONL files (one per input file)
        """
        logger.info(f"Processing local folder: {folder_path} for graph: {graphname}")
        return asyncio.run(self._process_folder_async(folder_path, graphname, temp_folder))


def extract_text_from_file_with_images_as_docs(file_path, graphname=None):
    """
    Extract text and images from a file, treating images as separate document entries.
    """
    file_path = Path(file_path)
    extension = file_path.suffix.lower()
    base_doc_id = str(file_path.stem)

    logger.debug(f"Extracting with images as docs: {file_path} (type: {extension})")

    if extension == '.pdf':
        return _extract_pdf_with_images_as_docs(file_path, base_doc_id, graphname)
    elif extension in ['.jpeg', '.jpg', '.png', '.gif']:
        return _extract_standalone_image_as_doc(file_path, base_doc_id, graphname)
    else:
        content = extract_text_from_file(file_path, graphname)
        doc_type = get_doc_type_from_extension(extension)
        return [{
            "doc_id": base_doc_id,
            "doc_type": doc_type,
            "content": content,
            "position": 0
        }]

def _sanitize_image_filenames(image_folder, markdown_content):
    """Rename image files that contain spaces (replace with underscores).

    pymupdf4llm can produce filenames with spaces.  Renaming them avoids
    downstream issues with path parsing and markdown rendering.

    Returns the updated markdown_content with paths adjusted to match the
    renamed files.
    """
    if not image_folder.exists():
        return markdown_content

    for img_file in image_folder.iterdir():
        if not img_file.is_file() or ' ' not in img_file.name:
            continue
        new_name = img_file.name.replace(' ', '_')
        new_path = img_file.with_name(new_name)
        img_file.rename(new_path)
        old_ref = str(img_file)
        new_ref = str(new_path)
        markdown_content = markdown_content.replace(old_ref, new_ref)

    return markdown_content


def _extract_pdf_with_images_as_docs(file_path, base_doc_id, graphname=None):
    """
    Extract PDF as ONE markdown document with inline image references using pymupdf4llm.
    Uses unique temporary folder per PDF to allow parallel processing.
    After processing, delete the extracted image folder.
    """
    # Use a unique ABSOLUTE temp folder per PDF.
    # A relative path would resolve to whatever the process CWD happens to be at
    # call time (varies across ThreadPoolExecutor threads in container deployments).
    # pymupdf4llm embeds os.path.join(image_path, filename) in the markdown, so an
    # absolute image_path produces absolute embedded paths that PIL can always open
    # regardless of CWD.
    image_output_folder = Path(tempfile.mkdtemp(prefix="tg_pdf_"))

    try:
        import pymupdf4llm
        from PIL import Image as PILImage
        from common.utils.image_data_extractor import describe_image_with_llm

        # Ensure clean slate - remove folder if it exists from failed previous run
        if image_output_folder.exists():
            shutil.rmtree(image_output_folder, ignore_errors=True)

        # Convert PDF to markdown with extracted image files
        # Use lock because pymupdf4llm's table extraction is not thread-safe
        # See: https://github.com/pymupdf/PyMuPDF/issues/3241
        with _pymupdf4llm_lock:
            try:
                markdown_content = pymupdf4llm.to_markdown(
                    file_path,
                    write_images=True,
                    image_path=str(image_output_folder),  # unique folder per PDF
                    margins=0,
                    image_size_limit=0.08,
                )
            except Exception:
                # Retry with table_strategy="lines" if first attempt fails
                try:
                    markdown_content = pymupdf4llm.to_markdown(
                        file_path,
                        write_images=True,
                        image_path=str(image_output_folder),  # unique folder per PDF
                        margins=0,
                        image_size_limit=0.08,
                        table_strategy="lines",
                    )
                except Exception as e:
                    logger.error(f"pymupdf4llm failed for {file_path}: {e}")
                    # Cleanup folder if it was created
                    if image_output_folder.exists():
                        shutil.rmtree(image_output_folder, ignore_errors=True)
                    return [{
                        "doc_id": base_doc_id,
                        "doc_type": "markdown",
                        "content": f"[PDF extraction failed: {e}]",
                        "position": 0
                    }]

        if not markdown_content or not markdown_content.strip():
            logger.warning(
                f"No text layer found in PDF: {file_path}. "
                "The file may be a scanned image-only PDF — consider enabling OCR."
            )
            if image_output_folder.exists():
                shutil.rmtree(image_output_folder, ignore_errors=True)
            return [{
                "doc_id": base_doc_id,
                "doc_type": "markdown",
                "content": f"[Scanned PDF — no text layer extracted: {file_path.name}]",
                "position": 0
            }]

        # Clean up artefacts common in form PDFs (duplicate rows, ColN headers)
        markdown_content = _clean_pdf_markdown(markdown_content)

        # Rename image files that contain spaces to avoid path-parsing issues
        markdown_content = _sanitize_image_filenames(image_output_folder, markdown_content)

        # Extract image references from markdown
        image_refs = extract_images(markdown_content)

        if not image_refs:
            # cleanup folder anyway
            if image_output_folder.exists():
                shutil.rmtree(image_output_folder, ignore_errors=True)

            return [{
                "doc_id": base_doc_id,
                "doc_type": "markdown",
                "content": markdown_content,
                "position": 0
            }]
        image_entries = []
        image_counter = 0
        for img_ref in image_refs:
            try:
                img_path = Path(img_ref["path"])  # convert to Path
                image_id = img_ref["image_id"]
                # Image description
                description = describe_image_with_llm(str(img_path))
                markdown_content = insert_description_by_id(
                    markdown_content,
                    image_id,
                    description
                )
                # Convert image to base64
                pil_image = PILImage.open(img_path)
                buffer = io.BytesIO()

                if pil_image.mode != "RGB":
                    pil_image = pil_image.convert("RGB")

                pil_image.save(buffer, format="JPEG", quality=95)
                image_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

                image_counter += 1
                image_doc_id = f"{base_doc_id}_image_{image_counter}".lower()

                # Replace file path with tg:// protocol reference in markdown
                markdown_content = replace_path_with_tg_protocol(
                    markdown_content,
                    image_id,
                    image_doc_id
                )

                image_entries.append({
                    "doc_id": image_doc_id,
                    "doc_type": "image",
                    "image_description": description,
                    "image_data": image_base64,
                    "image_format": "jpg",
                    "parent_doc": base_doc_id,
                    "page_number": 0,
                    "width": pil_image.width,
                    "height": pil_image.height,
                    "position": image_counter
                })

            except Exception as img_error:
                logger.warning(f"Failed to process image {img_ref.get('path')}: {img_error}")
                failed_path = img_ref.get("path", "")
                if failed_path:
                    markdown_content = re.sub(
                        r'!\[.*?\]\(' + re.escape(failed_path) + r'\)',
                        "",
                        markdown_content,
                    )

        # FINAL CLEANUP — delete folder after processing everything
        if image_output_folder.exists() and image_output_folder.is_dir():
            try:
                shutil.rmtree(image_output_folder)
                logger.debug(f"Deleted image folder: {image_output_folder}")
            except Exception as delete_err:
                logger.warning(f"Failed to delete folder {image_output_folder}: {delete_err}")

        # Build final result
        result = [{
            "doc_id": base_doc_id,
            "doc_type": "markdown",
            "content": markdown_content,
            "position": 0
        }]
        result.extend(image_entries)
        return result

    except ImportError as import_err:
        logger.error(f"Required library missing: {import_err}")
        # Cleanup on import error
        if image_output_folder.exists():
            shutil.rmtree(image_output_folder, ignore_errors=True)
        return [{
            "doc_id": base_doc_id,
            "doc_type": "markdown",
            "content": "[PDF extraction requires pymupdf4llm and PyMuPDF]",
            "position": 0
        }]
    except Exception as e:
        logger.error(f"Error extracting PDF: {e}")
        # Cleanup on any other error
        if image_output_folder.exists():
            shutil.rmtree(image_output_folder, ignore_errors=True)
        raise

def _extract_standalone_image_as_doc(file_path, base_doc_id, graphname=None):
    """
    Extract standalone image file as ONE markdown document with inline image reference.
    """
    try:
        from PIL import Image as PILImage
        from common.utils.image_data_extractor import describe_image_with_llm

        pil_image = PILImage.open(file_path)
        if pil_image.width < 100 or pil_image.height < 100:
            pass
        description = describe_image_with_llm(str(Path(file_path).absolute()))
        buffer = io.BytesIO()
        if pil_image.mode != 'RGB':
            pil_image = pil_image.convert('RGB')
        pil_image.save(buffer, format="JPEG", quality=95)
        image_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')

        image_id = f"{base_doc_id}_image_1".lower()
        content = f"![{description}](tg://{image_id})"
        return [
            {
                "doc_id": base_doc_id,
                "doc_type": "image",
                "content": content,
                "position": 0
            },
            {
                "doc_id": image_id,
                "doc_type": "image",
                "image_description": description,
                "image_data": image_base64,
                "image_format": "jpg",
                "parent_doc": base_doc_id,
                "page_number": 0,
                "width": pil_image.width,
                "height": pil_image.height,
                "position": 1
            }
        ]

    except Exception as e:
        logger.error(f"Error extracting image: {e}")
        return [{
            "doc_id": base_doc_id,
            "doc_type": "markdown",
            "content": f"[Image extraction failed: {str(e)}]",
            "position": 0
        }]


def extract_text_from_file(file_path, graphname=None):
    """
    Extract text content from a file based on its extension.
    """
    file_path = Path(file_path)
    extension = file_path.suffix.lower()

    logger.debug(f"Extracting text from {file_path} (type: {extension}) for graph: {graphname}")

    try:
        if extension in ['.txt', '.md']:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read().strip()
        elif extension in ['.html', '.htm', '.csv']:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read().strip()
        elif extension == '.json':
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return json.dumps(data, indent=2, ensure_ascii=False)
        elif extension == '.docx':
            import docx
            doc = docx.Document(file_path)
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        elif extension in ['.xlsx', '.xls']:
            import pandas as pd
            engine = 'openpyxl' if extension == '.xlsx' else 'xlrd'
            try:
                xl = pd.ExcelFile(file_path, engine=engine)
            except Exception:
                xl = pd.ExcelFile(file_path)
            sheet_texts = []
            for sheet_name in xl.sheet_names:
                # Always read with header=None so no data row is silently
                # consumed as column names for headerless spreadsheets.
                df = xl.parse(sheet_name, header=None)
                if df.empty:
                    continue
                df = df.fillna('')
                # Detect header row: first row is all non-empty strings with
                # no purely numeric values → treat as column names.
                first_row = df.iloc[0]
                if all(isinstance(v, str) and v.strip() for v in first_row):
                    df.columns = first_row.tolist()
                    df = df.iloc[1:].reset_index(drop=True)
                else:
                    df.columns = [f"Column {i + 1}" for i in range(len(df.columns))]
                sheet_md = df.to_markdown(index=False)
                sheet_texts.append(f"## Sheet: {sheet_name}\n\n{sheet_md}")
            return "\n\n".join(sheet_texts) if sheet_texts else "[Excel file is empty or contains no data]"
        elif extension == '.xml':
            import xml.etree.ElementTree as ET
            tree = ET.parse(file_path)
            root = tree.getroot()

            def extract_text_from_element(element):
                text = element.text or ""
                for child in element:
                    text += " " + extract_text_from_element(child)
                if element.tail:
                    text += " " + element.tail
                return text.strip()

            content = extract_text_from_element(root)
            import re
            return re.sub(r'\s+', ' ', content).strip()
        else:
            return f"[Unsupported file type: {extension}]"

    except Exception as e:
        logger.error(f"Error extracting text from {file_path}: {e}")
        raise Exception(f"Text extraction failed: {e}")


def get_doc_type_from_extension(extension):
    """Map file extension to a chunker-compatible document type."""
    if not extension.startswith('.'):
        extension = '.' + extension
    extension = extension.lower()

    if extension in ['.html', '.htm']:
        return 'html'
    elif extension in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp']:
        return 'image'
    else:
        return 'markdown'

def get_supported_extensions():
    """Get list of supported file extensions."""
    return {'.txt', '.md', '.html', '.htm', '.csv', '.json', '.pdf', '.docx', '.doc', '.xml', '.jpeg', '.jpg', '.png', '.gif', '.xlsx', '.xls', '.jsonl'}

def is_supported_file(file_path):
    """Check if a file is supported for text extraction."""
    extension = Path(file_path).suffix.lower()
    return extension in get_supported_extensions()