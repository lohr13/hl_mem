# HL-Mem 配置管理重构方案：TOML 单一配置源

状态：最终设计已确认，待实施。

源码基线：2026-07-30；`pyproject.toml` 与 `src/hl_mem/__init__.py` 当前均为 `0.17.4`。目标版本为 `0.18.0`。

## 1. 最终配置契约

- 运行配置文件固定为 `hl_mem.toml`，默认从当前工作目录查找。
- `hl_mem.toml` 必须存在；缺失立即报错，不使用代码默认值继续启动。
- 非敏感配置只来自 `Settings` 的一套静态默认值和 TOML。
- 默认值不区分运行环境：Extractor、Embedder 默认 `fake`，Reranker、Image Describer 默认 `off`。
- 删除 `Settings.environment`、`Environment` 及其条件分支。
- 删除 `allow_fake_fallback`。启用真实组件必须在 TOML 中显式选择；真实组件失败时不得自动切换为 fake。
- `.env` 只保存四个独立密钥：
  - `LLM_API_KEY`
  - `EMBEDDING_API_KEY`
  - `RERANKER_API_KEY`
  - `IMAGE_API_KEY`
- 同名进程环境变量可以覆盖 `.env` 中的密钥。其他环境变量一律不参与配置解析。
- 任意 `HL_MEM_*` 变量均不读取、不提示，存在与否不影响运行结果。
- TOML 中的未知表或未知键直接失败。
- 每个进程只加载、校验一次配置，并将同一个 `Settings` 快照显式注入 API、Worker、MCP、Hermes 和存储组件。
- 本次改造是直接生效的 breaking change，发布版本固定为 `0.18.0`。

## 2. 当前问题与改造范围

### 2.1 `Settings` 契约不统一

当前 `Settings.from_env()` 同时承担环境变量解析、按 `environment` 改变默认值、校验和实体别名初始化，导致：

- `Settings()`、`Settings.from_env()`、`.env.example` 和启动脚本的默认值不一致。
- 同一字段会因运行环境得到不同默认值。
- 配置解析产生 `set_active_aliases()` 业务副作用。
- 非敏感配置散落在环境变量、模块常量和函数默认参数中。

目标改造：

- 删除 `Settings.from_env()` 及其调用点，正式入口统一使用 `load_settings()`。
- `Settings` 只保留一套静态安全默认值。
- 实体别名初始化移到进程装配层。
- `Settings.validate()` 只校验值、类型和字段间约束，不读取或判断运行环境。
- 所有测试通过显式构造 `Settings` 或命名依赖注入表达场景，不再依赖环境标记。

### 2.2 默认值决议

以下值作为唯一权威默认值；示例文件可以显式启用真实能力，但不得把示例值描述为默认值。

| 配置 | 默认值 |
|---|---:|
| LLM model | `glm-5.2` |
| LLM timeout | `90` |
| LLM structured mode | `json_object` |
| Extractor mode | `fake` |
| Embedder mode | `fake` |
| Reranker mode | `off` |
| Image Describer mode | `off` |
| Query expansion | `off` |
| Relation discovery | `off` |
| Cross-subject dedup threshold | `0.92` |
| Daily token limit | `500000` |

### 2.3 需要进入 `Settings` 的配置

- 将 `RECALL_DEFAULT_LIMIT` 提升为 `Settings.recall_default_limit`，TOML 路径为 `recall.default_limit`。
- 将 `RECALL_VECTOR_SCAN_LIMIT` 提升为 `Settings.recall_vector_scan_limit`，TOML 路径为 `recall.vector_scan_limit`。
- 将 6 个旁路读取模块缺失的 Hermes、entity aliases、database pool、database busy timeout、decay 和 access bonus 字段补入 `Settings`。
- `RETENTION_DAYS` 等部署参数由装配层从 `Settings` 显式传入。
- `config.py` 只保留真正不随部署变化的算法常量。

三个 dedup 阈值继续保持独立语义：

