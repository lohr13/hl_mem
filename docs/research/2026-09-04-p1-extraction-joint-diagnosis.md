# P1 提取收缩联合诊断：v1.1.3 真复刻与根因裁决

日期：2026-09-04

## 1. 结论

本次结果必须拆成“提取量”和“40-case QA 分数”两个问题，不能继续用一个总分混判。

1. **提取收缩由 `e29275e` 代码包造成，置信度高。** 同一天、同一 `qwen3.7-plus@coding`、同一数据与配置下，真正加载 v1.1.3 `9c2adb` extractor 的全量重提取产生 **353 claims**；当天实际加载 v1.1.4 `297ffd` 的各次运行只有 **219–228 claims**。因此“服务端在 9 月 4 日把所有版本都压到约 220”已被反事实实验否定。
2. **v1.1.3 今天恢复了 310 量级，但没有在单次运行中恢复 0.925。** 真复刻为 **353 claims / accuracy 0.875**。所以 Q1 的精确回答是：提取量可以恢复并超过 310；`310 / 0.925` 这一对联合结果没有复现。预注册的两个简化分支也没有完整覆盖这个混合结果：它远非 `~220`，但 accuracy 低于 0.90。
3. **此前 Arm A / Arm B 都不是真 A/B。** Arm A 代码的预期 extractor version 是 `llm-v2+4e39d40156e3`，Arm B 是 `llm-v2+325e6ea74ca4`；两份产物的 16 个 manifest 却全部是主线 `llm-v2+297ffd68bf0a`，并且 ingest fingerprint 与主线、假 v1.1.3 复刻完全相同。故“改数字无效”“恢复 atomic/coverage-first 文案也无效”均不能由这两份产物推出。
4. **控制枚举合并行为的主要 prompt 成分是 `e29275e` 新增并重复的粗粒度指令，不是 hard cap。** 具体是 `context-rich memory`、`同一主题且生命周期相同的相关背景合并`、`不要机械拆分多个名词/数字/属性/从句`，以及末尾再次出现的同义规则。它们与仍保留的“枚举逐项提取”规则互相冲突，模型在真实输出中选择了合并。`overflow_truncated_count=0` 说明后处理 cap 没有删 claim；但 12/16 数字写进 prompt 仍可能作为生成前的软预算约束。
5. **删除 soft-split / delta-repair 不是 310→220 的解释。** v1.1.3 真复刻配置中两者均为默认 `False`，353 条来自首轮提取；无需恢复重试风暴路径。

## 2. 真复刻的环境证明

### 2.1 版本与导入边界

- worktree：`.worktrees/v113-checkout`
- commit：`1a7b46b33411ac188f1553a21f1dd1c34e7139ff`
- tag：`v1.1.3`
- 解释器：主仓绝对路径 `D:\workspace\hl_agent\hl_mem\.venv\Scripts\python.exe`
- CWD：`D:\workspace\hl_agent\hl_mem\.worktrees\v113-checkout`
- `PYTHONPATH`：`D:\workspace\hl_agent\hl_mem\.worktrees\v113-checkout\src`

启动付费运行前的实际探针输出为：

```text
hl_mem_file=D:\workspace\hl_agent\hl_mem\.worktrees\v113-checkout\src\hl_mem\__init__.py
e2e_module=D:\workspace\hl_agent\hl_mem\.worktrees\v113-checkout\tests\eval\chinese_e2e.py
llm_key_sk_sp=True
llm_provider=dashscope
llm_model=qwen3.7-plus
llm_base_url=https://coding.dashscope.aliyuncs.com/v1
extractor_mode=llm
chunk_target_chars=12000
missing_source_count=0
```

这同时排除了 editable install 回落到主仓 `src/`、错误 E2E 模块、错误模型/endpoint 和缺失私有语料。

### 2.2 配置、密钥与输出

