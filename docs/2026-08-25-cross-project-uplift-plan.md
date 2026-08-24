# 会话接续无损化：六项目对比分析与提升方案

- 文档日期：2026-08-25
- 状态：**P1 已实施并验证**；P2–P6 待确认
- 适用版本基线：agent-handoff 2.3.0 → 本次发布 2.4.0

## 0. 调研方法与证据边界

本次由 8 个只读子代理并行探索，主对话不通读源码。工具实际使用情况：

| 工具 | 版本/状态 | 用途 |
| --- | --- | --- |
| rtk | 0.45.0 | 终端输出过滤 |
| graphify | 0.9.43，已有 `graphify-out/graph.json`（336 节点） | 项目A 符号与调用关系定位 |
| gh | 已认证 | GitHub 同类项目检索与元数据核实 |
| Context Mode | 可用 | 大输出沙箱分析、跨会话时间线检索 |
| MemSearch | 命中项目A 历史会话记录 | 回溯既有决策 |
| ripgrep | **未安装** | 已降级为 PowerShell `Select-String`，未擅自安装 |

对照项目：A=本项目，B=codex-history-vscode，C=cc-switch，D=vscode，E=claude-code（公开仓库），F=codex（Rust 源码）。

证据分级说明：
- **硬事实**：子代理在源码中读到并给出 `file:line`。
- **双源事实**：两个独立项目或项目+外部仓库互相印证。
- **推断**：由负面搜索结果或行为特征得出，未在代码中直接证实。本文逐处标注。

---

## 1. 结论摘要

项目A 在**上下文耗尽判定**与**本地服务安全姿态**两项上已领先全部对照项目，无需向外借鉴。真正的短板集中在三处：

1. **正确性**：Codex token 字段读错、`.jsonl.zst` 压缩历史可能整体漏扫、`gitops` 前缀剥离 bug。这三项直接影响输出正确性，优先级最高。
2. **保真度**：`transcript.py` 的完整对话渲染**从未进入 handoff 文档**，工具调用参数、错误正文、MCP 工具名、逐轮时间戳全部丢失。这与项目定位"无损接续"存在实质落差。
3. **可观测性与首次上手**：评分理由不可见、无 doctor 自检、无时间线画布、无持久索引。

生态位判断：无人做"扫盘上 transcript → 按耗尽度排序 → 生成无损文档 + 恢复提示"这一组合（推断，基于负面搜索结果）。最接近的三类项目各缺一环——会话内 skill 无扫描能力，`claude-mem`（91696★）用有损 AI 压缩，`abtop`（3468★）算得出耗尽度但从不导出。

---

## 2. 项目A 已显著超越的亮点（重构中必须保住）

以下每一项都在对照项目中找不到对等实现，属于本项目的净资产。任何改动若削弱其中任意一项，即为负向调整，应当拒绝。

### 2.1 上下文耗尽判定
- token 基线分级，压缩次数作为**硬历史事实**参与抬档，文件大小仅作兜底（`core/vitals.py:191-245`）。B 完全没有 token 计数；C 也没有；F 的百分比是运行时自算，不做跨会话排序。
- 抬档地板有实测依据：压缩 ≥2 → critical、≥1 → high；fatal+aborted ≥3 → high（`vitals.py:236,242`）。
- 兜底路径的误差被显式写进注释而非隐藏：1.0MB/194k tokens 判"healthy"、1.9MB 含 10 次压缩判"watch"（`vitals.py:60-63,202-206`）。

### 2.2 压缩窗口全量保留
`_DigestCollector.digest`（`vitals.py:698`）保留**所有**压缩窗口、不截断、彼此分隔，并从 Codex `replacement_history` 逐字捞回用户原话（`:741`）。六个项目中唯一真正针对"压缩即失真"下功夫的实现。F 的 rollout 虽然物理上保留 `replacement_history`，但 Codex 自身不做跨窗口聚合展示。

### 2.3 单一强制脱敏出口
`core/report.py:238` `_redact` 是唯一出口，覆盖密钥、本地 home 的各种 shell 形态（`_home_variants:26`）、**外来用户名含 Claude slug 目录**（`:180`）。C 的脱敏只在前端日志（`lib/frontendLogger.ts:9-32`），项目A 是全出口收口，强度更高。

### 2.4 拒绝输出必然失败的命令
`SessionRow.resume_cmd`（`vitals.py:386`）对 archived/外来会话直接拒发命令，而非吐一条注定报错的命令行。B 的 `getResumeCommand`（`extension.ts:705`）靠拆 ID 字符串拼命令，archived 才用模态框拦。

### 2.5 不可逆操作的守卫
- 搜索不可信时拒绝勾选计划复选框（`core/evidence.py:389`）——防止不可逆误标。
- 测试超时**先**判定再跑摘要正则，被杀的测试绝不会被报成通过（`core/probe.py:112`）。
- 并发检测分阻断/建议两级，避免逼用户上 `--force`（`core/gitops.py:221`）。

