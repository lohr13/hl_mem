"""Mine and materialize the frozen claim-pair evaluation dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import struct
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "var" / "hl_mem.db"
OUTPUT = ROOT / "evaluation" / "datasets" / "claim_pair_eval_v1.jsonl"


@dataclass(frozen=True)
class Claim:
    id: str
    subject: str
    predicate: str
    value: Any
    canonical_slot: str | None
    qualifiers: dict[str, Any]
    index_text: str
    vector: np.ndarray


@dataclass(frozen=True)
class Candidate:
    left: Claim
    right: Claim
    cosine: float
    lexical_overlap: float
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PairSpec:
    source_slice: str
    left_id: str
    right_id: str
    label: str
    rationale: str
    conflict_subtype: str | None = None


def pair(
    source_slice: str,
    left_id: str,
    right_id: str,
    label: str,
    rationale: str,
    conflict_subtype: str | None = None,
) -> PairSpec:
    return PairSpec(source_slice, left_id, right_id, label, rationale, conflict_subtype)


HIGH = "same_subject_slot_high_cosine"
MID = "same_subject_slot_mid_low_cosine"
CROSS = "cross_subject_semantic"
HARD = "hard_negative"


PAIR_SPECS = [
    # A: same subject + slot, cosine >= 0.88.
    pair(HIGH, "cedf1516b18f476fa443624473d3d6e6", "e734b4aa7d9c47c29e81e80076c15ddd", "equivalent", "安装目录完全相同，仅‘里的/中的’措辞不同。"),
    pair(HIGH, "4a20581f38bb41f6802469762d95abc2", "8c07c6670db348ea992afc4e1861e8dc", "equivalent", "同一提取优化文档路径，差异仅为 Windows 路径分隔符。"),
    pair(HIGH, "7fdf46beb11f476fbf272f4f734affe3", "da33cd7a326b4756a39696305d15cfac", "equivalent", "都表示火山小马的 memory provider 已切换为 hl_mem，项目限定只是中英文别名。"),
    pair(HIGH, "61674e12aebe435d9342d6ba97630909", "c4cca335f1744cc1accdde9dd295fb4b", "equivalent", "模型、提供商、上下文和最大 token 数全部一致；llm_extractor 的 default 即提取默认任务。"),
    pair(HIGH, "c4e490280ef74d24b5751f6793267256", "e6f7bad10e8641068aeed95ba3ecf333", "equivalent", "同一 /root/hl_mem/.env 文件，purpose 都指环境变量配置。"),
    pair(HIGH, "7f0a3f664a79433f8cb49fe6ea9f083b", "9055be8316b5490b9adfd71024cec8a0", "equivalent", "都描述火山小马承担金融建议、机器 agent 维护及财报分析，第二条只是展开表述。"),
    pair(HIGH, "708e045563584ea4943a74e65072ff0c", "be8cfd2f9e054561b5531f7b34cb8689", "equivalent", "日期时间、查看 observe 数据和决定 enforce 的计划一致。"),
    pair(HIGH, "83cd00eb182041e6b9b2ef00b64d9b38", "94858754274e4241beedc756fcc2b0a7", "compatible", "guardrail identifier 与 guardrail version 是两个不同配置键，即使当前都为空也不能合并。"),
    pair(HIGH, "050e1e7bbd764315ba2ee5b8069e0217", "44c28c2eaba04378b760fd7b56d9005d", "equivalent", "均表示本地小马已安装并正常运行 hl_mem，用途描述相同。"),
    pair(HIGH, "910c23a782664339908e8ad915d776f1", "95b1cebc17ec43e5a9c0a7a2e4dae9ff", "equivalent", "同一火山小马部署同时使用 qwen3.7-plus、百炼 embedding 与 reranker。"),
    pair(HIGH, "99e34f5db579466ab14b7f16cd14752f", "ec0b162b347e4384bc21f19a14448286", "equivalent", "插件目标路径和禁止放入 memory 子目录的约束完全相同。"),
    pair(HIGH, "77d9d48f3b8c4f82981a05c32da1cf65", "9e0dd11dc25c4d2990e01d9ceff7af2e", "compatible", "模型值相同，但 required task qualifier 分别是 fallback handling 与 general_chat，不能安全视为同一事实。"),
    pair(HIGH, "2f079b10bcb2482e832949cc2e1df7f6", "56e6414dd6dc46e290ec699640c406d3", "compatible", "配置内容相同，但 default_llm 与 chat_completion 的任务限定不同。"),
    pair(HIGH, "0235fc18d2cd40b5a0b6ed5779dc96e0", "6908c6c60c764edc967c720c18fe6b2f", "compatible", "max_lines 与 max_line_length 是不同配置键，共同取值 2000 不代表同一事实。"),
    pair(HIGH, "7967fd8a871548a69ca3e3c194316e58", "bf6cd5018aca49fbb3e1220196cc1774", "compatible", "一个是 response cache TTL，另一个是是否启用 cache，可同时成立但不能合并。"),

    # B: same subject + slot, 0.75 <= cosine < 0.88.
    pair(MID, "10ea8bc0f6e040f9acd5ad77c9a8b70d", "9ff193aebad04bfe97a61e5bde5b1671", "compatible", "两个路径分别指方案文档和评测数据集，属于不同文件。"),
    pair(MID, "3266e8b461b64b64b12de9727f3b65dc", "479bc83cf8714ea583c78c1a0fb9a008", "compatible", "都涉及 qwen3.7-plus，但任务限定不同，且第二条额外声明 thinking=false。"),
    pair(MID, "d5502067c93840d28f07004e8bbcd81d", "ec0b162b347e4384bc21f19a14448286", "compatible", "hl_mem.toml 配置文件路径与 Hermes 插件安装目录是不同对象。"),
    pair(MID, "b820a03ca3774c81808a3cedfd0cf9a9", "feb1c68f58944678beafc82fc1b2759e", "unrelated", "delete_orphans 开关与 compression 消息上限没有事实关系，只是共用 config.env slot。"),
    pair(MID, "77d9d48f3b8c4f82981a05c32da1cf65", "7a18b054efe84e2ab47e11ca07a23b2e", "compatible", "一个声明 fallback 模型，另一个声明当前会话实际模型；角色不同但可以同时为 qwen。"),
    pair(MID, "1f3f9092bb5a4eba82a30dcafe39ce02", "6444629e9a7648e7bfc1fff23c06c51d", "uncertain", "一条明确区分主模型与 fallback，另一条只说默认 LLM；缺少默认角色和时间上下文，无法安全判断冲突或等价。"),
    pair(MID, "7d5aac0eb4a246758566bda44ee781ae", "a9c50031d5ae442bbc8d5cce7740b680", "unrelated", "INDEX_TEXT_MODE 与 QUERY_CONTEXT_MODE 是不同功能的环境变量。"),
    pair(MID, "782dea4eaad44c6cbc54b537677ce5c2", "e21facf3f49844a8bf8698322ce636c9", "compatible", "reranker 是否启用和 relevance reranker floor 是相关但独立的配置。"),
    pair(MID, "076bd31dd1a649d082b644a9319b50bc", "bbfe8d62763b49e1a94b6b1c8a19dd64", "compatible", "LLM_BASE_URL 与 LLM_PROVIDER 共同描述 provider 配置，但不是同一配置键。"),
    pair(MID, "72b3ea452acd4ba89d720764d0e653e1", "85e68a029b8b4b399564da6f87b8727d", "compatible", "同日都涉及标注检查，但后者额外限定 10:00 及 enforce 决策，不能合并。"),
    pair(MID, "1b9bf7a4c2c4461d9dc74f14d4ca3dff", "e2eb66a941224cb4bd402bd20b5c67b7", "compatible", "模型名称相同，但分别用于审计提取和 auxiliary vision。"),
    pair(MID, "abd699c44c464f17916fa76248f68d08", "eb02f17a77f7480d91ffece918f9df64", "compatible", "都使用 Codex，但一个是清洗任务、另一个是深度分析任务。"),
    pair(MID, "c8a3732dea1e4d3982c6fdc5c87ad65e", "ed0fe25807e843988952be07d3910f48", "compatible", "数据迁移前清洗与条件性清理 PostgreSQL 是同一迁移背景下的不同计划。"),
    pair(MID, "6608b73b8262443ca2cc354450893421", "f2e30a9ef4ad400eb96f452398e89861", "unrelated", "前者描述当前启动界面，后者描述 Codex 的调研职责，不是同一工具事实。"),
    pair(MID, "15c6694f0f3443dd8397eed2c124eb69", "2a439a2d44304ea4b3b0339a46b4c320", "unrelated", "计划状态自动收敛开发与 enforce 提醒是不同计划，仅共享 plan.deadline slot。"),

    # C: different subjects with comparable semantics.
    pair(CROSS, "014321fec4c54b9e906b42ff2d8463de", "aa611775a0da4ceca6f066a2990fcf96", "equivalent", "subject 仅下划线/连字符不同，命题和 change qualifier 完全一致。"),
    pair(CROSS, "828cbde9555d442a82b0f303e8fff267", "95d66d8e9b9c43ccbdc414732a277263", "equivalent", "subject 仅命名风格不同，窄范围计划收敛方案完全相同。"),
    pair(CROSS, "42dfbd3880d5450ea27e6dfd6e8dce0c", "7b1d539bf4c54c0bb4894c642aedee6a", "equivalent", "user 与用户是同一主体别名，日期和 enforce 计划一致。"),
    pair(CROSS, "6ef46c7912dd4229be94fa5521303a79", "c4f7cceba75c4ef79e901baa72ed36ce", "equivalent", "user 与用户是别名，都说明启动的是 Codex CLI 而非 IDE。"),
    pair(CROSS, "6608b73b8262443ca2cc354450893421", "faea85c8fc8844088e4a9d4afe90d6e1", "equivalent", "Codex 与 codex_cli 指同一工具，CLI/非 IDE 状态一致。"),
    pair(CROSS, "a86bccf1f70c4198a024916f58f6d4be", "c7d60f6268fe47d1b80eaa5d2f429b63", "equivalent", "api-service 与 api_service 是格式化别名，API key 失败行为完全一致。"),
    pair(CROSS, "188b0409989e4ef8bebf6ee64db5fa8d", "1c1c3a1aa8e24347b2c182d9bb9a08f8", "equivalent", "system_environment 与 system_config 都指系统代理配置，键和值完全一致。"),
    pair(CROSS, "937f005223bc4ccd86a4ef80152aae64", "9b5bdda14e7246a28727a6cdba427a8f", "equivalent", "subject 是同一 recall 改进方案的命名变体，五个改进项一致。"),
    pair(CROSS, "79c33813f8a748d986adb46d7a71ed12", "e0696637f7a24d679aebe6be54b6b3ea", "uncertain", "value 都是用户要求修改 Hermes 模型，但顶层 subject 分别是 gateway 与用户；缺少原始证据无法安全纠正主体。"),
    pair(CROSS, "4aaa642bea6847408825636c64765320", "9587339944c74ae989f78705dfd7d9b4", "equivalent", "enforce task 的 subject 仅连字符不同，提醒日期与 plan qualifier 相同。"),
    pair(CROSS, "543d2e18f8ac40bd8e4b915be9b421f9", "cfea86a76a65449489f40f43ffc8acaf", "equivalent", "hindsight_postgres 与 hindsight_db 在文本中都明确为同一 Hindsight PostgreSQL 实例，端口同为 5434。"),
    pair(CROSS, "829c75fc54d34ae3b1223ce74a3a47b5", "e635528b0dbe46bca7541989e7ba2e8a", "compatible", "hl_mem 与 hl_mem_agent 不是已确认别名，且 task 不同；相同模型不能据此合并。"),
    pair(CROSS, "a9091fc4f736490b80994f1faa2e8450", "ac0c3d5ad7d04cc29536fff94e29ec3a", "compatible", "两者关联同一 stock MCP，但一条只给 Python 可执行文件，另一条是包含 server.py 的完整启动命令。"),
    pair(CROSS, "2771cca23c6d42f2bd1b1ed0ff1e96c4", "919f09496fbb4b66b58848cd4975f3ac", "compatible", "provider 值相同，但 project 分别为 hl_mem 与 hl_agent，不能跨项目合并。"),
    pair(CROSS, "c60e69f765f94ec99bb6c09e9b235371", "f4ee110e03bb42c1a1d274636cdab73d", "compatible", "一条是 Hermes 已配置的主模型，另一条是用户的变更要求；相关但命题主体和状态不同。"),

    # D: explicit hard negatives.
    pair(HARD, "40176653cb374f2f90615d0fbf71fe9f", "f0948d8f7fbf4742873be3fa62cf5308", "compatible", "HTTPS_PROXY 与 HTTP_PROXY 是不同环境变量，即使 URL 相同也不能合并。"),
    pair(HARD, "4f8b4bd3a7f04e4ca154ce8c684ce7ef", "83a0ee49e9c3450791139f806ff8d72a", "compatible", "同一 system 下 HTTP_PROXY 和 HTTPS_PROXY 是两个独立键。"),
    pair(HARD, "26b0d0c8d1714d9080c7ab1f09b9aabc", "f666265935e74330b10eec997c16f519", "compatible", "代理地址部分一致，但后者额外包含 NO_PROXY 清单，是更宽的复合配置。"),
    pair(HARD, "b854352ba34a403887041a7a8fe83dcc", "fb12a235530d41e98e8f58d743bd0157", "compatible", "一条描述代理地址及工具，另一条描述 sing-box 监听端口；可同时为真但非同一事实。"),
    pair(HARD, "a6cbff1e4c684f5dbf6d6dd1822990d0", "e2e7bae291904658b0fa440377155cda", "compatible", "127.0.0.1 与 Tailscale 地址可属于不同访问面，端口相同也不能判等。"),
    pair(HARD, "1c2c2dc96188490dadad5aa9e796d49d", "a2a9fe2101334ed8b0075c9bf58c0c08", "unrelated", "HL_MEM_URL 与 llm_extractor timeout 是不同配置，数值和 URL 重叠不构成关系。"),
    pair(HARD, "035f0ad2cd824e56afd12e4355cc051c", "ea87193490244f2099f4911d8fcddd25", "compatible", "都提及 qwen3.7-plus，但 chat/fallback 与 compression 任务不同，后者还包含 URL 和超时。"),
    pair(HARD, "24038b50f0424c48be30ab51baf50223", "78b194e730624ad184e2815e2391ea31", "compatible", "default 的 glm-5.2 与 compression 的 qwen3.7-plus 属于不同 task，可同时配置。"),
    pair(HARD, "cb71d79a60bc467ab84ecc2bb52e7c53", "f666265935e74330b10eec997c16f519", "compatible", "NO_PROXY 清单与包含该清单的完整 proxy settings 是包含关系，不是等价原子事实。"),
    pair(HARD, "1f3f9092bb5a4eba82a30dcafe39ce02", "ea87193490244f2099f4911d8fcddd25", "compatible", "同模型被用于 fallback 和 compression 两个不同角色，不能合并。"),
    pair(HARD, "88cce6bae4d440caa5e51c2b5703a5b9", "ac04c8c521204aea96725c0add6e8a23", "unrelated", "代理配置与向量扫描批大小没有语义关系。"),
    pair(HARD, "60886598e08c4eabaac7a9b03b1c1438", "6ba740faa1a44ff38bd2d0e1f046e1ab", "unrelated", "HTTP/HTTPS 代理与 idle_timeout 是不同配置主题。"),
    pair(HARD, "a6cbff1e4c684f5dbf6d6dd1822990d0", "b5bc3ea7e0674f65985a24c05c2bc12f", "unrelated", "daemon 服务 URL 与外网代理规则都在 network slot，但不是同一网络事实。"),
    pair(HARD, "b5bc3ea7e0674f65985a24c05c2bc12f", "e2e7bae291904658b0fa440377155cda", "unrelated", "代理环境变量与火山部署的 Tailscale 监听地址是不同网络配置。"),
    pair(HARD, "26b0d0c8d1714d9080c7ab1f09b9aabc", "f87f9de3206a4a6b98deac3b5825ab1c", "compatible", "代理地址与代理绕过清单相互配套，但各自是独立配置。"),
    pair(HARD, "104c867ec090499abc7b5ece21f525c3", "a133648b0e4d49ff9bed0acc5e19577c", "unrelated", "一个是需加入杀毒信任区的目录，另一个是 provider.py 文件路径。"),
    pair(HARD, "0a2e6006e3d34402a0e487e8827f4d0c", "ec0b162b347e4384bc21f19a14448286", "unrelated", "/healthz 返回版本号与 Hermes 插件安装目录没有事实关系。"),
    pair(HARD, "b90e16e27ee94d0a882160cc5ed068a6", "ec0b162b347e4384bc21f19a14448286", "conflict", "一条把目标设为 memory/hl_mem，另一条明确规定必须位于 plugins/hl_mem 且不能放入 memory 子目录。", "contradiction"),
    pair(HARD, "0f52b830930645a08f40f88191ceed24", "6444629e9a7648e7bfc1fff23c06c51d", "conflict", "同一 hl_mem 默认 LLM 分别为 glm-5.2 与 qwen3.7-plus，反映配置版本变化。", "state_change"),
    pair(HARD, "2ef94c3442a74e44b8766e30f9a57dad", "77cdcedbf1534e90ba70446d6f75b39e", "compatible", "‘本任务不要运行 pytest’与‘重构修复必须保持测试全绿’可同时成立，后者不要求当前执行者运行 pytest。"),
    pair(HARD, "137054429d2044e3981d2af1b3bef2ca", "85ecd94f95694d5fb8006f26c6a4c89e", "compatible", "直接实现不再询问与使用 Codex 排查问题是相容但不同的工作流偏好。"),
    pair(HARD, "6608b73b8262443ca2cc354450893421", "7cadac1144b74d6db905c5786320ede1", "compatible", "仅启动 CLI 的界面状态与用 Codex 执行清洗任务可以同时成立。"),
    pair(HARD, "6032b220815643ae844e043bd08d6e25", "da04916ebe084bba84c8d1622ce29bf6", "conflict", "前者记录 from_env 默认 auto 的不一致，后者记录默认已改为 off，属于同一默认值的先后变化。", "state_change"),
    pair(HARD, "662768916bb4450cb901ffff27174c14", "e9560f0cfec64d52b53eea36196840ff", "compatible", "当前主模型为 qwen 与计划改成 glm 可以同时为真，计划尚不等于已生效状态。"),
    pair(HARD, "9e0dd11dc25c4d2990e01d9ceff7af2e", "c60e69f765f94ec99bb6c09e9b235371", "compatible", "fallback qwen 与主模型 glm 是同一聊天配置中的不同角色，并不冲突。"),
]


PARAPHRASES = [
    ("00bdf2d256c749dba3e5bbe06e5e58bf", "hl_mem 不会把可信状态与保留期限硬性串联成单一线性生命周期。", "否定极性和两个生命周期维度均保持不变。"),
    ("022672d0a95c4735933642e8b36fd8dc", "本地主力工作站配备一块拥有 16GB 显存的 NVIDIA REDACTED_GPU。", "GPU 型号、显存容量和主体完全保留。"),
    ("8831730b6e0e4477bd3cc61878f7dff2", "hl_mem 采用 SQLite WAL。", "只是把‘使用’改写为‘采用’。"),
    ("083de73ef9924585afb3cce6c9ca7588", "hl_mem 的默认 LLM 服务提供商是 DashScope。", "默认角色、LLM 任务和 provider 均保持一致。"),
    ("18c7153f481e4920a8a72f69c175ce84", "memory-cleaning-workflow 通过 Codex 完成记忆数据清洗。", "工作流、工具和任务均未改变。"),
    ("37e8d97cb7804a0eab372cefd44bb384", "Hermes 使用 REDACTED_PROXY 与 xray 作为代理，端口为 10808。", "代理工具组合和端口号保持不变。"),
    ("1de2a1ba4d364833abf62bf5cb581cda", "hl_mem 清洗前数据库备份位于 REDACTED_PATH/var/hl_mem_before_cleanup_20260729_121246.db。", "备份用途和完整路径保持不变。"),
    ("48334ce60d7d4b1689591ee0ef358ddd", "hl_mem 服务监听 8200 端口。", "服务主体和端口值保持不变。"),
    ("2dac26a3315640a9863f2a0d04b0e476", "用户打算在 2026-08-03 22:00 设置 360 安全卫士信任区。", "计划动作及日期时间保持不变。"),
    ("137054429d2044e3981d2af1b3bef2ca", "用户希望 Codex 不再询问确认，直接进行实现。", "保留了直接实现且不要再问的工作流偏好。"),
]


def decode_embedding(blob: bytes) -> np.ndarray:
    """Decode a float32 BLOB through struct.unpack, then create a numpy vector."""
    if len(blob) % 4:
        raise ValueError(f"embedding byte length is not divisible by four: {len(blob)}")
    values = struct.unpack(f"<{len(blob) // 4}f", blob)
    return np.asarray(values, dtype=np.float32)


def cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    return float(np.dot(left, right) / denominator) if denominator else 0.0


def normalized_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def lexical_overlap(left: Claim, right: Claim) -> float:
    def bigrams(claim: Claim) -> set[str]:
        text = normalized_text(f"{claim.subject} {claim.predicate} {claim.value} {claim.canonical_slot or ''}")
        if len(text) < 2:
            return {text} if text else set()
        return {text[index : index + 2] for index in range(len(text) - 1)}

    left_terms = bigrams(left)
    right_terms = bigrams(right)
    union = left_terms | right_terms
    return len(left_terms & right_terms) / len(union) if union else 0.0


def load_claims() -> tuple[list[Claim], str, int]:
    connection = sqlite3.connect(f"file:{DATABASE.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id,subject_entity_id,predicate,value_json,qualifiers_json,canonical_slot,index_text,"
            "embedding_dense FROM claims WHERE status='active' AND embedding_dense IS NOT NULL ORDER BY id"
        ).fetchall()
        active_ids = [
            str(row[0])
            for row in connection.execute("SELECT id FROM claims WHERE status='active' ORDER BY id").fetchall()
        ]
    finally:
        connection.close()
    fingerprint = hashlib.sha256("".join(active_ids).encode("utf-8")).hexdigest()
    claims = [
        Claim(
            id=str(row["id"]),
            subject=str(row["subject_entity_id"] or ""),
            predicate=str(row["predicate"] or ""),
            value=json.loads(row["value_json"]),
            canonical_slot=row["canonical_slot"],
            qualifiers=json.loads(row["qualifiers_json"] or "{}"),
            index_text=str(row["index_text"] or ""),
            vector=decode_embedding(row["embedding_dense"]),
        )
        for row in rows
    ]
    return claims, fingerprint, len(active_ids)


def make_candidate(left: Claim, right: Claim, flags: Iterable[str] = ()) -> Candidate:
    if right.id < left.id:
        left, right = right, left
    return Candidate(
        left=left,
        right=right,
        cosine=cosine_similarity(left.vector, right.vector),
        lexical_overlap=lexical_overlap(left, right),
        flags=tuple(flags),
    )


def same_subject_slot_candidates(claims: list[Claim]) -> list[Candidate]:
    groups: defaultdict[tuple[str, str], list[Claim]] = defaultdict(list)
    for claim in claims:
        if claim.canonical_slot:
            groups[(claim.subject, claim.canonical_slot)].append(claim)
    candidates: list[Candidate] = []
    for group in groups.values():
        for left_index, left in enumerate(group):
            for right in group[left_index + 1 :]:
                candidates.append(make_candidate(left, right))
    return candidates


def cross_subject_candidates(claims: list[Claim]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for left_index, left in enumerate(claims):
        for right in claims[left_index + 1 :]:
            if left.subject == right.subject:
                continue
            same_slot = bool(left.canonical_slot and left.canonical_slot == right.canonical_slot)
            same_predicate = bool(left.predicate and left.predicate == right.predicate)
            if same_slot or same_predicate:
                candidates.append(make_candidate(left, right))
    return candidates


_NUMBER = re.compile(r"(?<![\w.])[+-]?\d+(?:\.\d+)?(?![\w.])")
_VERSION = re.compile(r"(?i)(?:\bv?\d+\.\d+(?:\.\d+)?\b|版本)")
_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|[/\\][\w.-]+[/\\])")
_NEGATION = re.compile(r"(?i)(?:不|未|没有|禁止|拒绝|停用|禁用|关闭|\bnot\b|\bnever\b)")


def hard_negative_candidates(same_candidates: list[Candidate], cross_candidates: list[Candidate]) -> list[Candidate]:
    result: list[Candidate] = []
    for candidate in same_candidates:
        if candidate.left.value == candidate.right.value:
            continue
        left_text = f"{candidate.left.value} {candidate.left.qualifiers}"
        right_text = f"{candidate.right.value} {candidate.right.qualifiers}"
        flags: list[str] = []
        if _NUMBER.search(left_text) and _NUMBER.search(right_text):
            flags.append("number")
        if _VERSION.search(left_text) or _VERSION.search(right_text):
            flags.append("version")
        if _PATH.search(left_text) or _PATH.search(right_text):
            flags.append("path")
        if bool(_NEGATION.search(left_text)) != bool(_NEGATION.search(right_text)):
            flags.append("polarity")
        if candidate.left.qualifiers != candidate.right.qualifiers:
            flags.append("qualifier")
        if flags:
            result.append(make_candidate(candidate.left, candidate.right, flags))

    for candidate in cross_candidates:
        if candidate.cosine < 0.80:
            continue
        left_subject = normalized_text(candidate.left.subject)
        right_subject = normalized_text(candidate.right.subject)
        if left_subject in right_subject or right_subject in left_subject:
            result.append(make_candidate(candidate.left, candidate.right, ("similar_entity",)))
    return result


def candidate_record(candidate: Candidate) -> dict[str, Any]:
    return {
        "left_id": candidate.left.id,
        "right_id": candidate.right.id,
        "left_subject": candidate.left.subject,
        "right_subject": candidate.right.subject,
        "left_predicate": candidate.left.predicate,
        "right_predicate": candidate.right.predicate,
        "left_slot": candidate.left.canonical_slot,
        "right_slot": candidate.right.canonical_slot,
        "left_value": candidate.left.value,
        "right_value": candidate.right.value,
        "left_qualifiers": candidate.left.qualifiers,
        "right_qualifiers": candidate.right.qualifiers,
        "cosine": round(candidate.cosine, 6),
        "lexical_overlap": round(candidate.lexical_overlap, 6),
        "flags": candidate.flags,
    }


def diverse(candidates: list[Candidate], limit: int) -> list[Candidate]:
    selected: list[Candidate] = []
    claim_uses: defaultdict[str, int] = defaultdict(int)
    slot_uses: defaultdict[str, int] = defaultdict(int)
    for candidate in candidates:
        slot = candidate.left.canonical_slot or candidate.right.canonical_slot or "<none>"
        if claim_uses[candidate.left.id] >= 2 or claim_uses[candidate.right.id] >= 2:
            continue
        if slot_uses[slot] >= max(3, limit // 4):
            continue
        selected.append(candidate)
        claim_uses[candidate.left.id] += 1
        claim_uses[candidate.right.id] += 1
        slot_uses[slot] += 1
        if len(selected) == limit:
            break
    return selected


def inspect(slice_name: str, limit: int) -> None:
    claims, fingerprint, active_count = load_claims()
    same = same_subject_slot_candidates(claims)
    cross = cross_subject_candidates(claims)
    if slice_name == "high":
        pool = sorted((item for item in same if item.cosine >= 0.88), key=lambda item: (-item.cosine, item.left.id, item.right.id))
    elif slice_name == "mid":
        pool = sorted(
            (item for item in same if 0.75 <= item.cosine < 0.88),
            key=lambda item: (-item.cosine, item.left.id, item.right.id),
        )
    elif slice_name == "cross":
        pool = sorted(cross, key=lambda item: (-item.cosine, item.left.id, item.right.id))
    elif slice_name == "hard":
        pool = sorted(
            hard_negative_candidates(same, cross),
            key=lambda item: (-len(item.flags), -item.cosine, item.left.id, item.right.id),
        )
    else:
        raise ValueError(slice_name)
    print(
        json.dumps(
            {
                "active_claims": active_count,
                "embedded_active_claims": len(claims),
                "fingerprint": fingerprint,
                "pool": len(pool),
            }
        )
    )
    for candidate in diverse(pool, limit):
        print(json.dumps(candidate_record(candidate), ensure_ascii=False))


def side_record(claim: Claim) -> dict[str, Any]:
    return {
        "claim_id": claim.id,
        "subject": claim.subject,
        "predicate": claim.predicate,
        "value": claim.value,
        "canonical_slot": claim.canonical_slot,
    }


def assign_splits(rows: list[dict[str, Any]], test_size: int) -> None:
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if right_root < left_root:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root

    for row in rows:
        union(str(row["left"]["claim_id"]), str(row["right"]["claim_id"]))

    component_rows: defaultdict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        component_rows[find(str(row["left"]["claim_id"]))].append(index)
    components = sorted(
        component_rows.values(),
        key=lambda indexes: hashlib.sha256(
            "|".join(rows[index]["pair_id"] for index in indexes).encode("utf-8")
        ).hexdigest(),
    )

    subsets: dict[int, tuple[int, ...]] = {0: ()}
    for component_index, indexes in enumerate(components):
        size = len(indexes)
        additions: dict[int, tuple[int, ...]] = {}
        for current_size, selected in sorted(subsets.items(), reverse=True):
            new_size = current_size + size
            if new_size <= test_size and new_size not in subsets and new_size not in additions:
                additions[new_size] = (*selected, component_index)
        subsets.update(additions)
    if test_size not in subsets:
        raise RuntimeError(f"cannot allocate exactly {test_size} test pairs by connected component")
    test_components = set(subsets[test_size])
    for component_index, indexes in enumerate(components):
        split = "test" if component_index in test_components else "dev"
        for index in indexes:
            rows[index]["split"] = split


def materialize() -> None:
    claims, fingerprint, active_count = load_claims()
    claims_by_id = {claim.id: claim for claim in claims}
    rows: list[dict[str, Any]] = []

    for spec in PAIR_SPECS:
        left = claims_by_id[spec.left_id]
        right = claims_by_id[spec.right_id]
        candidate = make_candidate(left, right)
        rows.append(
            {
                "pair_id": "",
                "source_slice": spec.source_slice,
                "left": side_record(candidate.left),
                "right": side_record(candidate.right),
                "label": spec.label,
                "conflict_subtype": spec.conflict_subtype,
                "merge_safe": spec.label == "equivalent",
                "rationale": spec.rationale,
                "mining_features": {
                    "cosine": round(candidate.cosine, 6),
                    "lexical_overlap": round(candidate.lexical_overlap, 6),
                },
                "split": "",
            }
        )

    for anchor_id, paraphrase, rationale in PARAPHRASES:
        left = claims_by_id[anchor_id]
        right = Claim(
            id=f"synthetic:{anchor_id}:paraphrase-1",
            subject=left.subject,
            predicate=left.predicate,
            value=paraphrase,
            canonical_slot=left.canonical_slot,
            qualifiers=left.qualifiers,
            index_text=paraphrase,
            vector=left.vector,
        )
        rows.append(
            {
                "pair_id": "",
                "source_slice": "llm_paraphrase_positive",
                "left": side_record(left),
                "right": side_record(right),
                "label": "equivalent",
                "conflict_subtype": None,
                "merge_safe": True,
                "rationale": rationale,
                "mining_features": {
                    "cosine": None,
                    "lexical_overlap": round(lexical_overlap(left, right), 6),
                },
                "split": "",
            }
        )

    for index, row in enumerate(rows, 1):
        row["pair_id"] = f"cp-{index:04d}"
    assign_splits(rows, test_size=32)

    header = (
        f"# corpus_count={active_count} corpus_fingerprint={fingerprint} "
        "method=sha256(concat(active_claim_ids_ordered_by_id))"
    )
    payload = "\n".join([header, *(json.dumps(row, ensure_ascii=False) for row in rows)]) + "\n"
    OUTPUT.write_text(payload, encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(OUTPUT),
                "pairs": len(rows),
                "active_claims": active_count,
                "embedded_active_claims": len(claims),
                "fingerprint": fingerprint,
            },
            ensure_ascii=False,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--inspect", choices=("high", "mid", "cross", "hard"))
    action.add_argument("--write", action="store_true")
    parser.add_argument("--limit", type=int, default=60)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.write:
        materialize()
    else:
        inspect(arguments.inspect, arguments.limit)
