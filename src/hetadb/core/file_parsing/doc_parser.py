"""Document parser for PDF/DOC/DOCX/PPT/PPTX via MinerU."""

import argparse
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from mineru.backend.pipeline.model_json_to_middle_json import result_to_middle_json
from mineru.backend.pipeline.pipeline_analyze import doc_analyze
from mineru.data.data_reader_writer import FileBasedDataWriter
from PIL import Image

from common.config import setup_logging
from hetadb.core.file_parsing.convert_to_unified import (
    ImageElement,
    MetaDict,
    TextElement,
    UnifiedDoc,
    _now_iso,
    load_hash_mapping,
    process_middle_files,
)

logger = logging.getLogger("hetadb.file_parsing")


def batch_parse(
    path_list: Sequence[str | Path],
    jsonls_dir: Path,
    image_dir: Path,
    dataset: str,
    mapping_json: Path,
    *,
    lang: Literal["zh", "en"] = "en",
    parse_method: str = "auto",
    formula_enable: bool = True,
    table_enable: bool = True,
    start_page_id: int = 0,
    end_page_id: int | None = None,
) -> None:
    for path in path_list:
        try:
            _parse_single_in_subprocess(
                path=path,
                jsonls_dir=jsonls_dir,
                image_dir=image_dir,
                dataset=dataset,
                mapping_json=mapping_json,
                lang=lang,
                parse_method=parse_method,
                formula_enable=formula_enable,
                table_enable=table_enable,
                start_page_id=start_page_id,
                end_page_id=end_page_id,
            )
        except Exception:
            logger.exception("doc parse failed for %s", Path(path).name)


def _single_parse_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S_%f")


def _parse_single_in_subprocess(
    *,
    path: str | Path,
    jsonls_dir: Path,
    image_dir: Path,
    dataset: str,
    mapping_json: Path,
    lang: Literal["zh", "en"],
    parse_method: str,
    formula_enable: bool,
    table_enable: bool,
    start_page_id: int,
    end_page_id: int | None,
) -> None:
    """Run MinerU parsing for one file in an isolated subprocess."""
    src_root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{src_root}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(src_root)
    )
    env.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    env.setdefault("TQDM_DISABLE", "1")

    cmd = [
        sys.executable,
        "-m",
        "hetadb.core.file_parsing.doc_parser",
        "--single",
        "--path",
        str(Path(path).expanduser().resolve()),
        "--jsonls-dir",
        str(Path(jsonls_dir).expanduser().resolve()),
        "--image-dir",
        str(Path(image_dir).expanduser().resolve()),
        "--dataset",
        str(dataset),
        "--mapping-json",
        str(Path(mapping_json).expanduser().resolve()),
        "--lang",
        lang,
        "--parse-method",
        parse_method,
        "--start-page-id",
        str(start_page_id),
        "--formula-enable",
        "1" if formula_enable else "0",
        "--table-enable",
        "1" if table_enable else "0",
    ]
    if end_page_id is not None:
        cmd.extend(["--end-page-id", str(end_page_id)])

    logger.info("Starting isolated doc parse subprocess for %s", Path(path).name)
    proc = subprocess.run(cmd, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            f"isolated doc parse failed for {Path(path).name} with exit code {proc.returncode}"
        )


