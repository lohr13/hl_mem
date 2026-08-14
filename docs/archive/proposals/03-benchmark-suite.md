# 公开长期记忆 Benchmark 适配方案

## 目标与边界

适配 LongMemEval 的固定小型 `core` 子集（建议 80 条，允许 50–100），建立 extraction、retrieval、lifecycle 三层可重复评测。runner 只读基准源数据，评测数据库使用临时副本；不修改领域模型，不把 benchmark 特例加入生产 prompt 或召回逻辑。

## 目录与集成点

```text
evaluation/
└── longmemeval/
    ├── manifest.json          # 上游版本、许可、原始文件 SHA-256、subset 定义
    ├── core.ids.json          # 固定样本 ID，不复制不必要的上游正文
    └── README.md
src/hl_mem/evaluation/
├── models.py                 # 输入、gold、metric、report dataclass
├── longmemeval.py            # 转换器
├── metrics.py                # 纯函数指标
├── runner.py                 # 三层只读评测编排
└── reporting.py              # JSON/Markdown
```

集成：

- `src/hl_mem/cli.py::main()` 增加 `eval` subcommand。
- `application/ingest.py::IngestService.ingest_event()` 用于把转换后的事件写入临时 DB。
- `application/recall.py::RecallService.recall()` 作为生产召回黑盒；通过既有 trace 获取候选归因。
- `domain.temporal.claim_is_visible()` 与 `workers/ttl.py::expire_claims()`、`workers/decay.py::decay_claims()` 用于 lifecycle 层。
- `storage/database.py::Database` 创建每次 run 的隔离 SQLite 文件。

## 类型与转换协议

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol


@dataclass(frozen=True)
class GoldTemporal:
    """用于 extraction/retrieval 时间正确性的 gold 区间。"""

    evidence_event_id: str
    occurred_start: str | None
    occurred_end: str | None
    valid_from: str | None
    valid_to: str | None


@dataclass(frozen=True)
class LifecycleCheckpoint:
    """某个双时间检查点下的期望可见性和状态。"""

    at: str
    known_as_of: str | None
    expected_visible_event_ids: tuple[str, ...]
    expected_hidden_event_ids: tuple[str, ...]
    expected_status_by_event_id: dict[str, str]
    worker_action: str | None  # expire_ttl | decay_access | None


@dataclass(frozen=True)
class BenchmarkCase:
    """规范化后的长期记忆评测样本。"""

    case_id: str
    events: tuple[dict[str, object], ...]
    query: str
    gold_evidence_event_ids: tuple[str, ...]
    gold_temporal: tuple[GoldTemporal, ...]
    lifecycle_checkpoints: tuple[LifecycleCheckpoint, ...]
    gold_answer: str | None
    as_of: str | None
    known_as_of: str | None
    category: str


class BenchmarkAdapterProtocol(Protocol):
    """把公开数据转换为 hl_mem 的事件和 gold 约束。"""

    def load(self, source: Path, subset: str) -> Iterable[BenchmarkCase]: ...
