from __future__ import annotations

from dataclasses import dataclass, field, fields

import yaml

from hetawiki.utils.path import PACKAGE_ROOT

_WIKI_CONFIG_PATH = PACKAGE_ROOT / "config" / "wiki_config.yaml"


@dataclass(frozen=True)
class MergeConfig:
    max_steps: int = 15
    max_seconds: int = 300
    temperature: float = 0.2


@dataclass(frozen=True)
class QueryConfig:
    max_steps: int = 10
    max_seconds: int = 120
    temperature: float = 0.2


@dataclass(frozen=True)
class LintConfig:
    enabled: bool = True
    interval_hours: int = 24
    max_steps: int = 20
    max_seconds: int = 600
    temperature: float = 0.2


@dataclass(frozen=True)
class WikiConfig:
    supported_extensions: frozenset[str] = field(default_factory=frozenset)
    merge: MergeConfig = field(default_factory=MergeConfig)
    query: QueryConfig = field(default_factory=QueryConfig)
    lint: LintConfig = field(default_factory=LintConfig)

    @classmethod
    def _from_dict(cls, raw: dict) -> WikiConfig:
        merge_raw = raw.get("merge", {})
        merge = MergeConfig(
            **{f.name: merge_raw[f.name] for f in fields(MergeConfig) if f.name in merge_raw}
        )
        query_raw = raw.get("query", {})
        query = QueryConfig(
            **{f.name: query_raw[f.name] for f in fields(QueryConfig) if f.name in query_raw}
        )
        lint_raw = raw.get("lint", {})
        lint = LintConfig(
            **{f.name: lint_raw[f.name] for f in fields(LintConfig) if f.name in lint_raw}
        )
        return cls(
            supported_extensions=frozenset(raw.get("supported_extensions", [])),
            merge=merge,
            query=query,
            lint=lint,
        )


_wiki_config: WikiConfig | None = None


def load_wiki_config() -> WikiConfig:
    global _wiki_config
    if _wiki_config is None:
        with _WIKI_CONFIG_PATH.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        _wiki_config = WikiConfig._from_dict(raw)
    return _wiki_config