- 配置源：`evaluation/tools/configs/e2e_cloud_qwen37plus.toml`
- 配置 SHA-256：`3D34DE2C61B44483DB84A329498A150D98053D26D342E80E559E086E7869395F`
- `.env`：运行时复制自主仓；其他 provider 密钥强制从该 `.env` 读取。
- `LLM_API_KEY`：从主仓 `.env` 取值并在单次 PowerShell 进程中显式赋给环境变量；只验证 `sk-sp-` 前缀，不打印密钥。
- `REFRESH=1`：最终报告为 `fresh_ingest: 16`。
- 报告：`var/eval/v113_true_recheck_20260904/report.json`
- 缓存：`var/eval/v113_true_recheck_20260904/cache/`
- 16 个缓存 manifest：全部为 `llm-v2+9c2adb1683ad`。

v1.1.3 测试当时尚无 `HL_MEM_CHINESE_E2E_CACHE_ROOT`，所以运行期间把 worktree 固定缓存目录建成指向上述主仓 cache 的目录联接。运行完成后只移除了联接、worktree 根目录临时 `hl_mem.toml` 和临时 `.env`；主仓 report/cache 均保留，16 个 manifest 完整。

## 3. 数据对照

claims 统一按每份报告引用的 16 个唯一 cache DB 执行 `SELECT COUNT(*) FROM claims` 后求和。这样不会把 9 月 2 日报告中 12 个 reused unit 的 `extracted_claims=0` 误当成空库。

| 运行 | 声称/实际代码 | cache | extractor version | claims | accuracy | R@5 | events accuracy | 结论 |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| 9-2 v1.1.3 原跑 | v1.1.3 / v1.1.3 | reused 12 + stale 4 | `9c2adb` | 310 | 0.925 | 0.9625 | 1.000 | 历史基线 |
| 9-4 v1.1.4 fresh | v1.1.4 / v1.1.4 | fresh 16 | `297ffd` | 221 | 0.825 | 0.9125 | 0.625 | 收缩复现 |
| 9-4 postfix fresh | v1.1.4 / v1.1.4 | fresh 16 | `297ffd` | 223 | 0.850 | 0.9500 | 0.500 | cap 观测为 0 |
| 9-4 “Arm A” | Arm A / **主线 v1.1.4** | fresh 16 | **实际 `297ffd`；应为 `4e39d4`** | 222 | 0.875 | 0.9500 | 0.625 | 无效 A/B 产物 |
| 9-4 “Arm B” | Arm B / **主线 v1.1.4** | fresh 16 | **实际 `297ffd`；应为 `325e6e`** | 228 | 0.875 | 0.9750 | 0.500 | 无效 A/B 产物 |
| 9-4 “v1.1.3 recheck” | v1.1.3 / **主线 v1.1.4** | fresh 16 | **实际 `297ffd`；应为 `9c2adb`** | 219 | 0.825 | 0.9750 | 0.500 | 已撤回的假复刻 |
| 9-4 v1.1.3 真复刻 | v1.1.3 / v1.1.3 | fresh 16 | `9c2adb` | **353** | **0.875** | **0.9625** | **0.750** | 提取量恢复，QA 未恢复到 0.925 |

真复刻的其他关键统计：

- 353 extracted / 353 stored / 0 skipped。
- PerLTQA 四个 persona：`57 / 60 / 56 / 60`，合计 233。
- 12 个 MemDaily：合计 120。
- extraction output tokens：49,233；Arm B 假产物为 32,880。
- extraction coverage：1.0；40/40 case 无运行错误。
- pytest 总耗时：1,420.38 秒。
- gate 失败项：overall accuracy `0.875 < 0.90`；`memdaily_noisy.R@5 = 0.25 < 0.50`。