### 2.6 本地服务安全姿态（全场最佳）
`gui/server.py`：环回绑定 127.0.0.1（`:572-588`）、每进程 `secrets.token_urlsafe(24)` + `compare_digest`（`:58,161-167`）、Host+Origin 校验挡 DNS rebinding（`:149-159`）、CSP/nosniff/no-referrer（`:180-192`）、GET/POST 读写分离、静态根穿越守卫、`/api/session-md` 钉死在 agent 数据根且"不存在"与"不允许"响应故意无法区分（`:432-466`）。
对比：C 的 Tauri webview 未见 CSP；B 的 webview `enableScripts: true`、算了 nonce 但**无 CSP meta 标签**、且用 `innerHTML` 拼接会话数据（`extension.ts:559,1168`）。

### 2.7 零依赖与零前端构建
`pyproject.toml` 的 dependencies 注释给出了理由：工具存在的前提是环境已经出问题，依赖越少越能启动。这是经过论证的取舍，不是偷懒。

### 2.8 工程纪律
- CI 测**安装后的产物**：wheel 装进干净 venv、干净 cwd 冒烟测 `--version`、三种 `--lang`、`--sweep`、`find_guide()`（`.github/workflows/ci.yml:352-381`）。
- 指南新鲜度门禁：重建后 diff 非空即失败（`ci.yml:77-81`）；版本/徽章漂移门禁（`:82-130`）。
- 三语指南**生成而非手写**，与 `--help` 共用 `cli.arg.*` 键，文档无法与解析器漂移（`scripts/build_guide.py:106-142`）。
- i18n 检查器用 `tokenize` 豁免注释/文档字符串，确保无 CJK 字面量绕过字符串表（`scripts/check_i18n.py:46-86`）；474 键 × 3 语言双向校验。
- 语言切换时服务端**重跑**生成结果，不留混语输出（`gui/static/app.js:709-730`）。
- 色带用颜色 + 文字徽章双通道编码，色盲不依赖色相（`style.css:268-293`）；写入警告用边框+颜色+措辞三重（`:242-252`）；有实测对比度 4.42 的修正记录（`:338-340`）。
- **注释文化**：几乎每个非显然分支都记录了促成它的实测数据或事故。这是本代码库最独特的资产，重构时逐条保留。

---

## 3. 正确性缺陷（优先级最高，全部有出处）

### D1 — Codex token 取了 `input_tokens` 而非 `total_tokens`（已修）
**先纠正一个误判**：外部调研建议"改读 `total_token_usage.total_tokens`"，这条**不适用**。项目A 的注释（`vitals.py:836-837`）判断正确——`last_token_usage` 才是当前占用，`total_token_usage` 是全会话累计、会远超窗口。F 项目源码站在项目A 这边：`tui\src\chatwidget.rs:1187-1188` 与 `tui\src\status\card.rs:342-346` 都用 `info.last_token_usage.percent_of_context_window_remaining(window)`。调研那条建议针对的是成本累计，不是上下文占用。

**真正的偏差在 `last_token_usage` 内部取哪个字段**：
- 项目A 取 `last.get("input_tokens")`（`vitals.py:840`）。
- Codex 官方口径是 `tokens_in_context_window()` → `self.total_tokens`（`protocol\src\protocol.rs:2261-2263`；TUI 侧同义实现 `tui\src\token_usage.rs:39-41`）。

`TokenUsage` 含 `input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens, reasoning_output_tokens, total_tokens`（`protocol.rs:2079-2093`）。只取 `input_tokens` 会漏掉 `output_tokens` 与 `reasoning_output_tokens`，**推理密集的会话被系统性低估**。

另有一处口径差异值得对齐：Codex 的百分比对分子分母同时扣除 `BASELINE_TOKENS = 12000`（系统提示 + 固定工具说明），使首轮之后 UI 显示 100%（`protocol.rs:2241,2275-2286`）。项目A 直接用 `tokens / window`，因此在同一会话上会比 Codex 自己显示的占用率**偏低**几个百分点。

修正：取 `total_tokens`（缺失时回落到 `input_tokens` 并标注降级）；`window` 上引入可选的 baseline 归一化，默认关闭并在 UI 标注口径，避免改变既有判定行为。

### D2 — `.jsonl.zst` 压缩历史整体漏扫（已实测确认，已修）
F 项目硬事实：旧 rollout 在 **7 天后**被 zstd 原地压缩为 `.jsonl.zst`，level 3，用 `rollout-compression.lock` 加锁（`rollout\src\compression.rs:18,254-261`）。常量在源码里写得很直白：`COMPRESSION_LEVEL: i32 = 3`、`MIN_ROLLOUT_AGE = Duration::from_secs(7 * 24 * 60 * 60)`（`:255-256`）。

项目A 的发现逻辑只认 `.jsonl`：`vitals.py:1013` 与 `:1124` 均为 `e.name.endswith(".jsonl")`，全仓无任何 `zst` 字样。**7 天前的 Codex 会话完全不可见**——这是"扫盘"这一核心能力的沉默失效，且随时间推移越来越严重。

修正：发现阶段同时匹配两种扩展名；读取时按扩展名选择解压流。依赖决策见 §7。