| 名称 | 默认值 | 生效位置 | 语义 |
|---|---:|---|---|
| `DEDUP_SEMANTIC_THRESHOLD` | `0.82` | `domain/claims/dedup.py` | 单次写入期、同一 subject 候选的 best-match 算法阈值 |
| `Settings.dedup_threshold` / `dedup.threshold` | `0.92` | `workers/deduplicate.py` | 后台跨 subject 去重候选阈值 |
| `Settings.recall_dedup_threshold` / `recall.dedup_threshold` | `0.95` | `recall/staged_pipeline.py` | 召回结果内同义 claim 折叠阈值；`0` 表示关闭 |

### 2.4 需要消除的旁路

| 位置 | 改造 |
|---|---|
| `adapters/hermes/provider.py` | 构造时接收 `Settings` 或明确的 Hermes 参数 |
| `domain/entity.py` | `load_entity_aliases(path)` 只接收路径 |
| `storage/database.py` | 显式接收 path、pool size、busy timeout |
| `storage/claims.py` | `helpful_rates()` 的 `min_samples` 由调用方传入 |
| `workers/decay.py` | decay、archive、access bonus、feedback 参数全部显式传入 |
| `workers/ttl.py` | feedback mode 与 short TTL 全部显式传入 |

完成后，上述模块不得直接读取进程环境，也不得保留可调参数的第二套默认值。

## 3. 目标设计

### 3.1 唯一 schema

继续使用 `src/hl_mem/settings.py::Settings` 作为唯一 schema，不新增 `schema.py` 或嵌套配置对象。字段 metadata 只允许以下两种键：

- `toml`：非敏感配置的 TOML 完整路径。
- `secret_env`：密钥使用的固定环境变量名。

示例：

```python
llm_model: str = field(
    default="glm-5.2",
    metadata={"toml": "llm.model"},
)
```

四个密钥字段均设置 `repr=False`：

| `Settings` 字段 | `secret_env` |
|---|---|
| `llm_api_key` | `LLM_API_KEY` |
| `embedding_api_key` | `EMBEDDING_API_KEY` |
| `reranker_api_key` | `RERANKER_API_KEY` |
| `image_describer_api_key` | `IMAGE_API_KEY` |

loader 通过 `dataclasses.fields(Settings)` 生成键映射和未知键集合，不维护第二份 schema。`IMAGE_API_KEY` 是图片描述器的独立密钥，不回退到 `LLM_API_KEY`。

### 3.2 加载模型

建议接口：

```python
def load_settings(
    config_path: Path | None = None,
    env_path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    ...
```

固定加载顺序：

1. 构造 `Settings` 静态安全默认值。
2. 读取 TOML 非敏感配置并覆盖默认值。
3. 读取可选 `.env` 中的四个固定密钥。
4. 用同名进程环境密钥覆盖 `.env`。
5. 调用 `Settings.validate()`。

约束：

- `config_path is None` 时使用 `Path.cwd() / "hl_mem.toml"`；不搜索项目根目录或其他目录。
- 默认路径和显式路径只要不存在，都抛出 `ConfigurationError`。
- `.env` 默认路径为当前工作目录的 `.env`；该文件可以缺失。
- TOML 只接受 metadata 声明的完整键路径；未知表、未知键和重复语义键直接失败。
- 四个密钥不得写入 TOML；`.env` 和进程环境只识别四个精确名称。
- 其他环境变量一律忽略。
- TOML 使用原生类型；除 `list -> tuple` 和字符串枚举外不做宽松强制转换。
- 错误信息包含文件路径、完整键路径和期望类型，不输出密钥值。
- loader 只返回 `Settings`，不创建组件、不加载实体别名、不修改 `os.environ`。

### 3.3 TOML 示例

以下结构只展示常用项，完整字段以配置参考文档为准：

```toml
[database]
path = "var/hl_mem.db"
pool_size = 8
busy_timeout_seconds = 30

[llm]
provider = "dashscope"
base_url = "https://coding.dashscope.aliyuncs.com/v1"
model = "glm-5.2"
structured_mode = "json_object"
timeout = 90
max_attempts = 3

[embedding]
mode = "fake"
model = "text-embedding-v4"
dim = 2048

[reranker]
mode = "off"
base_url = "https://dashscope.aliyuncs.com"
model = "gte-rerank-v2"

[image_describer]
mode = "off"
provider = "dashscope"
base_url = "https://coding.dashscope.aliyuncs.com/v1"
model = "qwen3.7-plus"
timeout_seconds = 20.0

[extraction]
mode = "fake"
pre_filter = false
chunk_target_chars = 12000

[recall]
default_limit = 20
vector_scan_limit = 200
packed_context_token_budget = 2000
query_expansion_mode = "off"
tag_channel_enabled = false

[dedup]
enabled = true
threshold = 0.92
audit_only = true

[retention]
temporal_ttl_days_low = 3
temporal_ttl_days_normal = 7
temporal_ttl_days_high = 14

[worker]
poll_interval = 2.0
maintenance_interval = 600
job_lease_minutes = 5

[relation]
discovery_mode = "off"

[hermes]
enabled = false
url = "http://127.0.0.1:8200"
timeout = 30
```