def _parse_single_impl(
    *,
    path: str | Path,
    jsonls_dir: Path,
    image_dir: Path,
    dataset: str,
    mapping_json: Path,
    lang: Literal["zh", "en"] = "en",
    parse_method: str = "auto",
    formula_enable: bool = True,
    table_enable: bool = True,
    start_page_id: int = 0,
    end_page_id: int | None = None,
) -> None:
    """Parse one document into the dataset's unified JSONL output."""
    del start_page_id, end_page_id  # reserved for future page-range parsing

    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)

    mapping_json = Path(mapping_json).expanduser().resolve()
    filename_to_hash, hash_to_filename = load_hash_mapping(mapping_json)

    pdf_bytes = _to_pdf_bytes(p)
    hash_name = p.name
    if p.stem + ".pdf" not in filename_to_hash and p.name in hash_to_filename:
        hash_to_filename[p.stem + ".pdf"] = hash_to_filename[p.name]
        hash_name = p.stem + ".pdf"

    mineru_output = (Path(jsonls_dir).expanduser().resolve().parent / "mineru_output" / f"{p.stem}_{_single_parse_run_id()}")
    mineru_output.mkdir(parents=True, exist_ok=True)
    sub_dir = mineru_output / p.stem
    sub_dir.mkdir(exist_ok=True)
    img_dir = sub_dir / "images"
    img_dir.mkdir(exist_ok=True)

    logger.info("Mineru parse starting: %s (lang=%s, method=%s)", p.name, lang, parse_method)
    _t0 = time.monotonic()
    (
        infer_results,
        all_image_lists,
        all_pdf_docs,
        lang_list,
        ocr_enabled_list,
    ) = doc_analyze(
        [pdf_bytes],
        [lang],
        parse_method=parse_method,
        formula_enable=formula_enable,
        table_enable=table_enable,
    )
    logger.info("Mineru parse done: %s in %.1fs", p.name, time.monotonic() - _t0)
    if not infer_results:
        raise RuntimeError(f"MinerU returned no inference result for {p.name}")

    image_writer = FileBasedDataWriter(str(img_dir))
    middle_json = result_to_middle_json(
        infer_results[0],
        all_image_lists[0],
        all_pdf_docs[0],
        image_writer,
        lang_list[0],
        ocr_enabled_list[0],
        formula_enable,
    )

    meta = MetaDict(
        source=str(hash_to_filename.get(hash_name, hash_name)),
        hash_name=hash_name,
        dataset=str(dataset),
        timestamp=_now_iso(),
        total_pages=len(middle_json["pdf_info"]),
        file_type="pdf",
        description="",
    )

    converter = _MinerUConverter()
    json_content: dict[str, list[dict[str, Any]]] = {}
    for page_idx, page_info in enumerate(middle_json["pdf_info"]):
        json_content[f"page_{page_idx}"] = converter.convert_page(page_idx, page_info, img_dir)

    middle_json_path = mineru_output / f"{p.stem}.json"
    with open(middle_json_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(UnifiedDoc(meta=meta, json_content=json_content), ensure_ascii=False, indent=4))

    _copy_images(img_dir, Path(image_dir).expanduser().resolve())
    process_middle_files(mineru_output, Path(jsonls_dir).expanduser().resolve())