**本机现状（诚实记录）**：`codex-cli 0.149.1`，`~/.codex/sessions` 有 252 个 `.jsonl`、`archived_sessions` 有 70 个，最旧的 8.1 天，但 **`.jsonl.zst` 一个都没有**。所以这个修复在本机暂时没有真实样本可验证——压缩可能在这个 CLI 版本里尚未启用，或者是惰性触发（`RUN_MARKER_STALE_AFTER = 6h`，需要 Codex 自己跑起来才会做）。这不改变修复的必要性：源码里阈值是硬编码的 7 天，一旦触发，漏扫就是确定会发生的。测试用注入的假 opener 覆盖，不依赖本机是否装了 zstd。

### D3 — Claude 上下文口径未表态（未修，留待 P2）
现状：项目A 把三项都算进上下文（`input + cache_read + cache_creation`，`vitals.py:818`）。

两个权威口径打架：
- `abtop`（`src/collector/claude.rs` 约 1460 行，附 issue #54）明确**排除** `cache_creation`。
- Anthropic statusline 文档的 `context_window.used_percentage` 是 `input + cache_creation + cache_read`。

项目A 目前与后者一致，但**未在任何界面标注口径**，用户无法判断数字含义。另：`/compact` 刚结束时 `current_usage` 为 null，需显式处理。

修正：保持现有口径（与官方 statusline 一致），但在 UI 与文档标注"口径：input + cache_creation + cache_read（同 Anthropic statusline）"，并提供 `AGENT_HANDOFF_CONTEXT_METRIC=statusline|abtop` 切换。这是加标注而非改行为，不影响既有结果。

### D4 — 点文件前缀剥离：修复了一半（真 bug，已实测确认，已修）
工作树里已存在正确的 `_strip_leading_dotslash`（`gitops.py:301-317`），注释详尽且指出了真实风险：`.env` 变 `env` 会导致 `:(exclude,literal)` 排除落空，**计划文档声明为用户私有的 `.env` 仍会被 `git add -A` 提交进去**。

但这个函数**只在 `gitops.py:329` 被调用一处**。`gitops.py:285` 仍是原始的 `.lstrip("./")`：

```python
rel = (rel_root / fn).as_posix().lstrip("./")
```

后果：`detect_concurrency` 的最近改动扫描里，点文件的相对路径被吃掉一截，永远匹配不上 `tracked_changes`（`:286`），于是 `.env`、`.gitignore`、`.claude/settings.json` 的并发改动**不会进入 advisory 警告**。属于同一 bug 的遗漏点，不是新问题。

修正：`:285` 改用同一个 helper。加针对点文件的回归测试覆盖两处调用点。

### D5 — `handoff.py:539` 非原子写（已修）
`out_path.write_bytes` 在 `_keep_previous` 读/复制之后执行，无 temp+rename。并发读者能看到截断的 handoff 文档。

修正见 §5.1。同一处理也应用到 `plan.update_plan`——计划文档是用户手写的，而且往往一份副本都没有。

### D6 — LRU 无锁并发改写（已修）
`vitals.py:967-990` 的模块级 `OrderedDict` LRU 被 `ThreadPoolExecutor`（`:1053`）并发改写。CPython 的 GIL 使单次 `__setitem__` 不会崩，但 `move_to_end` + 逐出的组合序列非原子，缓存可能超出 `_CACHE_MAX=256` 或丢失条目。

修正：加 `threading.Lock` 包裹读写与逐出。

### D7 — 其他有界缺陷
- `transcript.py:562-575` `joined()` 在丢弃循环内反复重建全文 → 工具块数量的 O(n²)，大 transcript 的主要耗时点。**未修，P3**。
- `transcript.py:258` 把每条保留轮次全量物化成 list + 两个去重集合，无上限 → 70MB transcript 的真实内存风险（`vitals.py` 的流式扫描无此问题）。**未修，P3**。
- `transcript.py:340` assistant 去重键只取前 400 归一化字符 → **长消息前缀相同就被静默丢弃**，这是保真度损失而非性能问题。**未修，P3**。
- `evidence.py:270-288` `_python_scan` whole-file 读入无大小上限。**未修**。
- `gitops.py:280` `Path(root).relative_to(repo)` 在符号链接树上可抛 `ValueError`，未加守卫。**已修**（P1）。
- 静默 swallow：`handoff.py:356`、`gitops.py:262`、`platform.py:197`、`menu.py:68/:252`。**未修，P2**。

---

## 4. 保真度落差：handoff 文档里没有对话

这是与项目定位差距最大的一处。`core/transcript.py` 有完整的对话渲染能力（`read_turns:416`、`render_markdown:490`），但它的输出**只流向导出包 `session.md`（`portable.py:370`）和 GUI 预览（`gui/server.py:464`），从未进入 `build_handoff`（`report.py:458`）**。

### 4.1 当前 handoff 文档实际包含什么

