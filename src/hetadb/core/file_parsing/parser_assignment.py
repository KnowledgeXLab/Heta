"""Dispatch raw files to appropriate parsers by file extension."""

import asyncio
import logging
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import py7zr
from rarfile import RarFile

from hetadb.core.file_parsing import doc_parser, html_parser, image_parser, sheet_parser, text_parser
from hetadb.core.file_parsing.convert_to_unified import JsonlRoller, UnifiedDoc
from hetadb.utils.hash_filename import rename_files_to_hash

logger = logging.getLogger("hetadb.file_parsing")

_ARCHIVE_EXTENSIONS = (".zip", ".7z", ".rar", ".tar.xz", ".tar.gz", ".tar.bz2", ".tar")


class ParserAssignment:
    DOC_EXTENSIONS = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}
    HTML_EXTENSIONS = {".html"}
    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".bmp", ".ico"}
    TEXT_EXTENSIONS = {".txt", ".text", ".md", ".markdown"}
    SHEET_EXTENSIONS = {".csv", ".xls", ".xlsx", ".ods"}

    def __init__(
        self,
        data_dir: str,
        dataset_name: str,
        raw_file_dir: str | list[Path],
        parsed_dir: str = "parsed_file",
        exclude_files: list[str] | None = None,
        config_supported_ext: str | set = "default",
    ):
        data_dir_path = Path(data_dir)
        if not data_dir_path.is_absolute():
            data_dir_path = data_dir_path.resolve()

        self.data_dir = data_dir_path
        self.dataset_name = dataset_name
        if isinstance(raw_file_dir, str):
            self.raw_file_dir = self.data_dir / dataset_name / raw_file_dir
        else:
            self.raw_file_dir = raw_file_dir

        self.parsed_dir = self.data_dir / dataset_name / parsed_dir
        for name in ("hash_dir", "image_dir", "text_json_out", "csv_out", "image_desc_out", "table_desc_out"):
            setattr(self, name, self.parsed_dir / name)

        self.exclude_files = exclude_files or []
        self.config_supported_ext = config_supported_ext

    def cleanup(self):
        """Remove previous parsing results."""
        if self.parsed_dir.exists():
            shutil.rmtree(self.parsed_dir)

    def step1_assignment(self):
        """Hash-rename raw files and collect them for parsing."""
        supported_ext = (
            self.DOC_EXTENSIONS | self.HTML_EXTENSIONS | self.IMAGE_EXTENSIONS
            | self.TEXT_EXTENSIONS | self.SHEET_EXTENSIONS
        )
        if self.config_supported_ext != "default":
            if not isinstance(self.config_supported_ext, set):
                raise ValueError("config_supported_ext must be a set")
            normalized = {
                f".{ext}" if not ext.startswith(".") else ext
                for ext in self.config_supported_ext
            }
            supported_ext = supported_ext & normalized

        if isinstance(self.raw_file_dir, Path):
            for filepath in self.raw_file_dir.rglob("*"):
                if filepath.name.lower().endswith(_ARCHIVE_EXTENSIONS):
                    self._extract_archive(filepath)

            if not self.raw_file_dir.exists():
                logger.warning("Directory does not exist: %s", self.raw_file_dir)
                files_to_process = []
            else:
                files_to_process = [
                    f for f in self.raw_file_dir.rglob("*")
                    if f.is_file()
                    and f.suffix.lower() in supported_ext
                    and not any(exc in f.name for exc in self.exclude_files)
                ]
        else:
            for filepath in self.raw_file_dir:
                if filepath.name.lower().endswith(_ARCHIVE_EXTENSIONS):
                    self._extract_archive(filepath)
            files_to_process = self.raw_file_dir

        if not files_to_process:
            logger.info("No supported files found")
            return

        self.mapping_json = rename_files_to_hash(
            self.dataset_name, files_to_process, self.hash_dir,
        )
        logger.info("Hash-rename completed")

    def _extract_archive(self, filepath: Path):
        """Extract archive to its parent directory.

        Each member path is resolved against out_dir to prevent ZIP Slip
        (path-traversal via ``../`` components in archive member names).
        """
        out_dir = filepath.parent.resolve()
        filename = filepath.name.lower()
        try:
            if filename.endswith(".zip"):
                with zipfile.ZipFile(filepath, "r") as zf:
                    for member in zf.infolist():
                        dest = (out_dir / member.filename).resolve()
                        if not str(dest).startswith(str(out_dir)):
                            logger.warning("Skipping unsafe zip member: %s", member.filename)
                            continue
                        zf.extract(member, out_dir)
            elif filename.endswith(".7z"):
                with py7zr.SevenZipFile(filepath, mode="r") as zf:
                    for name in zf.getnames():
                        dest = (out_dir / name).resolve()
                        if not str(dest).startswith(str(out_dir)):
                            logger.warning("Skipping unsafe 7z member: %s", name)
                            continue
                    zf.extractall(path=out_dir)
            elif filename.endswith(".rar"):
                with RarFile(filepath, "r") as rf:
                    for member in rf.infolist():
                        dest = (out_dir / member.filename).resolve()
                        if not str(dest).startswith(str(out_dir)):
                            logger.warning("Skipping unsafe rar member: %s", member.filename)
                            continue
                        rf.extract(member, out_dir)
            elif filename.endswith((".tar.xz", ".tar.gz", ".tar.bz2", ".tar")):
                with tarfile.open(filepath, "r:*") as tf:
                    safe_members = []
                    for member in tf.getmembers():
                        dest = (out_dir / member.name).resolve()
                        if not str(dest).startswith(str(out_dir)):
                            logger.warning("Skipping unsafe tar member: %s", member.name)
                            continue
                        safe_members.append(member)
                    tf.extractall(out_dir, members=safe_members)
        except Exception as e:
            logger.error("Failed to extract %s: %s", filepath, e)

    def step2_batch_parse(self, llm: Any, vlm: Any, max_workers: int = 4) -> None:
        """Parse all hash-renamed files using type-specific parsers.

        Phase 1 — text / html / doc / sheet are independent and run concurrently.
        Phase 2 — image_parser runs after phase 1 because its first pass reads
                  JSONL files produced by the phase-1 parsers.
        """
        text_files  = [p for p in self.hash_dir.rglob("*") if p.suffix.lower() in self.TEXT_EXTENSIONS]
        doc_files   = [p for p in self.hash_dir.rglob("*") if p.suffix.lower() in self.DOC_EXTENSIONS]
        html_files  = [p for p in self.hash_dir.rglob("*") if p.suffix.lower() in self.HTML_EXTENSIONS]
        image_files = [p for p in self.hash_dir.rglob("*") if p.suffix.lower() in self.IMAGE_EXTENSIONS]
        sheet_files = [p for p in self.hash_dir.rglob("*") if p.suffix.lower() in self.SHEET_EXTENSIONS]

        def _run_text():
            if text_files:
                logger.info("%d files for text_parser", len(text_files))
                text_parser.parse(text_files, self.text_json_out, self.dataset_name, self.mapping_json)

        def _run_doc():
            if doc_files:
                logger.info("%d files for doc_parser", len(doc_files))
                doc_parser.batch_parse(doc_files, self.text_json_out, self.image_dir, self.dataset_name, self.mapping_json)

        def _run_html():
            if html_files:
                logger.info("%d files for html_parser", len(html_files))
                html_parser.parse(html_files, self.text_json_out, self.dataset_name, self.mapping_json)

        def _run_sheet():
            if sheet_files:
                logger.info("%d files for sheet_parser", len(sheet_files))
                asyncio.run(sheet_parser.parse(sheet_files, self.csv_out, self.table_desc_out, self.dataset_name, self.mapping_json, llm))
                # Roll table UnifiedDoc JSONs into text_json_out/*.jsonl so that
                # chunk_directory picks them up alongside other parser output.
                # table_desc_out is kept intact for the SQL backup path.
                roller = JsonlRoller(self.text_json_out, prefix="tables")
                for json_file in sorted(self.table_desc_out.glob("*.json")):
                    try:
                        import json as _json
                        content = _json.loads(json_file.read_text(encoding="utf-8"))
                        roller.write(UnifiedDoc(meta=content["meta"], json_content=content["json_content"]))
                    except Exception:
                        logger.warning("Failed to roll table JSON into text_json_out: %s", json_file, exc_info=True)
                roller.close()

        # Phase 1: dispatch all independent parsers concurrently.
        from concurrent.futures import ThreadPoolExecutor as _TPE
        with _TPE(max_workers=max_workers) as executor:
            futures = [executor.submit(fn) for fn in (_run_text, _run_doc, _run_html, _run_sheet)]
        for future in futures:
            if future.exception():
                logger.error("Phase-1 parser error: %s", future.exception())

        # Phase 2: image_parser needs JSONL output from phase-1 parsers.
        jsonl_images = (
            [p for p in self.image_dir.rglob("*") if p.suffix.lower() in self.IMAGE_EXTENSIONS]
            if self.image_dir.exists() else []
        )
        if image_files or jsonl_images:
            logger.info("%d images to describe", len(image_files) + len(jsonl_images))
            asyncio.run(
                image_parser.create_description(
                    image_files, self.image_dir, self.text_json_out,
                    self.image_desc_out, self.dataset_name, self.mapping_json, vlm,
                )
            )
