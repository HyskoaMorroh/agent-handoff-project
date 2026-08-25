# 会话归属与最后活动的取证式重建：方案书

- 文档日期：2026-08-25
- 状态：**方案待确认，尚未改动任何源码**
- 版本基线：agent-handoff 2.5.0（工作树另有 P2 未提交改动 1055 行，见 §0.3）
- 前置文档：`docs/superpowers/plans/ccs-ui-notes.md`（截图勘查 + 10 项磁盘取证）、`docs/2026-08-25-cross-project-uplift-plan.md`（六项目对比，P1/P3 已实施）

---

## 0. 这次要解决什么

### 0.1 用户的指控与核实结果

用户看到界面把一个明明在 `C:\Users\devin\agent-handoff-project` 里干活的会话，标成了 `e:\output\kirara-ai\kirara-ai3.3.0b8`，并据此判断"数据混乱、颠倒黑白、过去会话文件都存到了后者"。

逐条核实（全部实测，方法与数据见前置文档）：

| 指控 | 核实结果 |
| --- | --- |
| slug 与会话错配 | **不成立**。9 个 slug 全部自洽；项目A 的 14 个会话 `cwd` 全部正确指向 `C:\Users\devin\agent-handoff-project` |
| 交接产物存到了 kirara 项目 | **不成立**。kirara 目录及其 `docs\` 下无任何 handoff 文件 |
| 界面把工作目录显示错了 | **半成立**。显示的 `cwd` 数值正确，但**这个口径回答不了用户的问题** |
| 复制出来的东西会让新会话改错代码 | **成立**，且比用户以为的更严重 |

关键一步取证：那个会话共 614 次工具调用，其中 **258 次文件写入全部落在 `agent-handoff-project`，0 次落在 kirara**。卡片上"用这个仓库交接"指向的仓库，这个会话从头到尾没改过一个字节。

所以真相是：**工具没有算错 `cwd`，但它把"在哪启动"当成了"在改哪个仓库"。** 用户的直觉完全正确，只是错因不是数据混乱，而是口径选错 + 强证据从未采集。

### 0.2 两个独立的核心缺陷

**缺陷 A —— 归属口径**：`SessionRow.repo`（`core/vitals.py:522-544`）只有两级依据：`cwd` → `nearest_repo`，失败则退到 `repos[0]`（正文提到过的第一个路径）。转录里存在**远比这两者可靠的证据**，一次都没被读取。

**缺陷 B —— 最后活动时间**：`core/vitals.py:1452` 用 `datetime.fromtimestamp(st.st_mtime)`，即文件 mtime。mtime 与真实最后活动大面积脱钩，且污染全部排序与"最差会话"推荐。

两者都不是显示层问题，都在数据层，且都有干净的修法。

### 0.3 工作树现状（动手前必须知道）

`git status` 显示 12 个文件有未提交改动，共 **+1055 / −13** 行，内容是计划书里的 P2（评分可解释 + doctor 自检 + 时间线 sparkline）：

- 新增 `core/doctor.py`（256 行，8 个检查函数）与 `cmd_doctor`
- 新增 `band_reason()`（`vitals.py:374-456`）—— 返回结构化判定依据而非句子
- 新增 `_TimelineCollector`（`vitals.py:960-1039`）—— 按轮次采样占用走势，等距抽稀保留首尾与压缩点
- 新增 `--fs-*` / `--sp-*` 设计令牌、`role="tabpanel"` 等 a11y 补全
- i18n 三语各 +43 键（现 526 键，三语对齐）

实测基线：`python -m pytest` **748 项收集，全部通过，3 skipped，退出码 0**。

**本方案的所有改动都建立在这份工作树之上，不回滚任何既有改动。** P2 的 `band_reason` 结构化证据模式正是本方案要复用的模板。

---

## 1. 缺陷 A：归属口径的取证式重建

### 1.1 证据分层（核心设计）

转录里能回答"这个会话在哪个仓库工作"的信号，按可靠性排序：

**Claude Code 侧**

| 级别 | 证据 | 语义 | 实测可得性 |
| --- | --- | --- | --- |
| E1 | `Edit`/`Write`/`MultiEdit`/`NotebookEdit` 的 `input.file_path` | **改了这个文件** | 12/63 会话有；有则极强 |
| E2 | `Read`/`Grep`/`Glob` 的 `file_path`/`path` | 看了这个文件 | 与 E1 高度共现 |
| E3 | `cwd` + `nearest_repo` | 在哪启动 | 55/63 会话有（8 份在 400 行预算内读不到）|
| E4 | 正文提到的路径（现状 `repos[]`） | 提过这个路径 | 15 个会话有 ≥2 个候选 |

**Codex 侧**（语义与 Claude 完全不同，必须分开处理）

| 级别 | 证据 | 语义 | 实测可得性 |
| --- | --- | --- | --- |
| E1 | `turn_context.workspace_roots` | **harness 声明的工作区根** | 逐轮记录，样本含 1–3 项 |
| E2 | `exec_command` 参数里的 `workdir` | 命令实际在哪跑 | 151/316 rollout 有 |
| E3 | `apply_patch` 的目标路径 | 改了什么 | 稀疏（样本仅 2 次）|
| E4 | 最新一条 `turn_context.cwd` | 最后的当前目录 | 逐轮记录 |
| E5 | `session_meta.cwd`（现状唯一依据） | 会话沙箱目录 | 常**不是**仓库 |

Codex 的 E5 之所以排最后：实测 316 份 rollout 里 `cwd` 形如 `C:\Users\devin\Documents\Codex\2026-08-17\1-e-output-kirara-…`，那是 Codex 自己的会话沙箱目录，`nearest_repo` 直接返回空。cwd 推出的仓库与"命令跑得最多的仓库"一致的只有 **16/151**。

### 1.2 判定算法

新增 `core/attribution.py`（新文件，不动既有模块的职责边界）：

```
RepoEvidence:
    repo: str            # norm_path 规范化后的仓库根
    display: str         # 原始大小写，给界面显示
    level: str           # "edit" | "read" | "cwd" | "workspace" | "exec" | "mention"
    hits: int            # 命中次数
    samples: list[str]   # 最多 3 个代表文件，已脱敏