| 内容 | 生产者 | 保真等级 |
| --- | --- | --- |
| git 现场（分支/HEAD/ahead/detached） | `gitops.repo_meta` | 逐字，已脱敏 |
| 提交快照输出 | `gitops.do_commit` | 逐字 |
| 受保护文件 | `plan.parse_plan` | 逐字 |
| 计划任务进度表、文件与符号比率、结论 | `evidence.score_tasks` | **启发式评分** |
| 缺口（缺失文件/符号） | 同上 | 派生列表 |
| 测试命令 + 单行结果 + 失败 ID | `probe.run_tests` | **摘要** |
| 环境陷阱 | `probe.detect_env_pitfalls` | 启发式 |
| 近期提交 | `gitops.recent_commits` | 逐字 |
| 会话体征表 + 最差会话建议 | `vitals.scan_session_vitals` | 评分 |
| 用户提问 | `_DigestCollector.asks` | **逐字，截至 1200 字符** |
| 最后一条提示 | 同上 | 逐字，截至 600 字符 |
| 压缩摘要（全窗口） | `_DigestCollector.digest` | **逐字，不截断** |
| 恢复提示 | `report.build_prompt:287` | 组装，含 resume 命令与深链 |

不存在的类别：**子代理结果、决策记录、待办、风险**。

### 4.2 具体丢失项（逐条有出处）

| 丢失内容 | 位置 | 严重度 |
| --- | --- | --- |
| 工具调用**参数**（只留名字） | `transcript.py:306` | 高 —— 下一会话无法知道上一会话对哪些文件做了什么 |
| 工具结果截至 400 字符 | `TOOL_RESULT_CHARS:42` | 高，且**无降级标签** |
| MCP 工具名丢失，硬编码为 `"output"` | `transcript.py:388` | 中 |
| 错误/堆栈正文：只计数从不引用 | `vitals.py` fatal/errors/aborted | 高 —— 下一会话会重踩同一错误 |
| thinking/reasoning 主动丢弃 | `:299`、`strip_thinking:134` | 中 —— **被否决的方案随之消失，下一会话重新论证死路** |
| 逐轮时间戳从不读取 | 全仓 | 中 —— 只有文件 mtime |
| 文件 diff、图片、hook 输出、slash 命令上下文 | `_BOILERPLATE_TAGS:72` 过滤 | 中 |
| Codex `developer` 角色消息跳过 | `:372` | 低 |
| 连续工具轮次被合并 → 顺序失真 | `merge_tool_runs:432` | 中 |
| assistant 去重键只取前 400 归一化字符 | `:340` | **高 —— 长消息前缀相同即被静默丢弃** |
| Claude 无压缩摘要正文提取（只有次数） | `vitals.py` | 高 —— Claude 会话整体丢失摘要 |

### 4.3 对照项目的保真取舍

- **D（vscode）保住了工具调用身份**：`IChatToolInvocationSerialized`（`chatService.ts:1097-1114`）保留 `toolCallId`、`toolId`、`invocationMessage`/`pastTenseMessage`、`resultDetails`、`isComplete`、`isConfirmed`，只丢 observable 与 `otherClientToolCall`，并注明"仅活动期元数据在序列化时省略"。项目A 恰恰丢了参数，这是对比下最刺眼的差距。
- **D 显式降级并打标签**：`repoData` 有降级枚举 `tooManyChanges`（>100 文件）、`tooLarge`（>900KB）、`trimmedForStorage`（`chatModel.ts:1895-1985`）；序列化有 `PERSIST_ENTRY_MAX_STRING_CHARS = 1MiB` / `PERSIST_ENTRY_MAX_TOTAL_CHARS = 100MiB`，遇 V8 `RangeError` 用截断 replacer 重试（`objectMutationLog.ts:226-261`）。**项目A 的 400 字符截断是静默的**。
- **F 的压缩即为 handoff 规范**：`prompts\templates\compact\prompt.md` 原文自称"为另一个将接手任务的 LLM 准备的 handoff 摘要"，四个板块——进展与决策 / 约束与偏好 / 剩余步骤 / 关键数据与引用。这套骨架可直接作为项目A 文档结构的对齐基准。
- **F 的保真缺口正是项目A 的价值所在**：压缩只逐字保留最近的**用户**消息，上限 `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000`（`core\src\compact.rs:57,639-700`），其余全部替换为一条 `SUMMARY_PREFIX + 最后一条 assistant 消息`；工具调用、推理、输出从模型上下文中丢弃，**但仍留在 rollout 文件里**。项目A 要补的就是这段差值。
- **F 的 rollout 跨压缩仍然无损**：`compacted` 记录内嵌完整 `replacement_history` 加 `window_number`/`window_id`/`previous_window_id`/`first_window_id`（`compact.rs:360-384`），因此从单个文件即可重建压缩前后两个状态。
- **B 是反面参照**：只留 role + 纯文本 + 时间戳，工具调用、输出、推理、usage、model、cwd、git branch、`session_meta`、`turn_context` 全丢，渲染用 `textContent` 无 markdown。定位是"聊天大意"。**不要学它的 `return null` 兜底——应对未知记录类型显式记账而非静默丢弃。**

### 4.4 F 项目补充的 Codex 事实（修正既有认知）

