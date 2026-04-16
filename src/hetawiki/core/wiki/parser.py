"""Document parser for HetaWiki.

Converts supported file types to plain Markdown text for LLM ingestion.

Supported formats:
  - .md / .txt          — read directly
  - .pdf                — MinerU
  - .docx               — MinerU (native, v3.0+)
  - .doc / .ppt / .pptx — LibreOffice → PDF → MinerU
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

from common.config import load_config
from mineru.backend.pipeline.pipeline_analyze import doc_analyze
from mineru.backend.pipeline.model_json_to_middle_json import result_to_middle_json
from mineru.data.data_reader_writer import FileBasedDataWriter


logger = logging.getLogger(__name__)

_PLAIN_EXTENSIONS  = {".md", ".txt", ".markdown"}
_MINERU_NATIVE     = {".pdf", ".docx"}
_OFFICE_EXTENSIONS = {".doc", ".ppt", ".pptx"}

def _ingest_cfg() -> dict:
    return load_config("hetawiki").get("ingest", {})

# Module-level executor — fixed capacity prevents unbounded thread accumulation
# when MinerU tasks time out (timed-out threads keep running until MinerU finishes,
# but the pool size caps how many can exist simultaneously).
_PARSE_EXECUTOR = ThreadPoolExecutor(
    max_workers=_ingest_cfg().get("max_workers", 4),
    thread_name_prefix="hetawiki-mineru",
)


def parse_to_markdown(file_path: Path) -> str:
    """Convert a document to Markdown text.

    Args:
        file_path: Absolute path to the source file.

    Returns:
        Markdown string extracted from the document.

    Raises:
        ValueError: Unsupported file extension.
        RuntimeError: Conversion or parsing failed.
    """
    suffix = file_path.suffix.lower()

    if suffix in _PLAIN_EXTENSIONS:
        return file_path.read_text(encoding="utf-8")

    if suffix in _MINERU_NATIVE:
        return _mineru_to_markdown(file_path)

    if suffix in _OFFICE_EXTENSIONS:
        pdf_bytes = _office_to_pdf_bytes(file_path)
        return _mineru_bytes_to_markdown(pdf_bytes)

    raise ValueError(f"Unsupported file type: {suffix}")


def _mineru_to_markdown(file_path: Path) -> str:
    pdf_bytes = file_path.read_bytes()
    timeout = _ingest_cfg().get("parse_timeout", 300)
    future = _PARSE_EXECUTOR.submit(_mineru_bytes_to_markdown, pdf_bytes)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        raise TimeoutError(f"MinerU parsing timed out after {timeout}s: {file_path.name}")


def _mineru_bytes_to_markdown(pdf_bytes: bytes) -> str:

    infer_results, image_lists, pdf_docs, langs, ocr_flags = doc_analyze(
        [pdf_bytes], ["ch"],
        parse_method="auto",
        formula_enable=True,
        table_enable=True,
    )

    with tempfile.TemporaryDirectory() as tmp:
        image_writer = FileBasedDataWriter(tmp)
        middle = result_to_middle_json(
            infer_results[0],
            image_lists[0],
            pdf_docs[0],
            image_writer,
            langs[0],
            ocr_flags[0],
            formula_enabled=True,
        )

    return _middle_json_to_markdown(middle)


def _middle_json_to_markdown(middle: dict) -> str:
    parts: list[str] = []
    for page_info in middle.get("pdf_info", []):
        for blk in page_info.get("para_blocks", []):
            text = _extract_block_text(blk)
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def _extract_block_text(blk: dict) -> str:
    spans: list[str] = []
    for line in blk.get("lines", []):
        for span in line.get("spans", []):
            if span.get("type") in ("text", "inline_equation") and "content" in span:
                spans.append(span["content"])
    return " ".join(spans).strip()


def _office_to_pdf_bytes(file_path: Path) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            "libreoffice", "--headless", "--convert-to", "pdf",
            "--outdir", tmp, str(file_path),
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"LibreOffice conversion failed: {result.stderr.decode()}")

        pdf_files = list(Path(tmp).glob("*.pdf"))
        if not pdf_files:
            raise RuntimeError("LibreOffice did not produce a PDF")
        return pdf_files[0].read_bytes()