class _MinerUConverter:
    """Convert MinerU middle JSON pages to unified format."""

    def convert_page(
        self, page_idx: int, page_info: dict[str, Any], img_dir: Path,
    ) -> list[dict[str, Any]]:
        elements: list[Any] = []
        counters = {"text": 0, "image": 0, "table": 0, "interline_equation": 0}
        all_text_parts: list[str] = []

        for blk in page_info.get("para_blocks", []):
            if blk["type"] not in ("image", "table", "interline_equation"):
                text = self._extract_text(blk)
                if text:
                    elements.append(
                        TextElement(
                            id=f"text_{page_idx}_{counters['text']}",
                            type="text",
                            text=text,
                            bbox=self._fmt_bbox(blk.get("bbox", [])),
                        )
                    )
                    counters["text"] += 1
                    all_text_parts.append(text)

            if blk["type"] == "image":
                img_url, caption = self._collect_image(blk, img_dir)
                if img_url:
                    elements.append(
                        ImageElement(
                            id=f"image_{page_idx}_{counters['image']}",
                            type="image",
                            url=img_url,
                            bbox=self._fmt_bbox(blk.get("bbox", [])),
                            caption=caption,
                        )
                    )
                    counters["image"] += 1

            if blk["type"] == "table":
                tbl_url, caption = self._collect_table(blk, img_dir)
                if tbl_url:
                    elements.append(
                        ImageElement(
                            id=f"image_{page_idx}_{counters['table']}",
                            type="table",
                            url=tbl_url,
                            bbox=self._fmt_bbox(blk.get("bbox", [])),
                            caption=caption,
                        )
                    )
                    counters["table"] += 1

            if blk["type"] == "interline_equation":
                eq_url, caption = self._collect_equation(blk, img_dir)
                if eq_url:
                    elements.append(
                        ImageElement(
                            id=f"image_{page_idx}_{counters['interline_equation']}",
                            type="interline_equation",
                            url=eq_url,
                            bbox=self._fmt_bbox(blk.get("bbox", [])),
                            caption=caption,
                        )
                    )
                    counters["interline_equation"] += 1

        merge_text = re.sub(r"\s+", " ", " ".join(all_text_parts)).strip()
        if merge_text:
            elements.append(
                TextElement(
                    id=f"merge_text_{page_idx}",
                    type="merge_text",
                    text=merge_text,
                )
            )

        return elements

    @staticmethod
    def _collect_image(blk: dict[str, Any], img_dir: Path) -> tuple[str | None, str]:
        img_url = None
        caption_parts: list[str] = []
        for b in blk.get("blocks", []):
            if b["type"] == "image_body":
                p = _MinerUConverter._get_path(b, img_dir)
                if p.exists():
                    img_url = p.name
            elif b["type"] == "image_caption":
                caption_parts.append(_MinerUConverter._extract_text(b))
        return img_url, " ".join(caption_parts).strip()

    @staticmethod
    def _collect_table(blk: dict[str, Any], img_dir: Path) -> tuple[str | None, str]:
        tbl_url = None
        caption_parts: list[str] = []
        for b in blk.get("blocks", []):
            if b["type"] == "table_body":
                p = _MinerUConverter._get_path(b, img_dir)
                if p and p.exists():
                    tbl_url = p.name
            elif b["type"] == "table_caption":
                caption_parts.append(_MinerUConverter._extract_text(b))
        return tbl_url, " ".join(caption_parts).strip()

    @staticmethod
    def _collect_equation(blk: dict[str, Any], img_dir: Path) -> tuple[str | None, str]:
        eq_url = None
        caption = ""
        for line in blk.get("lines", []):
            for sp in line.get("spans", []):
                if sp.get("type") == "interline_equation":
                    img_path = (
                        img_dir / sp["image_path"] if "image_path" in sp else None
                    )
                    if img_path and img_path.exists():
                        eq_url = img_path.name
                    caption = sp.get("content", "")
        return eq_url, caption

    @staticmethod
    def _get_path(body: dict[str, Any], img_dir: Path) -> Path:
        for line in body.get("lines", []):
            for span in line.get("spans", []):
                if "image_path" in span:
                    return img_dir / str(span["image_path"])
        return Path()

    @staticmethod
    def _extract_text(blk: dict[str, Any]) -> str:
        spans: list[str] = []
        for line in blk.get("lines", []):
            for sp in line.get("spans", []):
                if sp.get("type") in ("text", "inline_equation") and "content" in sp:
                    spans.append(sp["content"])
        return " ".join(spans).strip()

    @staticmethod
    def _fmt_bbox(bbox: list[Any]) -> list[int]:
        if not bbox:
            return []
        return [int(round(float(x))) for x in bbox]


# --- File format converters ---