- 记录信封：`RolloutLine { timestamp, ordinal?, #[serde(flatten)] item }`（`history\src\lib.rs:200-207`），`ordinal` 仅在 `Paginated` 历史模式出现。
- 九种 `type`：`session_meta`、`response_item`、`inter_agent_communication`、`inter_agent_communication_metadata`、`compacted`、`turn_context`、`world_state`、`security_risk_score`、`event_msg`（`history\src\rollout_payload.rs:20-52`）。
- **model / approval_policy / sandbox_policy / cwd 是逐轮的（`turn_context`），不在 `session_meta` 里**（`protocol.rs:3037-3084`）。handoff 文档必须报告**最新一条 `turn_context`**，而不是只看 meta。这一条项目A 目前没有实现。
- `session_meta` 的 `git` 只有 `commit_hash`、`branch`、`repository_url`，**无 dirty 标志**（`protocol.rs:3163-3173`）——会话开始时的工作树状态不可恢复，项目A 应自己抓取（这正好是 §5.4 的内容寻址快照）。
- 分叉可发现：`forked_from_id`、`parent_thread_id`，以及文件名形式 `…-<thread_id>_<rollout_id>.jsonl`（`rollout_file_name.rs:47,66-73`）。**血缘关系免费获得**，项目A 目前未利用。
- resume 不重放的记录：`event_msg`、`turn_context`、`world_state`、`security_risk_score`（`rollout_reconstruction.rs:372-376`）——它们是 UI/溯源数据，不是模型历史。
- 自动压缩阈值：`min(config_limit, context_window * 9 / 10)` = **窗口的 90%**（`openai_models.rs:486-497`）。项目A 的 90% 阈值与之吻合，可标注为有据。
- 每行写入即 `write_all` + `flush`，无批处理（`recorder.rs:1967-1973`），非原子重命名。**推断**：崩溃最多丢一行残行，解析器应容忍截断的末行。
- `~/.codex/history.jsonl` 是 `{session_id, ts, text}`、权限强制 `0o600`、超限按 `HISTORY_SOFT_CAP_RATIO = 0.8` 裁剪最旧行（`message-history\src\lib.rs:52,62-66,149,195-273`）。另有 `~/.codex/session_index.jsonl` 映射线程名/ID。
- 路径优先级（比 B/C 更全）：`$CODEX_SESSIONS_ROOT` → `$CODEX_HOME/sessions` → `~/.codex/sessions`。
- `codex resume [SESSION_ID] [--last] [--all]`、`codex fork [...]`、`codex exec resume [...]`；**不存在 `--continue`**（`cli\src\main.rs:344-361`）。SESSION_ID 可以是 UUID 或会话名。

---

## 5. 可靠性原语（从 C、D 移植）

### 5.1 原子写 + fsync
来源：C 的 `atomic_write_with_unix_mode`（`config.rs:336`）——同目录唯一临时名 `{file}.tmp.{pid}.{ns}.{ctr}`、`create_new(true)`、16 次冲突重试、失败路径必删临时文件。Windows 用 `ReplaceFileW`，仅在 `ERROR_NOT_SUPPORTED`（WSL UNC 路径）时退回 `rename`。

**C 自己漏了 fsync**，我们要补：写完 `flush` + `os.fsync(fd)`，重命名后再 `fsync` 父目录（POSIX；Windows 上目录 fsync 不适用，跳过）。

落点：`handoff.py:539`、`plan.update_plan`、以及所有写入用户可见产物的位置。

### 5.2 追加式日志 + 两阶段提交（可选增强）
来源：D 的 `objectMutationLog.ts:21-33`（追加式 JSONL 变更日志，超 512/1024 条重写 `Initial` 自压缩）与 `:400-410`（`write()` 返回待定状态，仅 `confirmWrite()` 提交）。

对项目A 的价值：handoff 文档是一次性产物，追加日志收益有限；但**持久化会话索引**（§6.2）正适合这个结构——末尾截断仍可回放到最后完好状态。

### 5.3 读前哈希、写前复验
来源：C 的 `openclaw_config.rs:324-337`。transcript 是 append-only，**扫描过程中必然在变**。当前项目A 的缓存键含 `(size, mtime)`，能检出变化但不报告。

改进：扫描开始记录 `(size, mtime)`，生成文档时复验；不一致则在文档中标注"扫描期间会话仍在活动，数据截至 <时间>"。这是加事实标注，不改判定。

### 5.4 内容寻址工作树快照（独占创新点）
来源：D 的 `chatEditingSessionStorage.ts:60-92,153-155`——`state.json` + `contents/<sha7>` blob，条目带 `originalHash`/`currentHash`。

GitHub 调研明确：**无人做"工作树 diff 快照与 transcript 偏移绑定"**。F 的 `session_meta.git` 无 dirty 标志（`protocol.rs:3163-3173`），会话开始时的工作树状态本身不可恢复。项目A 自己抓取即可独占这一能力。

设计：`--snapshot` 时把 `git diff` 的统一 diff 存为内容寻址 blob，与会话 ID + 最后 rollout ordinal 绑定；沿用 D 的降级枚举（`tooManyChanges` >100 文件、`tooLarge` >900KB、`trimmedForStorage`）并**显式标注降级原因**。

### 5.5 版本化 + normalize 漏斗
来源：D 的 `chatModel.ts:1993,2213`。每种格式一个结构，`normalize()` 统一出口：无 `version` 键 ⇒ 判 V1 ⇒ 盖当前版本；缺字段则回填（缺 `sessionId` 补 uuid、缺 `creationDate` 填一年前）。

