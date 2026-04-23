"""Parse stage for HetaDB pipelines."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hetadb.core.file_parsing.parser_assignment import ParserAssignment


@dataclass
class ParsePaths:
    """Path model required by the parse stage only."""

    data_dir: Path
    dataset_name: str
    raw_files_dir: Path
    parsed_dir: Path
    hash_dir: Path
    image_dir: Path
    text_json_out: Path
    csv_out: Path
    table_desc_out: Path
    image_desc_out: Path


@dataclass
class ParseConfig:
    """Minimal config required by the parse stage only."""

    llm_client: Any
    vlm_client: Any
    max_workers: int = 4
    supported_ext: str | set[str] = "default"


@dataclass
class ParseStageResult:
    """Structured outputs produced by the parse stage."""

    has_text: bool
    has_tables: bool
    has_images: bool

    def has_any_output(self) -> bool:
        return self.has_text or self.has_tables or self.has_images


def build_parse_paths(workspace_root: Path, kb_name: str, dataset: str) -> ParsePaths:
    """Build parse-stage paths without depending on DatasetPaths."""
    data_dir = workspace_root / "kb" / kb_name
    parsed_dir = data_dir / dataset / "parsed_file"
    return ParsePaths(
        data_dir=data_dir,
        dataset_name=dataset,
        raw_files_dir=workspace_root / "raw_files" / dataset,
        parsed_dir=parsed_dir,
        hash_dir=parsed_dir / "hash_dir",
        image_dir=parsed_dir / "image_dir",
        text_json_out=parsed_dir / "text_json_out",
        csv_out=parsed_dir / "csv_out",
        table_desc_out=parsed_dir / "table_desc_out",
        image_desc_out=parsed_dir / "image_desc_out",
    )


def run_parse_stage(paths: ParsePaths, config: ParseConfig) -> ParseStageResult:
    """Parse raw files into standardized parsed_file artifacts."""
    parser = ParserAssignment(
        data_dir=str(paths.data_dir),
        dataset_name=paths.dataset_name,
        raw_file_dir=str(paths.raw_files_dir),
        parsed_dir=paths.parsed_dir.name,
        config_supported_ext=config.supported_ext,
    )
    parser.cleanup()
    parser.step1_assignment()
    parser.step2_batch_parse(config.llm_client, config.vlm_client, max_workers=config.max_workers)

    has_text = any(paths.text_json_out.glob("*.jsonl"))
    has_tables = any(paths.csv_out.glob("*.csv")) or any(paths.table_desc_out.glob("*.json"))
    has_images = paths.image_desc_out.exists() and any(paths.image_desc_out.iterdir())

    return ParseStageResult(
        has_text=has_text,
        has_tables=has_tables,
        has_images=has_images,
    )
