"""Materialize the frozen production-backed recall evaluation dataset.

The gold mapping is deliberately curated: each gold group is one answer unit,
and the first ID is its preferred representative.  The database is always
opened read-only.  ``--audit-no-answer`` embeds queries without writing results
back and exposes their dense top-k for manual absence review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "var" / "hl_mem.db"
OUTPUT = ROOT / "evaluation" / "datasets" / "recall_eval_v1.jsonl"


@dataclass(frozen=True)
class QuerySpec:
    query: str
    query_type: str
    gold_groups: tuple[tuple[str, ...], ...] = ()


def answer(query: str, query_type: str, *groups: Iterable[str]) -> QuerySpec:
    return QuerySpec(query, query_type, tuple(tuple(group) for group in groups))


def no_answer(query: str) -> QuerySpec:
    return QuerySpec(query, "hard_no_answer")


NAME = ("7a65c8f347dc42a88490d09e6abe4b79",)
GPU_MODEL = (
    "0935b081585144aba0e46d7c60ea1e1e",
    "022672d0a95c4735933642e8b36fd8dc",
    "5e09e2467df3483bb59381ff6b2ecb60",
    "ad2133138acd431188c24e2d5727376a",
    "bf317c8f108a4c5faa9dcc8fb236fe6d",
    "d8bd5976d01541ffac3348fe2b295de8",
)
GPU_VRAM = (
    "022672d0a95c4735933642e8b36fd8dc",
    "5e09e2467df3483bb59381ff6b2ecb60",
    "ad2133138acd431188c24e2d5727376a",
    "bf317c8f108a4c5faa9dcc8fb236fe6d",
)
EMBEDDING_MODEL = (
    "ce443f0d36da44b3b01c85f793402f37",
    "5769a886c68145bc959beb828ad576a7",
)
RERANKER_MODEL = (
    "fec4dffeac6d416c96a3e63ed39e9279",
    "f27eeef3ca1f49ee852ec244c6f7eb66",
)
EXTRACTOR_MODEL = ("6518d3c66eff4890a7213f7ad75c5841",)
PROXY_STACK = (
    "37e8d97cb7804a0eab372cefd44bb384",
    "de49fcbd83324796adb3b46fac18f8b1",
)
CODEX_PREFERENCE = (
    "14ae5c5ec2f34d6d88ab8955572dc28c",
    "4a148f804f454440b4395aba43d16990",
    "85ecd94f95694d5fb8006f26c6a4c89e",
)
PYTHON_RUNTIME = ("388809b44f3a45d2911c6611a1c66e5c",)
PROJECT_ARCHITECTURE = (
    "6f7d8c3891844a9c86f2dbecd376b82d",
    "a1f2839cc3d24f34abb1051f2c4e1bb9",
    "241e0a75ee264dfc935eccc048ae85c2",
)
TTL_POLICY = ("d2b23bf3358040e1b61bb1f13989c394",)
SERVICE_PORT = (
    "48334ce60d7d4b1689591ee0ef358ddd",
    "fbb04c6d81bb4c42a6456feb47cf46f3",
)
DATABASE_ENGINE = (
    "8831730b6e0e4477bd3cc61878f7dff2",
    "263b90863fc34cedabd718526ecffeb8",
    "c67d13bc130044f590eff32c250bf9af",
)
NO_LLM_RECALL = ("17af3dbad5d8443c9308f1fe7558c426",)
STATE_TTL = (
    "59b993bab4534931a43f8758fa3cd1b5",
    "6c323399617d44c18d2a239008643c2e",
)
MEMORY_PROVIDER = (
    "7fdf46beb11f476fbf272f4f734affe3",
    "da33cd7a326b4756a39696305d15cfac",
    "c27d5f9401f3439c93018f96f3d3c659",
)
HINDSIGHT_PORT = (
    "543d2e18f8ac40bd8e4b915be9b421f9",
    "cfea86a76a65449489f40f43ffc8acaf",
    "4490c63a29ef4c14b4b3a0750e9050c0",
)
VERTICAL_MEDIA = (
    "027ae8d1bdf64b08be8c9852dc824356",
    "62f2fe71b031447a80905bca53c514e8",
    "dab76544d6984787bdb259cf56abebc7",
)
DEFAULT_LLM_PROVIDER = ("083de73ef9924585afb3cce6c9ca7588",)
VECTOR_BACKEND = ("0ccdd0f627a641c6931968aa9af20884",)
HERMES_FALLBACK = (
    "77d9d48f3b8c4f82981a05c32da1cf65",
    "9e0dd11dc25c4d2990e01d9ceff7af2e",
    "caa060196b4646c2b50e6076c510f25a",
    "0a7f703d8c6b4d058945a6646c8dfe7b",
)
DATA_MODEL = ("2ae48b687c9a47c696ec7586d1dc0e93",)
ARCHIVE_BEHAVIOR = ("1d30ffe4a25041c9abd4ea10b554d79c",)
ANN_TRIGGER = (
    "fc77a293af514e32b36c140bd0000215",
    "bbb2bec5da3249b8bf54db09cab16976",
    "8a27b60242324e4d8077f38d7767526d",
    "4ff34f9417d04c1198bce9d3b680575f",
)
BACKFILL_CAS = (
    "468eee3ff0ed4c81a288cbd485dae9f3",
    "1c9147d26dea4a6d9f3fdbc7b10ed701",
)
PLAN_NOT_AUTO_CLOSED = (
    "99f66111449746c5b8b58942c5725bb3",
    "e1736b2e915645db8c13178d7368c473",
    "e8bd68764f4e479696f3900c92671aa3",
)
MEMORY_TIERING = (
    "1295282ba91b47b68690bd08b920f40e",
    "440ceaee088e4c1fa439d1e61f06abb0",
    "df1c0272c31a440c9d754980394fb956",
)
INDEX_TEXT_MODE = (
    "7d5aac0eb4a246758566bda44ee781ae",
    "d262a58066d44d1680eedb9ab2b82e7f",
)
BACKFILL_COMMAND = ("468eee3ff0ed4c81a288cbd485dae9f3",)
NO_PROXY_LIST = (
    "88cce6bae4d440caa5e51c2b5703a5b9",
    "9db5bc1ca63442b28ef53a02cb8ed115",
    "b5bc3ea7e0674f65985a24c05c2bc12f",
)
COMPRESSION_MODEL = (
    "78b194e730624ad184e2815e2391ea31",
    "ea87193490244f2099f4911d8fcddd25",
)
LOCAL_DATABASE_PATH = (
    "567e4c80c4ad4d4e872835e18f7d2022",
    "7869f7c2293d41eface1e823a366c7d2",
)
PLUGIN_PATH = (
    "99e34f5db579466ab14b7f16cd14752f",
    "ec0b162b347e4384bc21f19a14448286",
    "32e26a2b730146c0874b371da1c43905",
    "556adbbbdd2c4a3cbae9228c9ed4f064",
    "fdc4bb1d6e0a42a98422530b11bce057",
)
BACKUP_PATH = (
    "1de2a1ba4d364833abf62bf5cb581cda",
    "b592e97f9b724bb69674fcce55642b03",
)
EXTRACTION_PROMPT_PATH = ("63fb75d4b76d4a949cd6592efa3f4c9e",)
QWEN_CONTEXT_LENGTH = (
    "f815e02d5c4a4815ae29d76cf7842963",
    "7172873952914d93b4b27a47d40ca1db",
)
DAEMON_URL = ("a6cbff1e4c684f5dbf6d6dd1822990d0",)
HINDSIGHT_DATA_PATH = ("b4d0949c5f21423ba474515c4f5194a2",)


SPECS: tuple[QuerySpec, ...] = (
    # Existing recall_regression_v1 queries, remapped against this corpus.
    answer("我叫什么名字？", "normal", NAME),
    answer("what is my name", "deep_paraphrase", NAME),
    answer("姓名？", "normal", NAME),
    answer("我的 GPU 是什么？", "normal", GPU_MODEL),
    answer("显卡型号", "normal", GPU_MODEL),
    answer("how much VRAM do I have", "deep_paraphrase", GPU_VRAM),
    answer("embedding 模型是什么", "normal", EMBEDDING_MODEL),
    answer("向量模型", "deep_paraphrase", EMBEDDING_MODEL),
    answer("which reranker is configured", "entity_name", RERANKER_MODEL),
    answer("提取模型是什么", "normal", EXTRACTOR_MODEL),
    answer("我常用的代理是什么", "normal", PROXY_STACK),
    answer("REDACTED_PROXY 配置", "entity_name", PROXY_STACK),
    no_answer("我喜欢什么编辑器"),
    answer("哪个工具最趁手", "deep_paraphrase", CODEX_PREFERENCE),
    answer("preferred coding assistant", "deep_paraphrase", CODEX_PREFERENCE),
    no_answer("数据库版本"),
    answer("Python runtime", "entity_name", PYTHON_RUNTIME),
    no_answer("召回的候选数量"),
    answer("项目采用什么架构", "normal", PROJECT_ARCHITECTURE),
    answer("TTL 策略", "entity_name", TTL_POLICY),
    no_answer("用户是左撇子吗？"),
    no_answer("我的生日是哪天？"),
    no_answer("我住在哪个城市？"),
    no_answer("favorite football team"),
    no_answer("宠物叫什么？"),
    no_answer("银行卡号"),
    no_answer("昨晚吃了什么？"),
    no_answer("我的血型"),
    no_answer("是否拥有房产"),
    no_answer("最喜欢的电影导演"),
    # 10 ordinary natural questions.
    answer("hl_mem 服务监听哪个端口？", "normal", SERVICE_PORT),
    answer("hl_mem 使用什么数据库？", "normal", DATABASE_ENGINE),
    answer("hl_mem 的 recall 阶段会调用 LLM 吗？", "normal", NO_LLM_RECALL),
    answer("健康、进程和连通性这几类状态记忆保留几天？", "normal", STATE_TTL),
    answer("火山小马的 memory provider 是什么？", "normal", MEMORY_PROVIDER),
    answer("Hindsight 的 PostgreSQL 监听端口是多少？", "normal", HINDSIGHT_PORT),
    answer("我偏好的数字人素材分辨率和方向是什么？", "normal", VERTICAL_MEDIA),
    answer("hl_mem 默认使用哪个 LLM provider？", "normal", DEFAULT_LLM_PROVIDER),
    answer("hl_mem 默认采用哪种向量检索后端？", "normal", VECTOR_BACKEND),
    answer("Hermes 的 fallback 模型是什么？", "normal", HERMES_FALLBACK),
    # 7 deep paraphrases that avoid copying claim surface wording.
    answer("这个记忆系统用哪些机制区分事实何时成立、何时被记录，并保留来源？", "deep_paraphrase", DATA_MODEL),
    answer("一条记忆退出在线检索后，原陈述和证据还留着吗？", "deep_paraphrase", ARCHIVE_BEHAVIOR),
    answer("REST、MCP 和后台任务怎样避免各自复制写入业务逻辑？", "deep_paraphrase", PROJECT_ARCHITECTURE),
    answer("在什么规模或延迟压力出现之前，项目不会急着引入近似向量索引？", "deep_paraphrase", ANN_TRIGGER),
    answer("批量重建检索文本时，如何做到中断可续且重复执行安全？", "deep_paraphrase", BACKFILL_CAS),
    answer("已有计划得到结果后，目前会自动把旧计划关掉吗？", "deep_paraphrase", PLAN_NOT_AUTO_CLOSED),
    answer("高频手工知识和对话里产生的结构化记忆分别放在哪里？", "deep_paraphrase", MEMORY_TIERING),
    # 5 entity names / abbreviations / technical terms.
    answer("HL_MEM_INDEX_TEXT_MODE 支持哪些模式，默认值是什么？", "entity_name", INDEX_TEXT_MODE),
    answer("backfill-index-text 子命令具体做什么？", "entity_name", BACKFILL_COMMAND),
    answer("NO_PROXY 里配置了哪些本地地址和域名？", "entity_name", NO_PROXY_LIST),
    answer("qwen3.7-plus 在 compression 模块中的超时是多少？", "entity_name", COMPRESSION_MODEL),
    answer("gte-rerank-v2 在 hl_mem 中承担什么角色？", "entity_name", RERANKER_MODEL),
    # 5 path / version / numeric-sensitive questions.
    answer("本机 hl_mem 数据库文件的完整路径是什么？", "path_version", LOCAL_DATABASE_PATH),
    answer("Hermes 的 hl_mem 插件应该安装到哪个目录？", "path_version", PLUGIN_PATH),
    answer("数据库清洗前的备份文件保存在哪里？", "path_version", BACKUP_PATH),
    answer("提取 prompt 定义在哪个文件和变量里？", "path_version", EXTRACTION_PROMPT_PATH),
    answer("qwen3.7-plus 的上下文长度是多少？", "path_version", QWEN_CONTEXT_LENGTH),
    # 3 multi-answer-unit questions.
    answer("hl_mem 的监听端口和本地 daemon URL 分别是什么？", "multi_gold", SERVICE_PORT, DAEMON_URL),
    answer("Hindsight PostgreSQL 的监听端口和数据目录分别是什么？", "multi_gold", HINDSIGHT_PORT, HINDSIGHT_DATA_PATH),
    answer("hl_mem 使用的数据库和默认向量检索后端分别是什么？", "multi_gold", DATABASE_ENGINE, VECTOR_BACKEND),
    # 20 new near-miss no-answer questions.
    no_answer("hl_mem 调用提取模型时 temperature 设置为多少？"),
    no_answer("hl_mem 的 embedding 缓存目录在哪里？"),
    no_answer("hl_mem 使用的 Redis 端口是多少？"),
    no_answer("hl_mem SQLite 数据库的 page_size 配置是多少？"),
    no_answer("qwen3.7-text-embedding 每条结果会返回多少个 sparse item？"),
    no_answer("本地主力工作站的 NVIDIA 驱动版本是多少？"),
    no_answer("本地主力工作站 GPU 的序列号是什么？"),
    no_answer("我的手机号码是多少？"),
    no_answer("Hermes API token 的过期日期是什么时候？"),
    no_answer("Hindsight PostgreSQL 的数据库用户名是什么？"),
    no_answer("Hindsight PostgreSQL 的数据库密码是什么？"),
    no_answer("hl_mem 的加密密钥多久轮换一次？"),
    no_answer("hl_mem 数据库备份会保留多少天？"),
    no_answer("hl_mem 的运行日志文件保存在哪个路径？"),
    no_answer("hl_mem 生产 Docker 镜像使用什么 tag？"),
    no_answer("hl_mem 生产服务的主机名是什么？"),
    no_answer("我在 VS Code 中使用什么配色主题？"),
    no_answer("hl_mem 当前使用的 Python 3.11 补丁版本是多少？"),
    no_answer("hl_mem HTTPS 证书文件放在哪个路径？"),
    no_answer("hl_mem reranker 每次调用的 top_n 参数是多少？"),
)


def read_corpus() -> tuple[sqlite3.Connection, list[sqlite3.Row], list[str]]:
    connection = sqlite3.connect(f"file:{DATABASE.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    rows = connection.execute(
        "SELECT id,subject_entity_id,predicate,value_json,index_text,embedding_dense "
        "FROM claims WHERE status='active' ORDER BY id"
    ).fetchall()
    return connection, rows, [str(row["id"]) for row in rows]


def assign_splits(rows: list[dict[str, object]], test_size: int = 32) -> None:
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        if right_root < left_root:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root

    for row in rows:
        row_id = str(row["id"])
        find(row_id)
        for group in row["gold_groups"]:  # type: ignore[union-attr]
            for claim_id in group:
                union(row_id, f"claim:{claim_id}")

    components: defaultdict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        components[find(str(row["id"]))].append(index)
    ordered = sorted(
        components.values(),
        key=lambda indexes: hashlib.sha256("|".join(str(rows[index]["id"]) for index in indexes).encode()).hexdigest(),
    )

    subsets: dict[int, tuple[int, ...]] = {0: ()}
    for component_index, indexes in enumerate(ordered):
        additions: dict[int, tuple[int, ...]] = {}
        for size, chosen in sorted(subsets.items(), reverse=True):
            new_size = size + len(indexes)
            if new_size <= test_size and new_size not in subsets and new_size not in additions:
                additions[new_size] = (*chosen, component_index)
        subsets.update(additions)
    if test_size not in subsets:
        raise RuntimeError(f"cannot assign exactly {test_size} test rows without gold leakage")
    test_components = set(subsets[test_size])
    for component_index, indexes in enumerate(ordered):
        split = "test" if component_index in test_components else "dev"
        for index in indexes:
            rows[index]["split"] = split


def build_rows(active_ids: set[str]) -> list[dict[str, object]]:
    if len(SPECS) != 80:
        raise RuntimeError(f"expected 80 query specs, found {len(SPECS)}")
    rows: list[dict[str, object]] = []
    for number, spec in enumerate(SPECS, 1):
        for group in spec.gold_groups:
            missing = set(group) - active_ids
            if missing:
                raise RuntimeError(f"rq-{number:03d} references inactive/missing claims: {sorted(missing)}")
        rows.append(
            {
                "id": f"rq-{number:03d}",
                "query": spec.query,
                "query_type": spec.query_type,
                "intent": "current_state",
                "gold_ids": [group[0] for group in spec.gold_groups],
                "gold_groups": [list(group) for group in spec.gold_groups],
                "no_answer": not spec.gold_groups,
                "split": "",
            }
        )
    assign_splits(rows)
    return rows


def materialize() -> None:
    connection, corpus, corpus_ids = read_corpus()
    try:
        rows = build_rows(set(corpus_ids))
    finally:
        connection.close()
    fingerprint = hashlib.sha256("".join(corpus_ids).encode()).hexdigest()
    header = (
        f"# corpus_count={len(corpus_ids)} corpus_fingerprint={fingerprint} "
        "method=sha256(concat(active_claim_ids_ordered_by_id))"
    )
    payload = "\n".join([header, *(json.dumps(row, ensure_ascii=False) for row in rows)]) + "\n"
    OUTPUT.write_text(payload, encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(OUTPUT),
                "rows": len(rows),
                "answerable": sum(not bool(row["no_answer"]) for row in rows),
                "no_answer": sum(bool(row["no_answer"]) for row in rows),
                "corpus_count": len(corpus_ids),
                "corpus_fingerprint": fingerprint,
            },
            ensure_ascii=False,
        )
    )


def audit_no_answer(selected_ids: set[str], limit: int) -> None:
    # Imported lazily so --write and structural validation stay local/offline.
    from hl_mem.components import make_embedder
    from hl_mem.config_loader import load_settings
    from hl_mem.protocols import embed_queries

    connection, corpus, corpus_ids = read_corpus()
    try:
        rows = build_rows(set(corpus_ids))
        selected = [row for row in rows if row["no_answer"] and (not selected_ids or str(row["id"]) in selected_ids)]
        unknown = selected_ids - {str(row["id"]) for row in selected}
        if unknown:
            raise RuntimeError(f"not no-answer query IDs: {sorted(unknown)}")
        embedded_claims = [row for row in corpus if row["embedding_dense"] is not None]
        matrix = np.vstack([np.frombuffer(row["embedding_dense"], dtype="<f4") for row in embedded_claims])
        norms = np.linalg.norm(matrix, axis=1)
        embedder = make_embedder(load_settings(ROOT / "hl_mem.toml", ROOT / ".env"))
        query_blobs = embed_queries(embedder, [str(row["query"]) for row in selected])
        for query_row, query_blob in zip(selected, query_blobs, strict=True):
            vector = np.frombuffer(query_blob, dtype="<f4")
            scores = matrix @ vector / (norms * np.linalg.norm(vector))
            ranking = np.argsort(-scores, kind="stable")[:limit]
            candidates = [
                {
                    "rank": rank,
                    "claim_id": str(embedded_claims[index]["id"]),
                    "score": round(float(scores[index]), 6),
                    "text": str(embedded_claims[index]["index_text"] or "")[:240],
                }
                for rank, index in enumerate(ranking, 1)
            ]
            print(
                json.dumps(
                    {"id": query_row["id"], "query": query_row["query"], "top": candidates},
                    ensure_ascii=False,
                )
            )
    finally:
        connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--write", action="store_true")
    actions.add_argument("--audit-no-answer", action="store_true")
    parser.add_argument("--ids", default="", help="comma-separated rq-NNN IDs for no-answer audit")
    parser.add_argument("--limit", type=int, default=50)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.write:
        materialize()
    else:
        audit_no_answer({item for item in arguments.ids.split(",") if item}, arguments.limit)