对项目A：handoff 文档与导出包目前无版本号。加 `handoff-format: 1` 前置元数据 + `normalize()` 读取漏斗，旧文档将来不会咬人。

### 5.6 Windows 上不读 `$HOME`
来源：C 的 `config.rs:14-16` 注释——Git/Cygwin/MSYS 会注入假 `$HOME`。项目A 已用 `Path.home()`，此项**已合规**，仅需补一条回归测试防将来退化，并加 `AGENT_HANDOFF_TEST_HOME` 便于测试。

### 5.7 路径穿越守卫
来源：C 的 `session_manager/mod.rs:196-225`（canonicalize + 断言在提供者根内）。项目A 的 `/api/session-md` 已有等价守卫（`server.py:432-466`），**已合规**。CLI 侧 `--sessions`/`--find` 建议补同样断言。

---

## 6. 可观测性与首次上手

### 6.1 评分可解释（最高价值的可观测性补强）
现状：`band_for` 的抬档地板（压缩 ≥2 → critical、fatal+aborted ≥3 → high）在 UI 中完全不可见，API 不返回"为何是这一档"。用户只看到结论，无法判断是否可信。

方案：`SessionRow` 增 `band_reasons: list[str]`（i18n 键 + 参数），API 透出，会话卡片以可折叠列表呈现。每条形如"压缩 3 次 ≥ 2 → 抬至 critical"。

配套：**每个数字标注来源**（借 `Claude-Code-Usage-Monitor` 的 provenance 标签思路）——"来自 usage 字段" / "来自 token_count 事件" / "按体积估算（误差大）"。

### 6.2 持久化会话索引
来源：B 的 sidecar `history.jsonl` 差分追加（`historyManager.ts:313,341`）+ D 的小索引与正文分离（`chatSessionStore.ts:39,906-920`）。

现状：项目A 的 LRU 是纯内存，进程退出即失效（`vitals.py:967`）。首次扫描 463 个转录约 14 毫秒仅是文件名遍历，正文解析才是成本。

方案：`~/.config/agent-handoff/index.jsonl`（Windows 走 `%LOCALAPPDATA%`），按 `(path, size, mtime)` 差分，只解析新增/变化的转录。用 §5.2 的追加式结构，超阈值自压缩。**必须是可选加速层，索引损坏时静默回落全量扫描**。

### 6.3 doctor 自检命令
现状：无向导、无 doctor、无示例数据；`gitAvailable` 后端已广播但前端从不读（`server.py:279-281`），git 缺失只在运行中途暴露。

方案：`agent-handoff doctor` 输出检查清单——git 是否可用及版本、各 agent 数据根是否存在及会话计数、`CLAUDE_CONFIG_DIR`/`CODEX_HOME`/`CODEX_SESSIONS_ROOT` 是否生效、zstd 解压能力是否可用、是否在 git 仓库内、写入权限、UTF-8 stdio 状态。GUI 同步给一个"环境"面板。

来源参照：C 的恢复界面（`DatabaseUpgrade.tsx` 五相位）与 `EnvWarningBanner.tsx`（列出会覆盖配置的环境变量）。

### 6.4 首次运行探测而非询问
来源：C 的 `app_config.rs:719-729`（首启自动探测导入）+ 后端判定标志、UI 只做 null 检查（`FirstRunNoticeDialog.tsx:22-24`）。

项目A 的 GUI 已在加载时自动扫描（`app.js:816`），方向正确。补：空状态从"一行居中文字"升级为**可操作空状态**——列出探测到的数据根、各自会话数、以及"未发现会话"时的具体排查步骤（借 C 的 `ProviderEmptyState.tsx:11-20` 双 CTA 模式）。

---

## 7. 前端与画布交互

### 7.1 设计令牌补全
现状短板：字号是 14.5/13.5/12.5/11.5/9.5px 的随手小数，无命名字阶；间距全是字面 px，无 `--space-*` 令牌（`style.css`）。

方案：引入 `--fs-*`（5 级）与 `--space-*`（6 级，4px 基准）令牌，**逐个映射到现有实测值**，不改变任何渲染结果。这是纯重命名，风险最低。参照 C 的 `index.css:6-45` HSL 令牌分层。

### 7.2 可访问性修复（全部是明确缺陷）
- `index.html:2` `lang="zh-Hans"` 硬编码 → 切语言时写 `documentElement.lang`。当前英文/繁体用户的屏幕阅读器被告知错误语言。
- `role="tablist"` 半成品 → 补 `role="tabpanel"`、`aria-controls`、`aria-selected`、方向键切换。
- 仅一个 aria-live 区域（`#will`）→ 扫描/handoff 进度加 `aria-live="polite"` 播报。
- `title=` 当唯一 tooltip 通道 → 改为可聚焦的 `<button aria-describedby>` + 可见气泡，触屏与键盘用户可达。
- `-webkit-line-clamp` 补标准 `line-clamp`。
- 参照 D：折叠部件 chevron 设 `aria-hidden`、可访问名挂在 label 上（`chatCollapsibleContentPart.ts:66,109`）。

### 7.3 时间线画布（本次最大的新增交互）
现状：**无画布、无图表、无图谱、无时间线、无拖拽**。