9 月 2 日基线与本次真复刻的总体 R@5、MRR 恰好相同，分别为 `0.9625` 和 `0.958333...`，但 QA 从 37/40 降到 35/40。四个发生答案判定变化的 PerLTQA case，其 gold retrieval 都仍是 R@5=1、MRR=1：一个从错变对，三个从对变错，净 -2。这支持“单次 reader/生成采样参与分数波动”，同时也提醒：不同原子 claim 样本会改变 reader 上下文，不能只凭一轮把全部 -5pp 严格归因给服务端。

因此 Q1 的裁决分层如下：

| 子问题 | 裁决 | 置信度 |
| --- | --- | --- |
| 今天 v1.1.3 是否仍能产生 310 量级？ | 是，实际 353 | 高 |
| 219–228 的提取收缩是否由当天服务端普遍变化造成？ | 否 | 高 |
| `e29275e` 整体是否造成提取收缩？ | 是 | 高 |
| 今天能否单次复现 0.925？ | 本次不能，实际 0.875 | 高（对本次事实） |
| 0.925→0.875 是否纯服务端/时间漂移？ | 有同日共同波动证据，但未被严格单变量隔离 | 中 |

## 4. Q2：双臂为什么拉不回提取量

### 4.1 首要原因：双臂根本没有运行臂代码

`LLM_EXTRACTOR_VERSION` 是 prompt、response schema 与后处理规则的联合 hash，不是手写标签。直接从两个 arm worktree 导入得到：

| worktree | commit | 应有 version | 报告 16/16 manifest 实际 version |
| --- | --- | --- | --- |
| `p1-extraction-arm-a` | `77db153` | `llm-v2+4e39d40156e3` | `llm-v2+297ffd68bf0a` |
| `p1-extraction-arm-b` | `8dd748d` | `llm-v2+325e6ea74ca4` | `llm-v2+297ffd68bf0a` |

两臂报告的 `ingest_config_fingerprint` 也都是 `9039fd8d...`，与主线 fresh、postfix 和假 v1.1.3 recheck 完全相同；真 v1.1.3 是 `88d2a7c0...`。

`scripts/hlmem-python.sh` 会先清除 `PYTHONPATH`，再在第 17 行 `cd "$repo_root"`。当为复用主仓 venv 而调用主仓脚本时，CWD 与 editable install 会共同把执行重新绑定到主仓。A/B 产物的 hash 证明同类污染也发生在两个 arm 上。

所以不能再解释为“Arm B 已恢复 atomic 文案，但模型仍坚持合并”。正确说法是：**Arm B 的 intended prompt 从未到达模型。** 两臂必须作废；是否重跑应由修复决策决定，不应把现有 222/228 当作臂效果。

### 4.2 实际控制合并行为的 prompt 成分

`e29275e` 把 prompt 的主叙事从“原子事实”改成“上下文完整记忆”，并在高显著位置重复以下规则：

- 一条 claim 对应一个可独立更新、冲突或遗忘的**主题**；同一主题且生命周期相同的背景合并。
- 不要仅因一句话含多个名词、数字、属性或从句就机械拆分。
- 数值保留在“对应记忆”中，不要为了数字另建 claim。
- 末尾再次要求合并同主题背景，并再次告诫不要因多个名词、数字或从句拆分。
- 普通目标 12、hard maximum 16，并按未来用途/显著度排序。

虽然中间仍有“枚举中的每个可独立回答项分别保留”的规则，但它与上述多次出现的 general rule 冲突。动物、植物、威胁都天然会被模型解释成“同主题、同生命周期”，于是枚举被压成类别摘要。

关键事件 `perltqa:e2e:227a5ff7fc9e83f1752725c9` 的 DB 证据为：

| 运行 | event-linked claims | 形态 |
| --- | ---: | --- |
| 9-2 v1.1.3 | 18 | 每种动物、鸟、植物、威胁基本单独成条 |
| 假 Arm B（实际 `297ffd`） | 7 | 动物一条枚举、植物一条枚举、威胁一条复合摘要 |
| 假 v1.1.3（实际 `297ffd`） | 7 | 同样的类别合并 |
| 真 v1.1.3（实际 `9c2adb`） | 17 | 每种动物、鸟、植物、威胁重新分条 |