`.env` 只包含：

```dotenv
LLM_API_KEY=
EMBEDDING_API_KEY=
RERANKER_API_KEY=
IMAGE_API_KEY=
```

TOML 中将 `extraction.mode`、`embedding.mode`、`reranker.mode` 或 `image_describer.mode` 改为真实能力模式，才表示显式启用相应组件。

### 3.4 校验职责

loader 负责结构校验：

- 文件存在性和 TOML 语法。
- 表名、键名和类型。
- 密钥未进入 TOML。

`Settings.validate()` 负责值级校验：

- 字段运行时类型。
- 正数、非负数、百分比和阈值范围。
- 枚举允许值。
- 上下界顺序和互斥字段组合。
- 真实组件启用时所需 URL、模型名和对应独立密钥是否存在。

所有校验在数据库、网络客户端、后台线程创建前完成，不包含任何运行环境判断。

### 3.5 统一装配

| 入口 | 改造 |
|---|---|
| `components.py` | `initialize_process(settings)` 只执行 entity aliases 等显式、幂等的进程初始化 |
| `api/server.py` | app 工厂接收已经加载的 `Settings` |
| `workers/worker.py` | `Worker` 正式构造只接收 `Settings`；测试依赖改为命名参数 |
| `mcp/server.py` | `McpMemoryServer` 接收 `Settings`，独立进程入口只加载一次 |
| `adapters/hermes/plugin` | 独立入口加载一次，再构造 provider |
| `start_server.py` | 加载一次，同一对象传给 Worker 和 FastAPI app |
| `cli.py`、`doctor.py` | 调用统一 loader；命令行覆盖使用 `dataclasses.replace()` 显式表达 |

删除 Worker 的通用 `config: dict`。fake extractor、audit logger 等测试依赖使用清晰的命名参数注入，不能形成第二配置源。

## 4. 实施阶段

### 4.1 阶段 1：收敛 `Settings` 契约

目标：先建立唯一字段、默认值和校验契约。

任务：

- 删除 `environment`、`Environment`、`allow_fake_fallback` 和所有相关分支。
- 删除 `Settings.from_env()` 及配置解析中的实体别名副作用。
- 统一为第 2.2 节的静态安全默认值。
- 补齐 6 个旁路模块所需字段。
- 将 `RECALL_DEFAULT_LIMIT`、`RECALL_VECTOR_SCAN_LIMIT` 移入 `Settings`。
- 清理 `config.py` 中的死常量和部署参数默认值。
- 将 `Settings.validate()` 收敛为与运行环境无关的值级校验。
- 更新 settings、recall、dedup 和默认值单元测试。

### 4.2 阶段 2：实现 loader 并统一装配

目标：建立强校验的 TOML + secret 加载边界，消除旁路和重复装配。

任务：

- 新增 `src/hl_mem/config_loader.py`，实现第 3.2 节的加载模型。
- 为 `Settings` 字段补齐 `toml` 或 `secret_env` metadata。
- 缺失 TOML、未知键、错误类型和 TOML 密钥全部启动失败。
- 消除第 2.4 节列出的旁路读取。
- 统一 API、Worker、MCP、Hermes、CLI、doctor 和 `start_server.py` 的装配。
- 删除 Worker 的 `config: dict`，改用命名参数注入测试依赖。
- 增加 loader、装配、doctor、provider、decay 和 TTL 单元测试。

loader 测试至少覆盖：