设计（纯 SVG + CSS，零依赖，与项目A 的无构建约束一致）：
- **横轴为轮次序号**（不是墙钟时间——Codex 有 `timestamp`，Claude 逐轮时间戳项目A 目前不读；先用序号，读到时间戳后再叠加真实时距）。
- **压缩边界画为竖线**，标注 `window_number`；借 `abtop` 的 `200k C2` 徽章形式（`src/ui/context.rs::draw_context_bars`）。
- **上下文占用折线**叠加，配合 §6.1 的来源标注。
- **按记录类型分色的轨道**：用户 / assistant / 工具 / 错误 / 压缩。借 `claude-code-log` 的类型过滤器与 `claude-devtools` 的分段上下文条带。
- 交互：滚轮缩放、拖拽平移、点击某轮跳到详情、类型过滤复选框、键盘方向键移动游标。
- 渐进渲染借 D：折叠内容**首次展开才渲染**（`chatCollapsibleContentPart.ts:139`）；长列表用 `RangeMap` 式高度前缀和虚拟化（`listView.ts:289,307-308`）。
- `prefers-reduced-motion` 下禁用平滑过渡（项目A 已有该块，扩展覆盖画布）。

### 7.4 保真分层展示
借 `claude-code-log --depth full|high|low|minimal|user-only`。项目A 增 `--fidelity` 同义分层，UI 给分段控件。T0（逐字）**永不经过任何摘要处理**，使"无损"可审计。

---

## 8. 分阶段实施计划

每阶段独立可验证、可单独回滚。所有阶段共同约束：不删既有函数/变量/注释，不改既有默认行为，新增能力一律默认关闭或纯增量。

| 阶段 | 内容 | 风险 | 验证方式 |
| --- | --- | --- | --- |
| **P1（已完成）** | D1 Codex `total_tokens`、D2 `.jsonl.zst` 发现与解压、D4 点文件前缀、D6 LRU 加锁、D5 原子写、符号链接守卫、`CODEX_SESSIONS_ROOT`、`unknown` 区间 | 低 | 已完成，见下 |
| P2 | §6.1 评分可解释 + 数字来源标注；§6.3 doctor 命令 | 低 | 新增测试 + i18n 三语补齐 |
| P3 | §4 保真度：工具调用参数、错误正文、MCP 工具名、逐轮时间戳、显式降级标签、`turn_context` 最新值 | **中** | 保真度专项测试 + 人工核对样本 |
| P4 | §7.1 设计令牌、§7.2 可访问性 | 低 | `test_i18n.py` 既有门禁 + 新增 a11y 断言 |
| P5 | §7.3 时间线画布、§7.4 保真分层 | 中 | 新增前端测试（当前完全缺失） |
| P6 | §6.2 持久化索引、§5.4 内容寻址快照、§5.5 格式版本化 | 中 | 索引损坏/降级路径测试 |

### P1 实施记录（2026-08-25，已发布为 2.4.0）

改动的源文件与内容：

| 文件 | 改动 |
| --- | --- |
| `platform.py` | 新增 `atomic_write_bytes`、`_fsync_dir`、`TRANSCRIPT_SUFFIXES`、`is_transcript_name`、`is_compressed_transcript`、`zstd_opener`、`open_transcript`、`TranscriptCompressedError`；`agent_session_roots` 增读 `CODEX_SESSIONS_ROOT` |
| `core/vitals.py` | `last_token_usage` 改取 `total_tokens`（缺失回落 `input_tokens`）；`scan_one` 走 `open_transcript` 并处理压缩失败；新增 `_session_id_from_name`、`SessionRow.compressed_unreadable`；`band_for` 增 `unknown`；`BAND_ORDER` 增 `unknown`；`_cached_scan`/`clear_cache` 加 `threading.Lock`；两处目录遍历改用 `is_transcript_name`；`_Extractor.finish` 的 `fp.stem` 改用 `_session_id_from_name` |
| `core/transcript.py` | `_lines` 走 `open_transcript`，容忍 `TranscriptCompressedError` 与损坏的解压流 |
| `core/gitops.py` | `detect_concurrency` 的 `.lstrip("./")` 改用已有的 `_strip_leading_dotslash`；`relative_to` 加 `ValueError` 守卫 |
| `core/handoff.py`、`core/plan.py` | 输出改用 `atomic_write_bytes` |
| `core/report.py`、`cli.py`、`gui/static/app.js` | 「读不到正文」在文档、CLI 卡片、网页卡片三处显式呈现 |
| `gui/static/style.css` | 新增 `--unk`/`--unk-bg` 中性色（浅色与两种深色声明处各一份）、`unknown` 色带与徽章、`.badge-unknown`、`.metrics .dim` |
| `i18n/*.json` | 三语各新增 7 键：`band.unknown`、`band.advice.unknown`、`doc.vitals.cell.unreadable`、`cli.card.unreadable`、`gui.label.archived`、`gui.label.unreadable`、`gui.tip.unreadable` |
| `tests/test_platform.py` | 原子写 5 例（含写失败保原文、无残留临时文件）、压缩转录 6 例 |
| `tests/test_vitals.py` | Codex token 字段 2 例、`unknown` 区间 4 例、压缩会话发现/读取/降级/ID 剥离/坏流/`locate_by_id` 6 例、缓存线程安全 1 例 |
| `tests/test_gitops.py` | 根目录点文件进提示 1 例、符号链接仓库不中断 1 例 |
| `CHANGELOG.md` | `[未发布]` 改为 `[2.4.0] - 2026-08-25`，新增 9 条修复记录 |
| `pyproject.toml`、`__init__.py`、三份 README 徽章 | 2.3.0 → 2.4.0；测试徽章 660 → 681 |
| 三份 README | 转录存放表增 `.jsonl.zst` 行与 `CODEX_SESSIONS_ROOT`；新增压缩归档说明与可选依赖安装指引；体检判据表更正 Codex 字段并补 `unknown` 说明 |
| `docs/guide.html` + 打包副本 | `build_guide.py` 重新生成，版本随之更新（两份哈希一致） |

