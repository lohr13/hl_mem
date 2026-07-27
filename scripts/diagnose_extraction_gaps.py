"""只读诊断关键词事件到持久化 claims 的提取覆盖缺口。"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from hl_mem.ingest.event_filter import EventFilter


@dataclass(frozen=True)
class KeywordDomain:
    """一组代表同一诊断领域的关键词。"""

    name: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class GapSample:
    """未形成对应 claim 的事件样本。"""

    event_id: str
    actor_type: str | None
    session_id: str | None
    filter_reason: str
    content: str


@dataclass(frozen=True)
class DomainReport:
    """一个关键词领域的覆盖统计。"""

    domain: KeywordDomain
    event_hits: int
    claim_hits: int
    coverage: float | None
    filter_reasons: dict[str, int]
    samples: tuple[GapSample, ...]


KEYWORD_DOMAINS: tuple[KeywordDomain, ...] = (
    KeywordDomain("dhlive / 数字人 / 直播", ("dhlive", "数字人", "直播")),
    KeywordDomain("lip-rt / 唇形同步 / lip sync", ("lip-rt", "唇形同步", "lip sync")),
    KeywordDomain("MuseTalk / LatentSync / SoulX", ("MuseTalk", "LatentSync", "SoulX")),
    KeywordDomain("REDACTED_GPU / GPU / CUDA", ("REDACTED_GPU", "GPU", "CUDA")),
    KeywordDomain("Tailscale / 火山 / Bitwarden", ("Tailscale", "火山", "Bitwarden")),
    KeywordDomain("Hermes / config / provider", ("Hermes", "config", "provider")),
)


def open_readonly_database(database_path: Path) -> sqlite3.Connection:
    """以 SQLite URI 只读模式打开数据库。"""
    connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _matches(text: str, keywords: tuple[str, ...]) -> bool:
    normalized = text.casefold()
    return any(keyword.casefold() in normalized for keyword in keywords)


def _claim_text(row: sqlite3.Row) -> str:
    return " ".join(
        str(row[key] or "")
        for key in ("subject_entity_id", "predicate", "value_json", "index_text")
        if key in row.keys()
    )


def diagnose_domains(
    connection: sqlite3.Connection,
    domains: Sequence[KeywordDomain] = KEYWORD_DOMAINS,
    *,
    sample_limit: int = 5,
) -> list[DomainReport]:
    """统计领域覆盖率，并抽取没有相关 evidence claim 的事件。"""
    events = list(
        connection.execute(
            "SELECT id,actor_type,session_id,event_type,content_json FROM events ORDER BY recorded_at,id"
        )
    )
    claim_columns = {row["name"] for row in connection.execute("PRAGMA table_info(claims)")}
    selected_columns = ["id", "subject_entity_id", "predicate", "value_json"]
    if "index_text" in claim_columns:
        selected_columns.append("index_text")
    claims = list(connection.execute(f"SELECT {','.join(selected_columns)} FROM claims"))
    links = list(
        connection.execute(
            "SELECT derived_id,evidence_id FROM evidence_links " "WHERE derived_type='claim' AND evidence_type='event'"
        )
    )
    claims_by_id = {str(row["id"]): row for row in claims}
    linked_claim_ids: dict[str, set[str]] = {}
    for link in links:
        linked_claim_ids.setdefault(str(link["evidence_id"]), set()).add(str(link["derived_id"]))

    event_filter = EventFilter()
    reports: list[DomainReport] = []
    for domain in domains:
        matching_events = [row for row in events if _matches(str(row["content_json"]), domain.keywords)]
        matching_claims = [row for row in claims if _matches(_claim_text(row), domain.keywords)]
        reasons: Counter[str] = Counter()
        samples: list[GapSample] = []
        for row in matching_events:
            event = dict(row)
            should_extract, reason = event_filter.should_extract(event)
            reasons[reason] += 1
            related = [
                claims_by_id[claim_id]
                for claim_id in linked_claim_ids.get(str(row["id"]), set())
                if claim_id in claims_by_id
            ]
            if any(_matches(_claim_text(claim), domain.keywords) for claim in related):
                continue
            if len(samples) < sample_limit:
                samples.append(
                    GapSample(
                        event_id=str(row["id"]),
                        actor_type=row["actor_type"],
                        session_id=row["session_id"],
                        filter_reason=reason if not should_extract else "eligible",
                        content=str(row["content_json"])[:500],
                    )
                )
        event_hits = len(matching_events)
        claim_hits = len(matching_claims)
        reports.append(
            DomainReport(
                domain=domain,
                event_hits=event_hits,
                claim_hits=claim_hits,
                coverage=min(1.0, claim_hits / event_hits) if event_hits else None,
                filter_reasons=dict(sorted(reasons.items())),
                samples=tuple(samples),
            )
        )
    return reports


def _render_report(reports: Sequence[DomainReport]) -> str:
    lines = [
        "| 关键词领域 | events 命中 | claims 命中 | 覆盖率 |",
        "|---|---:|---:|---:|",
    ]
    for report in reports:
        coverage = f"{report.coverage:.1%}" if report.coverage is not None else "N/A"
        lines.append(f"| {report.domain.name} | {report.event_hits} | {report.claim_hits} | {coverage} |")
    lines.append("\n## 缺口样本")
    for report in reports:
        lines.append(f"\n### {report.domain.name}")
        lines.append(f"EventFilter 判定：{json.dumps(report.filter_reasons, ensure_ascii=False, sort_keys=True)}")
        if not report.samples:
            lines.append("未发现无对应 claim 的 event 样本。")
            continue
        for sample in report.samples:
            lines.append(
                f"- event={sample.event_id} actor={sample.actor_type} session={sample.session_id} "
                f"filter={sample.filter_reason}: {sample.content}"
            )
    lines.extend(
        [
            "\n## 漏提取原因分析",
            "- Prompt：system prompt 明确跳过临时状态和工具实现细节，但没有排除项目、硬件、部署或 provider 等长期事实。"
            "若样本为 eligible 且没有关联 claim，首要嫌疑是 should_memorize 判定偏保守或 LLM 未输出完整 claim。",
            "- EventFilter：只会拦截少于 5 字符、assistant 短确认/短状态汇报和无结构原始 tool_result；"
            "上方 filter 统计可直接确认具体样本是否在进入 LLM 前被过滤。",
            "- 分块：结构化分块保留重叠上下文，通常不会影响短 event；超长对话仍可能让项目主体与事实跨块分离，"
            "需要结合样本长度和 context_prefix 进一步回放。",
            "- 去重/写入：已有 evidence link 但相关 claim 文本不含关键词，可能是提取表达泛化；"
            "eligible 且完全无 evidence link 也可能发生在低 importance、校验失败或去重/写入阶段，需结合 DEBUG 提取埋点区分。",
        ]
    )
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析提取覆盖诊断参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.getenv("HL_MEM_DB_PATH", "var/hl_mem.db")),
        help="SQLite 数据库路径；以 mode=ro 打开",
    )
    parser.add_argument("--sample-limit", type=int, default=5, choices=range(3, 6))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """运行只读覆盖诊断并输出统计、样本和原因分析。"""
    args = parse_args(argv)
    connection = open_readonly_database(args.database)
    try:
        reports = diagnose_domains(connection, sample_limit=args.sample_limit)
    finally:
        connection.close()
    print(_render_report(reports))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