- 默认 TOML 路径缺失和显式路径缺失。
- 空 TOML、合法配置及全部支持的 TOML 类型。
- TOML 语法错误、未知表、未知键、错误类型和 TOML 中出现密钥。
- 四个 `.env` 密钥、进程环境覆盖 `.env`、占位密钥和错误信息脱敏。
- `IMAGE_API_KEY` 与 `LLM_API_KEY` 相互独立。
- 任意非密钥环境变量不影响加载结果。
- 同一进程的 API、Worker 和 MCP 复用同一个配置快照。

### 4.3 阶段 3：示例、文档与发布

目标：发布完整的新配置契约。

任务：

- 新增 `config.example.toml`，展示常用参数和显式启用真实能力的写法。
- 重写 `.env.example`，只保留四个独立密钥。
- 更新 `README.md`、`docs/configuration.md`、`docs/README.md`、`docs/api.md` 和 `docs/architecture.md` 的当前配置说明。
- 在 `docs/configuration.md` 中列出每个 TOML 键的类型、默认值、允许值和对应 `Settings` 字段。
- 更新 `docs/CHANGELOG.md`，明确 `0.18.0` 的 breaking change 和新启动要求。
- 将 `pyproject.toml` 与 `src/hl_mem/__init__.py` 版本同步为 `0.18.0`。

## 5. 总体验收标准

- 默认从当前工作目录读取 `hl_mem.toml`，文件缺失直接报错。
- TOML 中任何未知表、未知键、错误类型或密钥都直接报错。
- `Settings` 不含运行环境字段或动态默认逻辑。
- `Settings.validate()` 只做类型、正数、范围和字段组合校验。
- 默认模式为 Extractor/Embedder `fake`、Reranker/Image Describer `off`。
- 真实组件只能通过 TOML 显式启用，不存在自动 fake 回退。
- `Settings` metadata 只使用 `toml` 和 `secret_env`。
- `.env` 与进程环境仅接受四个密钥；`IMAGE_API_KEY` 不复用其他密钥。
- 任意 `HL_MEM_*` 变量不会改变配置或产生提示。
- `recall.default_limit` 和 `recall.vector_scan_limit` 控制所有召回入口。
- Worker 仅通过 `Settings` 和命名参数获取配置与测试依赖。
- 每个进程只产生一个经过校验的 `Settings` 快照。
- `rg -n "os\\.getenv|os\\.environ" src/hl_mem` 只允许命中 loader 的四密钥读取边界。
- API、Worker、MCP、Hermes、CLI、doctor 与启动脚本不各自实现解析逻辑。
- `config.example.toml` 可由 loader 直接加载。
- `.env.example` 的变量名精确等于四个固定密钥。
- 配置参考中的每个 TOML 键都能反查到唯一 `Settings` 字段。
- README 明确 `hl_mem.toml` 必须位于当前工作目录，缺失无法启动。
- 相关单元测试和完整 `tests/unit/` 通过。
- `pyproject.toml`、`__version__` 和 CHANGELOG 版本均为 `0.18.0`。

## 6. 明确不做

- 不做兼容层；不读取、不提示、不转换任何 `HL_MEM_*` 非密钥变量。
- 不提供“缺少 TOML 仍可启动”的隐式行为。
- 不提供按运行环境切换默认值的字段或逻辑。
- 不提供真实组件失败后自动切换 fake 的行为。
- 不做多个 TOML、profile、环境继承或 per-tenant 配置。
- 不做 include、alias、变量插值、配置指纹、diff 或热加载。
- 不新增 Pydantic Settings、第三方 TOML 库或第二份 schema。
- 不把全部字段倾倒进 `config.example.toml`。
- 不引入云 secret manager、密钥加密或 key rotation。
- 不改变 dedup、recall、TTL、decay 算法，只统一参数来源。
- 不修改数据库 schema。

## 7. 工作量

| 阶段 | 预估 |
|---|---:|
| 阶段 1：`Settings`、默认值、recall 配置与测试 | 1–1.25 人日 |
| 阶段 2：loader、旁路清理、统一装配与测试 | 2.5–3.25 人日 |
| 阶段 3：示例、文档、版本与发布检查 | 0.5–1 人日 |
| **总计** | **4–5.5 人日** |

估算包含代码评审修订和单元测试，不包含真实 Provider 联调或性能 benchmark。