验证结果：

- `python -m pytest`：**681 passed, 3 skipped**（改动前 660；新增 22 例，其中 1 例是把原有 Codex token 测试拆成两例）
- `python scripts/check_i18n.py`：3 languages, 481 keys each — all aligned；无 CJK 字面量绕过文案表
- `python scripts/build_guide.py`：156 keys × 3 languages，`docs/guide.html` 与 `gui/static/guide.html` 哈希一致
- 版本/徽章漂移门禁（本地复现 CI 逻辑）：version 2.4.0 一致；徽章 681 ≥ def 计数 482
- `python -m compileall src scripts`：exit 0
- `agent-handoff --version` → `agent-handoff 2.4.0`；`--lang en --help` 正常本地化
- `--vitals --limit 4 --lang en` 对**本机真实转录**跑通：12 份扫描、8 份需处理，会话 ID / 仓库 / resume 命令均正确
- 端到端合成校验（压缩转录的四种行为）：

  | 场景 | 结果 |
  | --- | --- |
  | `.jsonl.zst` 被目录遍历发现 | 是 |
  | 无 zstd 实现 | `band=unknown`、`unreadable=True`、**会话仍列出** |
  | 会话 ID 两层扩展名都剥掉 | `resume` 命令为 `codex resume 01a00c0a-…`（无 `.jsonl` 残留） |
  | 有 zstd 实现、含 output+reasoning 的 usage | `total_tokens=120000` → 99% → `critical` |

  同一份数据用旧字段 `input_tokens=80000` 只有 66%——会判成 `high`。**实测差两个区间**，这就是 D1 的实际影响。
- `ruff`：**本机未安装，未运行**（未擅自安装）。行长与导入顺序按 `line-length = 110` 与既有分组人工核对；CI 的 ruff 作业仍是权威。
- 未验证项：wheel 构建与"装进干净 venv 冒烟"这一 CI 门禁在本机跑不了（该 venv 无 pip）。CI 上会跑。

未改动的既有行为：函数与变量名一个都没改，注释一条都没删。所有新增能力要么是纯增量（新常量、新字段、新键），要么是把静默失败换成显式标注。

### P1 之后剩余的已知缺陷（未修，留待 P3/P6）

- `transcript.py:562-575` `joined()` 的 O(n²)：需要重构 `render_markdown` 的丢弃循环，属 P3 范围。
- `transcript.py:258` 轮次全量物化无上限：同上。
- `transcript.py:340` 400 字符去重键静默丢弃长消息：属保真度问题，P3 修。
- `evidence.py:270-288` `_python_scan` whole-file 无大小上限。
- 阈值仍硬编码（只有分母可配）：P2 连同评分可解释一起做。
- 静默 swallow 五处：逐个需要判断该不该出声，P2 随可观测性一起处理。

## 9. 依赖决策

`.jsonl.zst` 需要 zstd 解压，而项目A 的**零运行时依赖**是经过论证的取舍（`pyproject.toml:27-29`：工具运行在已损坏的环境里，任何需要 pip 安装的东西都是失败途径）。

不引入强制依赖。按可信度顺序尝试：
1. `compression.zstd`（Python 3.14+ 标准库）
2. `zstandard`（若用户环境已有）
3. `pyzstd`（若用户环境已有）
4. 全部不可用 → **会话仍然出现在列表中**，标注"已压缩归档，需 zstd 支持才能读取正文"，并在 doctor 中给出安装建议。

关键点：现状是这些会话**完全不可见**（沉默失效）；改动后最差情况是可见且带明确原因。这是严格的正向调整。

## 10. 不做的事

- 不引入前端框架、构建步骤或 CDN 资源——违背 §2.7 的零构建取舍。
- 不引入 LLM 调用做摘要——项目定位是无损，`claude-mem` 的有损压缩正是要避免的。
- 不抄 `winfunc/opcode`（AGPL-3.0，copyleft 传染）。
- 不抄任何无许可证仓库的代码：`claude-memory-compiler`、`codex-chat-history`、`claude_code_session_viewer`、**`anthropics/claude-code` 本身**（其 CHANGELOG 可作情报，代码不可搬）。
- 不抄 B 的 `return null` 静默丢弃、sentinel 字符串当状态、关键词黑名单猜系统提示。
- 不删除既有注释——那是本代码库最独特的资产。