例如假 Arm B 输出“栖息在亚马逊雨林中的动物包括巴西瓦奥卡、安哥拉蟒、绿翅蜂鸟和美洲豹等”；真 v1.1.3 则分别输出四条。这个同事件对照把“粗粒度 prompt → 枚举合并”的机制链闭合了。

### 4.3 cap、schema 与补提取路径的地位

- 所有带 cap 遥测的 v1.1.4 跑批均 `overflow_truncated_count=0`：没有 raw response 被 `cap_extraction_claims()` 截掉。
- 真 v1.1.3 本次每 event 最多 20 条；9 月 2 日缓存最多 21 条，均低于 24/30。因此对这批语料，30→24 不会触发确定性删除。
- 但 prompt 中的 “usually <=12 / max16” 是生成前指令，模型可以主动少写；“cap 未触发”不能排除数字文案的软约束效应。
- v1.1.3 的 `soft_split_enabled` 和 `delta_repair_enabled` 默认均为 `False`，本次配置未开启。353 条是首轮产物，删除这两条默认关闭的路径不是主因。

## 5. Q3：干净复刻方案

以下是本次已验证的 Windows/PowerShell 方案。核心原则是不用 `hlmem-python.sh`，并把“运行前身份”和“产物身份”都设为 hard gate。

```powershell
$main = 'D:\workspace\hl_agent\hl_mem'
$wt = "$main\.worktrees\v113-checkout"
$python = "$main\.venv\Scripts\python.exe"
$output = "$main\var\eval\v113_true_recheck_20260904"

# v1.1.3 test 默认从 worktree 根目录读这两个文件。
Copy-Item "$main\evaluation\tools\configs\e2e_cloud_qwen37plus.toml" "$wt\hl_mem.toml"
Copy-Item "$main\.env" "$wt\.env"

# 从主仓 .env 取 sk-sp 键，放入本次进程 env；不要打印值。
$line = Get-Content "$main\.env" |
  Where-Object { $_ -match '^\s*(?:export\s+)?LLM_API_KEY\s*=' } |
  Select-Object -Last 1
$llmKey = ($line -replace '^\s*(?:export\s+)?LLM_API_KEY\s*=\s*','').Trim().Trim('"').Trim("'")
if (-not $llmKey.StartsWith('sk-sp-')) { throw 'wrong LLM key' }
$env:LLM_API_KEY = $llmKey

# 让其余 provider 密钥来自刚复制的主仓 .env，避免父进程 env 污染。
'EMBEDDING_API_KEY','RERANKER_API_KEY','IMAGE_API_KEY' |
  ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }

$env:PYTHONPATH = "$wt\src"
$env:HL_MEM_CHINESE_E2E_REFRESH = '1'
$env:HL_MEM_CHINESE_E2E_REPORT = "$output\report.json"

# v1.1.3 test 无 cache-root env；用目录联接把固定路径映射到主仓输出。
New-Item -ItemType Directory "$output\cache" -Force | Out-Null
New-Item -ItemType Directory "$wt\var\eval" -Force | Out-Null
New-Item -ItemType Junction `
  "$wt\var\eval\chinese_e2e_cache" `
  "$output\cache" | Out-Null

Push-Location $wt
try {
  & $python -c "import hl_mem; print(hl_mem.__file__)"
  if ($LASTEXITCODE -ne 0) { throw 'import preflight failed' }

  & $python -m pytest tests/eval/test_chinese_e2e.py -m real_api -s -q --tb=short
  $testExit = $LASTEXITCODE
} finally {
  Pop-Location
}
```

付费运行前必须进一步把期望值写成断言，而不只是肉眼看：