```

`LongMemEvalAdapter` 将每轮消息转换成：

```json
{
  "id": "lme:<case_id>:<message_id>",
  "idempotency_key": "longmemeval:<case_id>:<message_id>",
  "tenant_id": "eval:<case_id>",
  "event_type": "message",
  "actor_type": "user|assistant",
  "content": {"text": "...", "benchmark_locator": {"case_id": "...", "message_id": "..."}},
  "occurred_at": "<数据集时间>",
  "recorded_at": "<确定性摄入顺序时间>"
}
```

若上游缺少时间，转换器使用 manifest 中固定 epoch 加消息序号，不使用运行时当前时间。gold 只引用稳定 event IDs。

LongMemEval 原始更新链若没有显式 lifecycle 标注，转换器按固定规则合成 checkpoint：每次同一事实的后续纠正产生“纠正前旧 event 可见、纠正后新 event 可见”的 current checkpoint，并保留一个纠正后的 historical checkpoint；含明确截止时间的事实在截止前后各生成一点；没有更新或期限的 case 只生成 ingest 后 checkpoint。规则版本写入 manifest/config hash，不能由 runner 临时推断。

## 固定子集

`core.ids.json` 由确定性分层抽样生成并提交：覆盖单会话、多会话、时间更新、知识更新、偏好和干扰项；每类至少 10 条，总数默认 80。manifest 固定：

- 数据集名称与上游 revision/下载 URL；
- source SHA-256；
- subset ID 文件 SHA-256；
- 转换器版本；
- 许可说明。

runner 对 hash 不匹配直接失败并给出重新获取指令，不静默换数据。基准正文是否提交仓库取决于上游许可；默认只提交 manifest 和 IDs，由用户通过 `--source` 指定下载后的 JSON。

## 三层评测

### Extraction

在临时 SQLite 中按事件顺序运行正式写入/提取管线，再通过 evidence link 将 Claim 映射回 gold event：

- evidence precision/recall/F1；
- claim yield：含 gold 的 event 是否产出至少一条 active/candidate Claim；
- temporal field accuracy：`occurred_start/end` 和 valid interval 是否覆盖 gold；
- schema validity 与 rejected/skipped reason 分布。

文本语义匹配默认使用固定 judge model + 固定 JSON prompt；同时报告无需 judge 的 evidence-link 指标。模型不可用时只产 deterministic 指标并把 judge 指标标为 `not_run`，不能当 0 分。

### Retrieval

对每个 case 用正式 recall，按最终 Claim 的 evidence event IDs 判定相关性：

- `Recall@k = 命中的相关 evidence 数 / gold evidence 数`，k 报告 1、5、10；
- `MRR = 1 / 第一条相关结果排名`，无命中为 0；
- `nDCG@k`：binary relevance，按 gold evidence 去重；
- temporal correctness：返回结果同时满足 case 的 valid-time 与 recorded-time gold 条件的比例，并单列“命中但时间错误”。

聚合同时给 micro、macro、按 category 分组值和 95% bootstrap CI（固定 seed）。

### Lifecycle

不依赖生成答案，使用合成自 gold timeline 的检查点：

- 过期前/后可见性；
- superseded/retracted Claim 在 current、historical、known_as_of 下的正确状态；
- TTL worker 的到期边界；
- decay/archive 不破坏历史可见性和 evidence chain。

每条断言输出 expected/actual IDs 和状态，便于回归定位。

## 只读 Runner 与可复现性

“只读”指不修改 source dataset 和用户生产 DB。runner：

1. 校验 source 与 subset hash；
2. 计算 `config_hash = sha256(canonical_json)`，canonical JSON 包括 git revision、prompt hash、adapter version、模型/provider、Settings 白名单、subset hash、seed；
3. 为每个 case 创建临时 DB，避免 namespace 串扰；
4. 固定 prompt、temperature=0、seed（provider 支持时）、并发度默认 1；
5. 运行结束关闭 DB；`--keep-db` 才保留到显式 output 目录；
6. 输出只写用户指定的报告目录。

禁止连接或迁移 `HL_MEM_DB_PATH` 指向的生产库；CLI 若 `--db` 与临时路径相同或不是 runner 创建的文件则拒绝。

## CLI

```text
hl-mem eval \
  --benchmark longmemeval \
  --subset core \
  --source D:/datasets/longmemeval.json \
  --output reports/longmemeval/core
```

可选：`--layers extraction,retrieval,lifecycle`、`--limit`（仅调试，报告标为非标准）、`--keep-db`。标准 core run 禁止覆盖 subset IDs、prompt 或 metric 参数。

## 报告格式

`report.json`：

```json
{
  "schema_version": 1,
  "benchmark": "longmemeval",
  "subset": "core",
  "config_hash": "...",
  "run": {"started_at": "...", "git_revision": "...", "models": {}},
  "metrics": {"extraction": {}, "retrieval": {}, "lifecycle": {}},
  "categories": {},
  "cases": [{"case_id": "...", "metrics": {}, "errors": []}]
}
```

`summary.md` 包含配置 hash、数据 hash、模型、三层总表、分类表、失败 case 列表和与可选 baseline JSON 的 delta。Markdown 完全从 JSON 渲染，避免两份计算逻辑。

## Migration 与依赖

无 migration，无运行时新依赖。JSON、hash、statistics、tempfile 使用标准库；现有 http/LLM 组件负责 judge。benchmark 源数据不进入 wheel，evaluation 模块可随包安装。

## 测试计划

- fixture 转换：角色、时间、稳定 ID、namespace、locator、缺失时间 fallback。
- manifest：source/subset hash 不匹配、未知 subset、重复 ID 明确失败。
- metrics：手算 Recall@k/MRR/nDCG、多个 Claim 指向同 event 时不重复计分、时间错误分离。
- runner：不写 source、不打开生产 DB、case 间隔离、固定 config hash、模型失败标记 `not_run`。
- reporting：JSON schema 字段、Markdown 数字与 JSON 一致、错误 case 可定位。
- CLI：目标命令解析、标准 subset 禁止非标准覆盖、退出码区分配置错误与 case 失败。
- 小型端到端 fixture（3–5 cases），不依赖网络和真实模型。

## 验收标准

- 同一 revision、数据、模型与配置产生相同 config hash 和 deterministic 指标。
- 报告可定位到 case、gold event 和返回 Claim/evidence。
- 评测代码没有 benchmark-specific 分支渗入 domain/application 生产逻辑。