def _office_to_pdf_bytes(file_path: str | Path) -> bytes:
    """Convert doc/docx/ppt/pptx to PDF bytes via LibreOffice."""
    file_path = Path(file_path).expanduser().resolve()
    suffix = file_path.suffix.lower()
    if suffix not in {".doc", ".docx", ".ppt", ".pptx"}:
        raise ValueError(f"unsupported office format: {suffix}")

    with tempfile.TemporaryDirectory(dir=file_path.parent) as tmp_dir:
        cmd = [
            "libreoffice", "--headless", "--convert-to", "pdf",
            "--outdir", tmp_dir, str(file_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            logger.error(e.stderr.decode())
            raise RuntimeError("libreoffice convert failed") from e

        pdf_files = list(Path(tmp_dir).glob("*.pdf"))
        if not pdf_files:
            raise RuntimeError("libreoffice did not produce pdf")
        return pdf_files[0].read_bytes()


def _image_to_pdf_bytes(file_path: str | Path) -> bytes:
    """Wrap a single image into a one-page PDF."""
    img = Image.open(file_path).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="pdf")
    return buf.getvalue()


_CONVERTERS = {
    ".jpg": _image_to_pdf_bytes,
    ".jpeg": _image_to_pdf_bytes,
    ".png": _image_to_pdf_bytes,
    ".pdf": lambda p: Path(p).read_bytes(),
    ".doc": _office_to_pdf_bytes,
    ".docx": _office_to_pdf_bytes,
    ".ppt": _office_to_pdf_bytes,
    ".pptx": _office_to_pdf_bytes,
}


def _to_pdf_bytes(file_path: str | Path) -> bytes:
    suffix = Path(file_path).suffix.lower()
    if suffix not in _CONVERTERS:
        raise ValueError(f"unsupported file type: {suffix}")
    return _CONVERTERS[suffix](file_path)


def _copy_images(src_dir: Path, dest_dir: Path) -> None:
    if not src_dir.exists():
        logger.warning("Source images dir not found: %s", src_dir)
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        if item.is_file():
            shutil.copy2(item, dest_dir)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Isolated MinerU document parser")
    parser.add_argument("--single", action="store_true", help="Run a single-file parse job")
    parser.add_argument("--path")
    parser.add_argument("--jsonls-dir")
    parser.add_argument("--image-dir")
    parser.add_argument("--dataset")
    parser.add_argument("--mapping-json")
    parser.add_argument("--lang", default="en", choices=("zh", "en"))
    parser.add_argument("--parse-method", default="auto")
    parser.add_argument("--formula-enable", default="1")
    parser.add_argument("--table-enable", default="1")
    parser.add_argument("--start-page-id", type=int, default=0)
    parser.add_argument("--end-page-id", type=int, default=None)
    return parser.parse_args()


def _configure_isolated_runtime() -> None:
    setup_logging("heta", force=True)
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TQDM_DISABLE", "1")

    try:
        from huggingface_hub.utils import disable_progress_bars

        disable_progress_bars()
    except Exception:
        pass

    try:
        import tqdm as tqdm_module
        import tqdm.auto as tqdm_auto
        import tqdm.std as tqdm_std

        original_init = tqdm_std.tqdm.__init__
        if not getattr(original_init, "_heta_disable_patch", False):
            def _quiet_init(self, *args, **kwargs):
                kwargs.setdefault("disable", True)
                return original_init(self, *args, **kwargs)

            _quiet_init._heta_disable_patch = True  # type: ignore[attr-defined]
            tqdm_std.tqdm.__init__ = _quiet_init
            tqdm_module.tqdm.__init__ = _quiet_init
            tqdm_auto.tqdm.__init__ = _quiet_init
    except Exception:
        pass

    try:
        from loguru import logger as loguru_logger

        def _loguru_to_logging(message):
            record = message.record
            target_logger = logging.getLogger(record["name"])
            target_logger.log(record["level"].no, record["message"])

        loguru_logger.remove()
        loguru_logger.add(
            _loguru_to_logging,
            level="INFO",
            format="{message}",
        )
    except Exception:
        pass


def main() -> int:
    _configure_isolated_runtime()
    args = _parse_args()
    if not args.single:
        raise SystemExit("doc_parser module only supports --single when executed as a script")
    _parse_single_impl(
        path=args.path,
        jsonls_dir=Path(args.jsonls_dir),
        image_dir=Path(args.image_dir),
        dataset=args.dataset,
        mapping_json=Path(args.mapping_json),
        lang=args.lang,
        parse_method=args.parse_method,
        formula_enable=args.formula_enable == "1",
        table_enable=args.table_enable == "1",
        start_page_id=args.start_page_id,
        end_page_id=args.end_page_id,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