RepoVerdict:
    primary: str                    # 结论
    confidence: str                 # "certain" | "likely" | "weak" | "none"
    basis: str                      # 结论来自哪一级
    evidence: list[RepoEvidence]    # 全部候选，按级别再按命中数排序
    conflict: bool                  # cwd 与 primary 是否不一致
```

判定规则（按序短路）：

1. 有 E1（写/patch/workspace_roots）且**唯一** → `certain`
2. 有 E1 但多个 → 取命中数最高者；若第一名命中数 ≥ 第二名 3 倍 → `likely`，否则 `weak` 且要求界面强制展开全部候选
3. 无 E1、有 E2 → 同上规则，上限 `likely`
4. 无 E1/E2、有 E3（cwd）→ `weak`，`basis="cwd"`
5. 只有 E4 → `weak`，`basis="mention"`
6. 全无 → `none`，`primary=""`

`conflict=True` 时（cwd 指向 X 而证据指向 Y），界面与文档必须**同时**显示两者并说明差异，绝不静默替换。这一条是防止"修好一个方向的错，制造另一个方向的错"。

### 1.3 为什么不直接把 `SessionRow.repo` 改掉

`SessionRow.repo` 的 docstring（`vitals.py:524-539`）记录了一次真实的判定失误与修正过程，是本代码库最有价值的资产之一。而且它的语义"cwd 优先"在**没有强证据时依然正确**。

因此：

- **`SessionRow.repo` 保持现状，一行不改**，语义仍是"启动目录所在仓库"。
- 新增 `SessionRow.verdict: RepoVerdict` 字段（`field(default_factory=...)`，向后兼容）。
- 新增 `SessionRow.work_repo` property：`verdict.primary or self.repo`。有强证据用强证据，没有就退回原逻辑 —— 纯增量，旧行为在无证据时逐字保留。
- 界面与下游动作改用 `work_repo`，但**同时显示 `repo`**，二者不同时给出差异说明。

### 1.4 `repos[]` 的两个小修

1. **去重按 `norm_path`**：`platform.py:222-224` 的 `norm_path` 已经会小写化与统一分隔符，但 `vitals.py:840` 的 `repos.append` 没用它。实测 `e:` 与 `E:` 同一目录占两槽，挤掉真实候选。修法：入列前 `norm_path` 去重，展示时保留首次见到的原始大小写。
2. **排序改为按证据级别**：现状按"正文里首次出现位置"排，而 `cwd` 天然出现在第 3 行，永远第一。改为按 §1.1 的级别排序，`cwd` 那一项标注 `level="cwd"`。

这两条使"（另有 N 个）"里的真答案浮到第一位。

### 1.5 成本控制

E1/E2 需要解析 `tool_use` 块，而 `_Extractor` 现状在 `done` 后就丢弃（`vitals.py:1414-1418`），走廉价分支。不能为了收集证据把整份 70MB 转录全解析。

设计：

- 新增 `_AttributionCollector`，只在 `deep=True` 时启用（与 `err_texts` 相同的开关，`vitals.py:1401`）。
- 预筛用**子串查找**而非 `json.loads`：Claude 侧命中 `"tool_use"` 且含 `"file_path"`；Codex 侧命中 `"workspace_roots"` 或 `"workdir"`。不命中的行零成本跳过。
- 独立行数预算 `ATTRIBUTION_LINE_BUDGET`（建议 1200，比 `PATH_LINE_BUDGET=260` 大 —— 写操作往往发生在会话中后段），超预算后停止收集并置 `truncated=True`，界面标注"证据采样截至前 N 行"。
- 复用 `_FullnessCollector` 已 `json.loads` 出的对象的模式：由已解析处调过来，不重复解析。

实测参考：本机最大转录 79MB / 单行最大 3.4MB，预算机制是必需的。

---

## 2. 缺陷 B：最后活动时间

### 2.1 现状与实测损害

`vitals.py:1452` 用文件 mtime。实测：

**Claude 侧**（63 份）：22 份偏差 >60 秒，10 份 >1 小时，3 份 >24 小时，最大 **241243 秒 ≈ 2.8 天**。

**Codex 侧**（324 份）：287 份 mtime **早于**最后记录（中位 −222 秒，p10 −1815 秒，最差 −15228 秒）；**268/324 份的 mtime 与文件名里的会话开始时间相差不到 2 秒** —— Codex 的 mtime 实质是**创建时刻**，与最后活动无关。

**排序损害**（492 份合计）：按 mtime 排 vs 按真实时间排，位次相同的只有 **122/492**，最大偏移 **93 位**。

受影响的下游：`vitals.py:1751`、`1755`（分组与组间排序）、`cli.py:569`（相关性排序）、`cli.py:502`（分组代表值）、`core/handoff.py:444`（"最差会话"推荐的输入）。一个 2.8 天的偏差足以把真正该交接的会话排到看不见的地方。

### 2.2 修法：尾读取真实最后活动

新增 `platform.py::last_record_time(fp, opener)`：

- 从文件末尾读 `TAIL_PROBE_BYTES`（**32768**，实测 63/63 命中；8KB 只有 57/63）。
- 丢弃第一个不完整行（尾读窗口几乎总从行中间开始）。
- 逆序扫描，取第一个信封级 `timestamp`。Claude 是顶层 `timestamp`，Codex 是 `RolloutLine.timestamp`，两者都在顶层，同一套取法。
- 找不到 → 加倍窗口再试一次（上限 512KB）→ 仍找不到则回落 mtime，并置 `time_source="mtime"`。

压缩转录（`.jsonl.zst`）不能尾读（seek 不到），必须整流解压。因此：`is_compressed_transcript` 为真时**直接回落 mtime** 并标注，不为了一个时间戳把整个压缩流解一遍。本机实测 `.zst` 数量为 0，这条路径优先保证不劣化。

### 2.3 字段与兼容

- **`SessionRow.mtime` 保持现状**（仍是文件 mtime），字段名、类型、语义一行不改。所有既有测试与 `disk.py` 的用法不受影响。
- 新增 `SessionRow.last_active: datetime`，以及 `time_source: str`（`"record"` | `"mtime"` | `"mtime-compressed"`）。
- 新增 `active_at` property：`last_active or mtime`。
- 排序键与界面显示改用 `active_at`；`time_source != "record"` 时界面在时间旁标注来源（借用 P2 已建立的"每个数字标注来源"模式）。
- 文档里 `doc.sessions.mtime` 的值改用 `active_at`，i18n 键名不变（键名不变是硬约束，改键名会让三语文案表错位）。

### 2.4 顺带修正一个认知

截图里两张卡片显示同一秒（`2026-08-25 18:41:26`）**不是共享状态 bug**。`app.js:394` 的 `ago(r.mtime)` 与 `title: r.mtime_text` 各取各自行对象，代码无缺陷。根因就是 mtime 本身不稳定：卡2 的文件在截图之后又被写入，把 mtime 推到了 19:28。修掉 §2.2 之后这个现象自动消失。

---

## 3. 缺陷 C：输出目录漂移

### 3.1 复现

`core/handoff.py:295`：`out_dir = plan_path.parent if plan_path else (repo / "docs")`。

`core/plan.py:116-172` 的 `find_plan` 递归全仓（深度 5），筛选条件是：`.md`、体积 ≥80 字节、含 `- [`、匹配 `TASK_HEAD`、复选框 ≥3 个，然后**按 mtime 取最新**。

在项目A 上重放该算法，候选 3 个，**全部是 README**：

```
2026-08-25T02:24:31  cb=4  README.zh-Hant.md   ← 胜出，out_dir = 仓库根
2026-08-25T02:21:38  cb=4  README.en.md
2026-08-25T02:18:05  cb=4  README.md
```

三份 README 都含 `### Task 1: 建立数据层` 这样的**示例片段**（README 在演示计划文档格式），各带 4 个 `- [ ] **Step N**` 复选框，因此全部通过判定。

与磁盘吻合：仓库根有 `2026-08-24-handoff.md` 与 `.prev.md`，而 `docs/` 下是更早的 8-22 / 8-23 两份。**输出目录在 8-24 那次运行时从 `docs/` 漂到了仓库根**，因为那天动过 README。

### 3.2 修法（三层，全部是收紧判定而非改变语义）

1. **文件名负面清单**：`README*`、`CHANGELOG*`、`CONTRIBUTING*`、`LICENSE*`、`CODE_OF_CONDUCT*` 不参与候选。这些文件的性质是"说明"，不是"计划"，即使含示例复选框也不该被当作计划文档。
2. **示例片段识别**：候选文件里若 `TASK_HEAD` 匹配位置**同时**处在围栏代码块（``` 或 ~~~）内，该匹配不计数。README 的示例几乎总在围栏里。
3. **计划信号加权**：要求候选同时含 `INTENT_RX`（`plan.py:73-77` 已有的目标段落正则）匹配。真计划文档有目标段，README 的示例片段没有。

三条叠加后在项目A 上的预期结果：候选为空 → `out_dir = repo/docs` → 与 8-22/8-23 的历史行为一致。

### 3.3 附带的可用性修复

`app.js:887` 现状 `!lastDry` 才渲染 `gui.handoff.wrote`。改为**试运行也显示将写往哪里**（文案区分"将写入"与"已写入"）。用户因此能在真正落盘之前发现目录不对。这是最便宜的一道防线。

---

## 4. 与外部项目的对标（先调研轮子，再决定造不造）

### 4.1 核实过的同类项目

全部经 `gh api /repos/...` 逐一核实存在性、star、最近推送、许可证（未核实到的一律不写）：

| 仓库 | star | 最近推送 | 语言 | 许可 |
| --- | --- | --- | --- | --- |
| `thedotmack/claude-mem` | 91806 | 2026-08-23 | JavaScript | Apache-2.0 |
| `ccusage/ccusage` | 18157 | 2026-08-24 | Rust | NOASSERTION |
| `Maciek-roboblog/Claude-Code-Usage-Monitor` | 8654 | 2026-07-05 | Python | MIT |
| `d-kimuson/claude-code-viewer` | 1274 | 2026-08-18 | TypeScript | MIT |
| `daaain/claude-code-log` | 1202 | 2026-08-25 | Python | MIT |
| `eckardt/cchistory` | 137 | 2026-06-10 | TypeScript | MIT |
| `Yuyz0112/claude-code-reverse` | 2427 | 2025-08-26 | JavaScript | **无许可证** |

另检索到 `citrolabs/ego-lite`(13388)、`matt1398/claude-devtools`(3866)、`badlogic/cchistory`(487)、`coleam00/claude-memory-compiler`(1280)、`ZeroSumQuant/claude-conversation-extractor`(662) 等，与本方案两个核心问题相关度较低，不逐一展开。

### 4.2 关键发现：两个问题社区都没解好

**"最后活动时间"—— 全部用 mtime，无人质疑**

- `daaain/claude-code-log`：`providers/base.py:95-96` 的 `file_mtime_iso` 就是 `datetime.fromtimestamp(path.stat().st_mtime)`，`providers/claude.py:41` 用它填 `created_at`。
- `eckardt/cchistory`：`src/project-discovery.ts:45` `lastModified: stats.mtime`，`:61` 直接按它降序排；`src/jsonl-stream-parser.ts:164,168` 也按 mtime 排文件。
- `d-kimuson/claude-code-viewer`：`ProjectRepository.ts:44` `lastModifiedAt: Option.getOrElse(stat.mtime, …)`，`:53` SQL `orderBy(desc(projects.dirMtimeMs))`。

**唯一的例外**：`claude-code-log` 的 SQLite 缓存里**同时**存了 `first_timestamp` / `last_timestamp`（`cache.py:53-54`、`820-829`），即内容派生的时间范围 —— 但那是全量解析后的副产品，用于展示时间跨度，**不是**用来替代 mtime 做排序。

结论：**尾读取真实最后活动，社区无人做**。项目A 若做，是净新增能力，且实测代价仅 32KB 尾读。

**"归属哪个仓库"—— 最好的做法也只到"多个 cwd 里挑一个"**

`daaain/claude-code-log` 的 `utils.py:162-197` `best_working_dir` 是社区里最讲究的一处：

```python
real_dirs = [wd for wd in working_directories if not _is_temp_path(wd)]   # 先滤掉 /tmp、macOS temp
paths_with_indices = [(Path(wd), i) for i, wd in enumerate(real_dirs)]
best_path, _ = min(paths_with_indices, key=lambda p: (len(p[0].parts), p[1]))  # 层级最浅优先，同层按新近
```

但它的候选池 `working_directories` 来自 `cache.py:918-924` 的 `SELECT DISTINCT cwd FROM sessions` —— **候选全部是 cwd**。它解决的是"同一项目下多个 cwd 该显示哪个"，不是"cwd 与实际工作对象不一致"。

`eckardt/cchistory` 的 `jsonl-stream-parser.ts:338-350` `extractProjectRoot` 更直接：遍历项目目录下的 jsonl，**返回第一个找到的 `cwd`** 就结束。

`d-kimuson/claude-code-viewer` 干脆绕开了 slug 解码问题：`project/functions/id.ts:41-47` 用 `base64url` 编码完整路径当 project id，`decodeProjectId` 是真正可逆的，并配 `validateProjectPath`（`:57-64`）做路径穿越守卫。这是**规避**而非解决 —— 它不需要反解 Claude 的 slug，因为它自己另建了一套 id。

结论：**用工具调用的 `file_path` 反推工作对象，社区无人做**。这是项目A 的独占空白区。

### 4.3 上游 issue 佐证问题真实存在

`anthropics/claude-code` 仓库：

- **#63675**（open，"Session working directory diverges from the directory Claude Code was launched from"）：原文描述"the agent's shell (pwd) and the session's reported *primary working directory* both pointed at the wrong project, causing the agent to attempt work against the wrong repository"。**与用户遇到的完全同一类问题**，上游未修。
- **#30828**（closed）/ **#39424**（closed）：slug 生成把下划线换成连字符，导致同一项目产生两个目录。证实 slug 编码是**有损**的，不可逆。
- **#39148**（open，8 条评论）："feat: add preserve-session plugin for path-independent session history" —— 社区在提路径无关的会话历史方案，说明路径绑定被广泛认为是问题。
- **#83414**（open）："Auto-memory system-prompt block names wrong project directory (uses $HOME encoding, not actual $PWD)"。

对项目A 的直接含义：**slug 反解这条路走不通，不要尝试**。项目A 现状根本不解码 slug（`core/disk.py:192-194` 记录了曾靠目录名取项目名并因 Codex 的 `年/月/日` 布局而被移除），这个决定是对的，本方案不动它。归属判定应当完全建立在转录内容上，而非目录名。

### 4.4 一处值得直接借鉴的细节

`claude-code-log` 的 `utils.py:239-260` 在拼 resume 命令时的 Windows 处理，注释写得很实在：

```
# Windows shells (PowerShell 7+, cmd): double quotes handle spaces; backslashes are
# literal inside them. `pushd` rather than `cd` because cmd's `cd` changes the
# directory but not the *drive* — pasted on C:, `cd "D:\proj"` silently leaves
# you on C: and `claude -r` runs in the wrong project.
```

它还拒绝含换行的 cwd（`:247-248`，理由是多行剪贴板粘贴会逐行立即执行），并用 `_RESUME_SESSION_ID_RE.fullmatch` 校验会话 ID。

项目A 现状 `resume_cmd`（`vitals.py:561-591`）只给 `claude --resume <sid>`，**不带目录切换**。而 resume 必须在正确 cwd 下执行才能找到会话 —— 这正是用户"复制进去执行就改错代码"的另一半原因。

改法（§5 的 T4）：`resume_cmd` 保持现状不动（它的语义是"纯命令"，有既有测试与文档依赖），新增 `resume_cmd_with_cd` property：`pushd "<work_repo>" && claude --resume <sid>`，走 `work_repo` 而非 `repo`，并采用上面三条安全约束（拒绝含换行的路径、盘符路径用 `pushd`、ID 正则校验）。界面把两者都提供，默认复制带 `pushd` 的那条。

---

## 5. 分阶段实施计划

沿用既有计划书的阶段编号（P1/P3 已完成，P2 在工作树未提交），本方案为 **P7–P10**。每阶段独立可验证、可单独回滚。

共同约束（与既有计划书一致，逐字继承）：

- 不删既有函数、变量、注释。
- 不改既有字段的名称、类型、语义。
- 新增能力一律纯增量或默认关闭。
- i18n 三语同步，键名只增不改。
- 每阶段跑全量测试 + `check_i18n.py` + `compileall`。

| 阶段 | 内容 | 风险 | 验证 |
| --- | --- | --- | --- |
| **P7** | §2 最后活动时间（`last_record_time` + `last_active`/`time_source`/`active_at` + 排序切换 + 界面来源标注） | 低 —— 纯新增字段，`mtime` 不动 | 尾读命中率回归、压缩回落、坏尾行容错、排序变化断言 |
| **P8** | §3 输出目录漂移（`find_plan` 三层收紧 + 试运行显示路径） | 低 —— 只收紧判定 | README 不再入选、真计划仍入选、围栏内示例不计数、`docs/` 回落 |
| **P9** | §1 归属重建（`core/attribution.py` + `_AttributionCollector` + `verdict`/`work_repo` + `repos[]` 去重与排序） | 中 —— 新解析路径，需预算控制 | 双侧证据提取、冲突标注、无证据回落、预算截断标注、性能实测 |
| **P10** | §4.4 `resume_cmd_with_cd` + 界面证据展开面板 + 文档/CHANGELOG/README 同步 | 低 | 命令拼装安全性（换行拒绝、盘符 `pushd`、ID 校验）、三语文案 |

顺序理由：P7、P8 是明确 bug 且互不依赖，先修完可立即验证收益；P9 是新能力且改动面最大，放在稳定基线之后；P10 依赖 P9 的 `work_repo`。

### 5.1 每阶段的文件清单

**P7**
- `platform.py`：新增 `TAIL_PROBE_BYTES`、`TAIL_PROBE_MAX`、`last_record_time()`
- `core/vitals.py`：`SessionRow` 增 `last_active`/`time_source`、`active_at` property、`as_dict` 增两键；`scan_one` 调 `last_record_time`；`_group_rows` 与相关排序改 `active_at`
- `core/handoff.py`：`picked.sort` 改 `active_at`
- `cli.py`：`:502`、`:569` 排序改 `active_at`；卡片时间旁标来源
- `core/report.py`：文档时间用 `active_at`，非 `record` 来源时加标注
- `gui/static/app.js`：`ago()`/`title` 改 `active_at`，来源标注
- `i18n/*.json`（三份）：新增来源标注键
- `tests/test_platform.py`、`tests/test_vitals.py`

**P8**
- `core/plan.py`：`PLAN_NAME_DENY`、围栏检测、`INTENT_RX` 加权
- `gui/static/app.js`：试运行显示目标路径
- `i18n/*.json`：`gui.handoff.will_write`
- `tests/test_plan.py`

**P9**
- `core/attribution.py`（新文件）
- `core/vitals.py`：`_AttributionCollector`、`ATTRIBUTION_LINE_BUDGET`、`SessionRow.verdict`/`work_repo`、`repos[]` 去重与排序
- `gui/server.py`：API 透出 `verdict`
- `gui/static/app.js` + `style.css`：证据面板（可折叠，沿用"为什么是这一档"的形态）
- `core/report.py`：文档里写入归属结论 + 依据 + 冲突提示
- `cli.py`：卡片显示归属与冲突
- `i18n/*.json`
- `tests/test_attribution.py`（新）、`tests/test_vitals.py`

**P10**
- `core/vitals.py`：`resume_cmd_with_cd`
- `gui/static/app.js`：复制按钮改用带 `pushd` 的命令
- `CHANGELOG.md`、`pyproject.toml`、`__init__.py`、三份 README、`docs/guide.html`（`build_guide.py` 重新生成）
- `tests/test_vitals.py`

### 5.2 版本规划

- P7 + P8（两个 bug 修复）→ **2.5.1**
- P9 + P10（新能力）→ **2.6.0**

徽章与版本漂移门禁（既有 CI 逻辑）需同步更新测试计数。

---

## 6. 界面设计

### 6.1 会话卡片的归属区改造

现状（截图取证）：

```
工作目录    e:\output\kirara-ai\kirara-ai3.3.0b8
涉及仓库    E:\output\kirara-ai\kirara-ai3.3.0b8（另有 1 个）
```

改造后：

```
在改哪个仓库  C:\Users\devin\agent-handoff-project        [确定 · 258 次文件写入]
启动目录      e:\output\kirara-ai\kirara-ai3.3.0b8       ⚠ 与上一行不同
              ▸ 归属依据（4 类证据）
```

设计约束：

- **"在改哪个仓库"排在最前**，那是用户唯一真正想知道的。
- 置信度做成徽章，沿用 P2 已有的档位徽章样式体系，四档配色对应 `certain`/`likely`/`weak`/`none`。
- `conflict` 时在"启动目录"行给警示标记 —— **不隐藏 cwd**。用户需要知道两者不同，因为 resume 必须在 cwd 下执行。
- "（另有 N 个）"这种纯文本计数**取消**，改为可展开面板，列出全部候选 + 级别 + 命中数 + 代表文件（脱敏后）。理由：截图里真答案就藏在那个"另有 1 个"里，不可展开等于把答案锁起来。
- 面板首次展开才渲染（沿用既有 `▸ 为什么是这一档` 的惰性渲染模式）。

### 6.2 时间显示

```
最后活动   2026-08-25 07:34:39   [来自转录记录]
```

`time_source != "record"` 时改为 `[来自文件时间 · 可能不准]`，并在 tooltip 说明原因（压缩归档 / 尾部无时间戳）。这与 P2 建立的"每个数字标注来源"完全同构。

### 6.3 不做的 UI 改动

- 不引入前端框架、构建步骤、CDN 资源（违背既有零构建取舍）。
- 不做 canvas（`app.js:220` 的注释已论证：canvas 内容对读屏软件完全不存在，SVG 可带可访问名）。
- 不改既有配色体系与设计令牌数值 —— P2 刚把字面量映射成 `--fs-*`/`--sp-*` 令牌且渲染结果零变化，本方案只复用不改动。

---

## 7. 验证计划

### 7.1 每阶段必跑

```
python -m pytest                    # 基线 748 项，全过
python scripts/check_i18n.py        # 三语 526 键对齐（新增后同步上升）
python -m compileall src scripts    # exit 0
python scripts/build_guide.py       # 指南与打包副本哈希一致
agent-handoff --version             # 版本号一致
```

### 7.2 真实数据实测（本机可复现）

| 项 | 期望 |
| --- | --- |
| P7 尾读命中率 | 63/63 Claude 转录在 32KB 内命中；Codex 同样命中 |
| P7 Codex 时间修正 | 287 份负漂移会话的显示时间从"创建时刻"变为真实最后活动 |
| P7 排序变化 | 位次相同数从 122/492 上升到 492/492（按定义） |
| P8 项目A 候选 | README ×3 全部落选，候选为空，`out_dir = docs/` |
| P9 截图会话 `1ee778a2` | `work_repo = C:\Users\devin\agent-handoff-project`，`confidence=certain`，`basis=edit`，`conflict=True` |
| P9 截图会话 `6640504f` | 无文件证据 → 回落 cwd，`confidence=weak`，界面说明"本次会话未改动任何文件" |
| P9 Codex 侧 | 135 份 cwd/workdir 不一致的 rollout 归属改为 workdir 或 workspace_roots |
| P9 性能 | 79MB 转录在预算内完成，与改动前扫描耗时对比 |
| P10 命令安全 | 含换行路径拒绝、盘符路径用 `pushd`、非法 ID 拒绝 |

### 7.3 已知不可本机验证

- `ruff check`：**本机未安装**，不擅自安装。行长按 `line-length = 110` 与既有导入分组人工核对，CI 的 ruff 作业为权威。
- wheel 装进干净 venv 的冒烟门禁：本机 venv 无 pip，CI 上跑。

这两项与 P1/P3 的记录一致，如实标注。

---

## 8. 项目A 已显著超越的亮点（本次改动必须保住）

既有计划书 §2 已逐条列出并说明"任何削弱即为负向调整"。本方案额外确认这几项与新改动的关系：

- **上下文耗尽判定**（`vitals.py:191-245`）：不动。P9 只增归属字段，不碰 `band_for`。
- **压缩窗口全量保留**（`_DigestCollector.digest`）：不动。
- **单一强制脱敏出口**（`report.py:238` `_redact`）：P9 的证据代表文件路径**必须经此出口**，不新开出口。
- **拒绝输出必然失败的命令**（`resume_cmd`）：P10 的 `resume_cmd_with_cd` 继承同样的边界判断（外来转录、archived 会话一律不给）。
- **`band_reason` 的"返回事实而非句子"**（P2，`vitals.py:374-456`）：P9 的 `RepoVerdict` 逐字沿用这一模式 —— 结构化证据 + i18n 渲染文案，三语共用同一份判定依据。

新增一项本次确立的净资产：**归属判定的证据分层**。经核实，社区无同类实现（社区最好的 `best_working_dir` 候选池全是 cwd），且上游 issue #63675 证明该问题真实存在且未解决。

---

## 9. 不做的事

- **不解码 `~/.claude/projects/<slug>`**：编码有损（上游 #30828 / #39424 证实），不可逆。`core/disk.py:192-194` 记录的移除决定是对的，维持。
- **不改 `SessionRow.repo` 与 `SessionRow.mtime` 的语义**：新增并行字段，旧行为在无证据时逐字保留。
- **不引入 LLM 做归属推断**：本项目定位无损，判定必须可解释、可复现。
- **不引入运行时依赖**：零依赖是经过论证的取舍（`pyproject.toml:27-29`）。
- **不抄无许可证仓库**：`Yuyz0112/claude-code-reverse` 无 LICENSE，只作情报不搬代码。`ccusage/ccusage` 许可证为 `NOASSERTION`，同样只作对标。
- **不删既有注释**：这是本代码库最独特的资产。

---

## 10. 待确认事项

动手前需要确认：

1. **阶段顺序与范围**：是否按 P7 → P8 → P9 → P10 推进？是否要先只做 P7+P8（两个确定的 bug）再评估？
2. **工作树里的 P2 改动**：那 1055 行未提交改动是否先提交为 2.5.1 的一部分，还是与 P7/P8 一并提交？
3. **`conflict` 时的默认动作**：界面"用这个仓库交接"按钮默认走 `work_repo`（改了什么）还是 `repo`（在哪启动）？我的建议是 `work_repo`，但把 `repo` 摆在旁边一键可选 —— 因为 resume 必须在 cwd 下跑，两者用途不同。
4. **`ATTRIBUTION_LINE_BUDGET` 取值**：建议 1200。太小会漏掉后段的写操作，太大影响大转录扫描速度。是否接受？
