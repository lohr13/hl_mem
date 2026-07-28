"""只读比较三种 claim index_text 格式下的 dense cosine 排名。"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from hl_mem.components import make_embedder
from hl_mem.core.vector import cosine_similarity
from hl_mem.domain.claims.claim import IndexTextMode, build_index_text
from hl_mem.protocols import EmbedderProtocol
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository

INDEX_TEXT_MODES: tuple[IndexTextMode, ...] = ("legacy", "value_only", "natural")


@dataclass(frozen=True)
class DiagnosticQuery:
    """一条查询及其目标 claim 识别条件。"""

    query: str
    target_terms: tuple[str, ...]
    target_claim_id: str | None = None


@dataclass(frozen=True)
class RankResult:
    """单个查询在一种 index_text 模式下的排名结果。"""

    query: str
    mode: IndexTextMode
    target_claim_id: str | None
    rank: int | None
    score: float | None


BUILTIN_QUERIES: tuple[DiagnosticQuery, ...] = (
    DiagnosticQuery("用户的技术栈和工具", ("技术栈", "工具", "Python", "PyTorch")),
    DiagnosticQuery(
        "唇形同步项目", ("唇形同步", "lip-rt", "dhlive", "MuseTalk", "LatentSync")
    ),
    DiagnosticQuery("数据清洗历史", ("数据清洗", "清洗")),
    DiagnosticQuery("GPU 硬件信息", ("GPU", "REDACTED_GPU", "CUDA")),
    DiagnosticQuery("hl_mem 服务配置", ("hl_mem", "配置", "端口")),
    DiagnosticQuery("Hermes 和 hl_mem 的关系", ("Hermes", "hl_mem")),
    DiagnosticQuery("用户偏好", ("偏好", "喜欢")),
    DiagnosticQuery("REDACTED_GPU", ("REDACTED_GPU",)),
    DiagnosticQuery("Codex 工作流", ("Codex", "工作流")),
    DiagnosticQuery("开源项目", ("开源", "项目")),
)


def open_readonly_database(database_path: Path) -> sqlite3.Connection:
    """以 SQLite URI 只读模式打开数据库。"""
    resolved = database_path.resolve()
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _claim_search_text(claim: dict[str, Any]) -> str:
    return json.dumps(claim, ensure_ascii=False, default=str).casefold()


def _select_target_claim(
    claims: Sequence[dict[str, Any]], diagnostic: DiagnosticQuery
) -> str | None:
    if diagnostic.target_claim_id is not None:
        return (
            diagnostic.target_claim_id
            if any(claim.get("id") == diagnostic.target_claim_id for claim in claims)
            else None
        )
    scored: list[tuple[int, str]] = []
    for claim in claims:
        text = _claim_search_text(claim)
        matches = sum(term.casefold() in text for term in diagnostic.target_terms)
        if matches:
            scored.append((matches, str(claim["id"])))
    return max(scored, default=(0, ""))[1] or None


def compare_index_text_modes(
    claims: Sequence[dict[str, Any]],
    diagnostics: Sequence[DiagnosticQuery],
    embedder: EmbedderProtocol,
) -> list[RankResult]:
    """重算三种文本 embedding，并返回每条诊断查询的目标 dense 排名。"""
    target_ids = {
        diagnostic.query: _select_target_claim(claims, diagnostic)
        for diagnostic in diagnostics
    }
    query_vectors = {
        diagnostic.query: embedder.embed_one(diagnostic.query)
        for diagnostic in diagnostics
    }
    results: list[RankResult] = []
    for mode in INDEX_TEXT_MODES:
        ranked_vectors = [
            (str(claim["id"]), embedder.embed_one(build_index_text(claim, mode=mode)))
            for claim in claims
        ]
        for diagnostic in diagnostics:
            target_id = target_ids[diagnostic.query]
            scores = sorted(
                (
                    (
                        claim_id,
                        cosine_similarity(query_vectors[diagnostic.query], vector),
                    )
                    for claim_id, vector in ranked_vectors
                ),
                key=lambda item: (-item[1], item[0]),
            )
            target = next(
                (
                    (rank, score)
                    for rank, (claim_id, score) in enumerate(scores, start=1)
                    if claim_id == target_id
                ),
                None,
            )
            results.append(
                RankResult(
                    query=diagnostic.query,
                    mode=mode,
                    target_claim_id=target_id,
                    rank=target[0] if target else None,
                    score=target[1] if target else None,
                )
            )
    return results


def _load_diagnostics(path: Path | None) -> list[DiagnosticQuery]:
    if path is None:
        return list(BUILTIN_QUERIES)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("diagnostic set must be a JSON array")
    return [
        DiagnosticQuery(
            query=str(item["query"]),
            target_terms=tuple(str(term) for term in item.get("target_terms", [])),
            target_claim_id=str(item["target_claim_id"])
            if item.get("target_claim_id")
            else None,
        )
        for item in payload
    ]


def _render_table(results: Sequence[RankResult]) -> str:
    lines = ["| 查询 | 模式 | 目标 claim | 排名 | cosine |", "|---|---|---|---:|---:|"]
    for result in results:
        score = f"{result.score:.6f}" if result.score is not None else "N/A"
        lines.append(
            f"| {result.query} | {result.mode} | {result.target_claim_id or '未找到'} | "
            f"{result.rank if result.rank is not None else 'N/A'} | {score} |"
        )
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析只读 A/B 诊断参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.getenv("HL_MEM_DB_PATH", "var/hl_mem.db")),
        help="SQLite 数据库路径；以 mode=ro 打开",
    )
    parser.add_argument("--diagnostic-set", type=Path, help="可选 JSON 诊断集")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """执行三模式 A/B 并输出 Markdown 表格。"""
    args = parse_args(argv)
    settings = Settings.from_env()
    embedder = make_embedder(settings)
    connection = open_readonly_database(args.database)
    try:
        claims = ClaimRepository(connection).list_all()
    finally:
        connection.close()
    diagnostics = _load_diagnostics(args.diagnostic_set)
    print(_render_table(compare_index_text_modes(claims, diagnostics, embedder)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