```text
resolved hl_mem.__file__ starts with <worktree>\src\hl_mem\
resolved tests.eval.chinese_e2e.__file__ starts with <worktree>\tests\eval\
git HEAD == expected commit
LLM_EXTRACTOR_VERSION == expected hash
model/base_url/config SHA-256 == preregistered values
LLM_API_KEY prefix == sk-sp-（只输出布尔值）
```

运行后再检查 16/16 manifest 的 `extractor_version` 全部等于期望值；任何一个不符，整次运行自动标记 invalid，不进入质量比较。pytest 因质量门禁退出 1 不等同于实验中止，仍需检查报告 `status=completed`、40 cases、16 fresh manifests 和 16 DB。

## 6. 修复建议

建议按“复杂度低、性价比高、机制合理”排序，不恢复默认关闭的补提取重试链。

| 优先级 | 建议 | 复杂度 | 性价比 | 合理性/边界 |
| --- | --- | --- | --- | --- |
| P0 | **先给付费评测加执行身份 hard gate**：git SHA、`hl_mem.__file__`、E2E 模块路径、extractor hash、config hash、16/16 manifest hash | 低 | 极高 | 已发生三次同类污染（两臂 + 假复刻）；没有身份 gate 的 A/B 不可审计 |
| P0 | **撤销当前 prompt 的粗粒度主叙事**：恢复 atomic、逐事实拆分、最小自包含 answerable span；删除/中和三处“同主题合并/不要拆分” | 低 | 高 | 真 v1.1.3 在同日、同事件恢复 17/18；这是最直接的机制修复 |
| P0 | **保留 `e29275e` 的 bounded/no-retry 安全机制**，不要恢复 soft-split/delta-repair 默认路径 | 低 | 高 | 本次 353 不依赖补提取；避免重新引入 retry storm 与成本失控 |
| P1 | **重新做有效的 prompt/cap A/B**：至少比较 atomic+cap24 与 atomic+cap30；付费前校验 expected hash | 低到中 | 高 | 现有 A/B 作废，不能据其选择 24 或 30；当前样本每 event 最大 20/21，24 看似足够，但仍需真实验证 prompt 软预算效应 |
| P1 | **门禁增加 semantic-atom/per-event density**：保留 event coverage，同时加入关键枚举逐项率、answer-bearing relation/attribute coverage 和 event claim-count drift | 中 | 高 | 现有 extraction coverage 只要每个 gold event 有一条 claim 就是 1.0，会漏报 18→7 |
| P1 | **把 extractor 与 reader 评价拆开**：冻结 extraction cache 后重放 reader，或多次 reader 取分布；不要用单次 overall accuracy 判断 extractor 因果 | 中 | 中高 | 9-2 与真复刻 R@5/MRR 相同而 QA 净 -2；单轮生成有噪声 |
| P2 | 只有 atomic+bounded 仍欠提取时，再评估按 source density 的动态预算或一次有界 completeness repair | 高 | 中 | 当前证据已显示 prompt 可以首轮恢复 353，暂不值得先上复杂补偿链 |

最小合理修复不是“单独把 16 改 30”，而是：**恢复 atomic 语义、保留 bounded safety、先用有效 A/B 决定 24/30，再用语义覆盖门禁防回归。**

## 7. 最终裁决

- **Q1**：v1.1.3 今天能恢复 310 量级（实际 353），所以 `e29275e` 对提取收缩的代码因果坐实；0.925 本次未复现，QA/reader 波动需独立处理。
- **Q2**：A/B 没拉回不是两种修复都无效，而是两臂都实际跑了主线 `297ffd`。主线中重复的 context-rich / same-topic merge / anti-split 指令控制了枚举合并；真 `9c2adb` 已把关键事件从 7 恢复到 17。
- **Q3**：直接调用绝对 Python、worktree CWD、显式 `PYTHONPATH`、运行前 import/hash 断言、运行后 16/16 manifest hash 复核，缺一不可。

