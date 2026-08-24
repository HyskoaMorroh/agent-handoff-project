<h1 align="center">agent-handoff</h1>

<p align="center"><b>会话卡死时，把进度从对话里搬进仓库</b></p>

<p align="center">
  <a href="CHANGELOG.md"><img alt="版本" src="https://img.shields.io/badge/version-2.4.0-1F6B4F?style=flat-square"></a>
  <a href="pyproject.toml"><img alt="Python" src="https://img.shields.io/badge/python-3.9%20%E2%80%93%203.13-2F5473?style=flat-square"></a>
  <a href="tests/"><img alt="测试" src="https://img.shields.io/badge/tests-681%20passed-1F6B4F?style=flat-square"></a>
  <a href="pyproject.toml"><img alt="运行时依赖" src="https://img.shields.io/badge/runtime%20deps-0-7C6210?style=flat-square"></a>
  <a href="LICENSE"><img alt="许可" src="https://img.shields.io/badge/license-MIT-6B7B7E?style=flat-square"></a>
</p>

<p align="center">
  简体中文 · <a href="README.zh-Hant.md">繁體中文</a> · <a href="README.en.md">English</a>
</p>

AI 编码会话会因为上游 400、供应商熔断、上下文超限而突然死掉。死掉的不是代码——代码在磁盘上——死掉的是**只存在于那段对话里的东西**：目标、已经排除的方案、红线约束、下一步该做什么。新会话从零开始，于是重做已完成的工作，或者改掉你明确说过不能碰的文件。

> **什么时候跑它**：当会话的上下文快满了。工具直接读转录里的 token 数
> （Claude 的 `message.usage`、Codex 的 `token_count` 事件），而不是猜——
> 已经压缩过的会话一律按「满过」对待，因为自动压缩只在装不下时才触发。
> 详见 [会话体检的判据](#会话体检的判据)。

## 它做四件事

| | 做什么 | 凭什么 |
|:--:|---|---|
| **1** | **提交快照** | 自动排除计划文档声明为「用户私有」的文件 |
| **2** | **回填计划** | 按客观证据勾选：文件是否存在、符号是否**真被定义** |
| **3** | **传承会话** | 勾选相关会话，把它们**自己写下的**压缩摘要与你的原话带过去 |
| **4** | **生成交接** | 一份交接 Markdown + 一段可直接粘贴的开场提示词 |

它**不硬编码任何项目知识**：项目名、路径、任务名、测试命令全部从仓库自身推断。

<p align="center">
  <img src="docs/img/gui-light.png" alt="会话体检界面（浅色）" width="880">
  <br><sub>会话体检 · 浅色</sub>
</p>

<details>
<summary><b>深色（跟随系统）</b></summary>
<p align="center">
  <img src="docs/img/gui-dark.png" alt="会话体检界面（深色）" width="880">
</p>
</details>

<details>
<summary><b>交接结果长什么样</b>（完成度判定 · 缺口 · 受保护文件 · 开场提示词）</summary>
<p align="center">
  <img src="docs/img/gui-result.png" alt="交接结果：完成度表格、缺口明细、受保护文件与可复制的开场提示词" width="880">
  <br><sub>注意 <code>Task 2</code>：文件在、但它声明的 <code>render_report</code> 没定义 → 判「部分」，不勾。<br>
  这正是这个工具最该被看到的一条判据。</sub>
</p>
</details>

---

## ⚠ 先读这一节：什么时候**不该**用它

> [!IMPORTANT]
> 同一个 APP、同一台机器、同一个供应商、上下文没满、会话文件完好时——
> **用原生续接，不要用这个工具。**

```bash
claude --resume        # 或 claude --continue / 会话内 /resume
codex resume           # 或 codex resume --last
```

原生续接恢复的是**完整对话历史**（Claude Code 官方文档：「the full history,
including tool calls and results」），即原样重放 token，**无损**。
本工具产出的是**有损摘要**，不可能等于无损重放。

它的价值只在原生续接**结构性做不到**的七类场景：

| 场景 | 为什么原生做不到 |
|---|---|
| **上下文已耗尽** | 官方自己的解法也是压缩摘要——闲置 1 小时 + 超 10 万 token 时弹「Resume from summary」，等于跑 `/compact` |
| **跨 APP**<br>Claude Code ↔ Codex | 两边格式与事件语义完全不同，**无任何官方导入机制** |
| **换模型 / 换供应商** | 文档明列：模型在已退役、被 `--model` 覆盖、或 Bedrock 等部署 ID 供应商上**不恢复** |
| **会话文件损坏** | `Failed to resume the conversation`，退出码 1 |
| **要丢弃被污染的历史** | 原生只能全带或摘要；`/branch` 是复制而非裁剪 |
| **跨机器** | 转录可以拷，但官方按 ID 查找只在「恰好一个项目持有该 ID」时才解析，手工副本会被判 not-found |
| **plan mode / 后台任务**<br>**MCP / CLI 启动参数** | 文档明列**永不恢复**——写进交接文档反而更可靠 |

### 哪些东西原理上传不过去

工具会把这一段写进生成的提示词，免得接续会话把「摘要里没有」当成「没发生过」：

| | 传不过去的东西 | 后果 |
|:--:|---|---|
| 🧠 | 模型内部推理状态与 prompt 缓存 | `encrypted_content` 是服务端加密串，换会话即失效 |
| 🔑 | 工具授权运行时态 | 新会话会重新弹权限确认 |
| 🔌 | MCP 连接与认证令牌 | 转录只记工具**名字**，不记连接 |
| ⚙ | 后台进程、监听端口、已启动的服务 | 「服务已启动」的假设失效 |
| 🚫 | 被否决方案的推理过程 | thinking 无签名不可回放，且多在子代理转录里——实测「过程:报告」= **124:1**（14.6 MB vs 118 KB） |

---

## 装

需要 Python 3.9+ 与 git。没有第三方运行时依赖——这个工具在环境已经出问题之后才被运行，任何需要 `pip install` 的东西都是一条新的失败路径。

```bash
pip install -e .
```

或者不装，直接跑：

```bash
# Windows
scripts\agent-handoff.cmd .
# Linux / macOS / WSL
./scripts/agent-handoff.sh .
```

## 用

三种入口，功能等价，选顺手的。

### 网页界面（推荐）

最好看，三语随时切换，浅色/深色/跟随系统。

```bash
agent-handoff --gui
```

只绑定 `127.0.0.1`，每个请求校验一次性令牌。它能对任意路径跑 `git commit`，所以这不是可选项。

### 交互菜单（不想记参数）

把项目文件夹拖进窗口就能填路径。

```bash
python -m agent_handoff.menu
# Windows 双击 scripts\双击运行.cmd ；Linux / macOS 跑 ./scripts/run.sh
```

### 命令行

适合写进脚本。加 `--json` 让输出被程序消费，退出码有语义（见下）。

```bash
agent-handoff --vitals
```

### 典型流程

```bash
# 1. 先看哪个会话该交接了（只读，不碰任何仓库）
agent-handoff --vitals

# 2. 截图对不上是哪段对话？按 ID 前几位 / 目录名 / 提问关键词找
agent-handoff --find 01a00e83
agent-handoff --find 工作流

# 3. 预演：只显示将要做什么，不写任何文件
agent-handoff /path/to/project --dry-run

# 4. 勾选要传承的会话，然后执行
agent-handoff /path/to/project --pick-sessions

# 5. 转录占了多少磁盘？哪些可以安全扔掉？（只读元信息，毫秒级；不删任何文件）
agent-handoff --sweep
agent-handoff --sweep --by-repo          # 额外按仓库聚合（要读转录，慢很多）
agent-handoff --sweep --out disk.md      # 导出 md 报告
```

### 全部参数

| 参数 | 作用 |
|---|---|
| `repo` | 仓库路径，默认当前目录 |
| `--plan PATH` | 计划文档路径；省略则自动探测最新的含复选框任务文档 |
| `--out PATH` | 交接文件输出路径；默认与计划文档同目录（同日重跑见下方说明） |
| `-m, --message MSG` | 提交信息；省略则自动生成 |
| `--no-commit` | 不提交，只分析并生成交接文件 |
| `--skip-tests` | 不跑测试（快速模式） |
| `--test-timeout N` | 单条测试命令超时秒数，默认 900 |
| `--vitals` | 只体检本机会话转录并退出，不碰仓库 |
| `--no-vitals` | 跳过会话体检（交接文件里不含体征表） |
| `--sweep` | 报告转录占了多少磁盘、哪些可以安全回收，然后退出；**不删任何文件** |
| `--by-repo` | 配合 `--sweep`：额外按仓库聚合占用（要读转录，慢很多） |
| `--find KEYWORD` | 按会话 ID / 目录 / 话题关键词定位会话；可重复或用逗号分隔一次找多个 |
| `--limit N` | 每个智能体最多扫描多少个最新转录，默认 12 |
| `--pick-sessions` | 交互勾选要传承的会话（列出后按编号选择） |
| `--export-bundle [DIR]` | 打成可拷到另一台电脑的包（**含转录副本**）；省略路径写到 `~/.agent-handoff/bundles/` |
| `--import-bundle DIR` | 读一个包，把里面的路径解析到本机并报告；只读，不复制任何转录 |
| `--sessions PATH` | 直接指定要传承的转录；可重复，或用逗号分隔 |
| `--force` | 忽略并发写入警告强行继续 |
| `--dry-run` | 全程只打印将要做什么，不写任何文件 |
| `--lang {zh-Hans,zh-Hant,en}` | 界面与产出语言；省略则按系统区域设置 |
| `--gui` | 启动本地网页界面 |
| `--port N` | 网页界面端口；0 自动挑空闲端口 |
| `--no-browser` | 启动网页界面但不自动打开浏览器 |
| `--jobs N` | 并行度；0 按 CPU 核数自动决定 |
| `--json` | 以 JSON 输出结果，供脚本消费 |
| `--version` | 打印版本号后退出 |

退出码：`0` 成功 · `1` 未找到匹配的会话 · `2` 参数或环境错误 · `3` 检测到并发写入而停止。

环境变量 `AGENT_HANDOFF_LANG` 优先于系统区域设置，方便在中文系统上产出英文交接文档。

**同一天重跑不会丢掉上一份。** 交接文件名只带日期，第二次运行会覆盖第一次。
覆盖前先比字节：内容相同就直接覆盖（多数重跑是「调完再跑」，要的就是最新那份），
内容不同才把旧那份留成 `<原名>.prev.md`。只保留最近一份——留成长链又变成
「该读哪个」的问题，真正的历史归 git 管。

## 它怎么知道你的项目

| 信息来源 | 推断出什么 |
|---|---|
| git 元数据 | 分支 / HEAD / 未提交改动 / 领先远程多少提交 |
| `pyproject.toml`、`package.json`、`Cargo.toml`、`go.mod` | 技术栈与测试命令 |
| 计划文档 `**Files:**` 段 | 每个任务应产出哪些文件 |
| 计划文档 `**Interfaces:**` 段 | 每个任务应产出哪些符号 |
| 计划文档 约束段 | 哪些文件不得提交 |
| 计划文档 Goal / Constraints 段 | 提示词里要点名让新会话先读的段落 |

计划文档格式（自动探测最新的含复选框任务文档）：

```markdown
**Goal:** 一句话说清这个项目要做什么。

## Global Constraints
- `docs/LOGO.jpg` 是用户私有文件，不得提交。

### Task 1: 建立数据层

**Files:**
- Create: `src/db/schema.py`
- Modify: `src/config.py`

**Interfaces:**
- Produces: `create_schema`, `Migration`

- [ ] **Step 1** 定义表结构
- [ ] **Step 2** 写迁移脚本
```

Task 1 的两个文件都存在、两个符号都真的被定义了，它才会把两个 Step 勾上。文件存在但符号没定义 = "部分完成"，不勾。

解析器认得真实 markdown 的写法差异，因为漏认不会报错、只会静默失真：
动词可以加粗（`- **Modify**: x`）、冒号可省（`- Modify x`）、动词可省
（`- \`x\` — 说明`）、一行可以列多个路径、列表符 `-` `*` `+` 都算、
标题 1–6 级且允许缩进、`Task` 与 `Phase`/`阶段` 都认、
`Produces` 之外还认 `Exports`/`Provides`/`提供`/`导出`。

### 判定的粒度是 Task，不是 Step

一个 Task 的全部 Step 一起勾或一起不勾。证据只来自 Task 的 `**Files:**` 与
`**Interfaces:**` 声明——**Step 文字里提到的文件名和符号不算证据**。

```markdown
### Task 1: core

**Files:**
- Create: `mod.py`

**Interfaces:**
- Produces: `alpha`

- [ ] **Step 1** 定义 alpha
- [ ] **Step 2** 定义 gamma        ← gamma 没在 Interfaces 里声明
```

上面这个 Task 里 `mod.py` 存在、`alpha` 真被定义，于是判定为完成，**两个 Step
一起被勾上**——包括那个 `gamma` 还不存在的。这不是判错，是它按声明判：
`gamma` 从未进入过证据集。

想让 Step 2 独立判定，就把它拆成自己的 Task，或者把 `gamma` 写进
`**Interfaces:**`。粒度做在 Task 上是因为 Step 是自然语言，从「定义 gamma」
这句话里猜符号名必然出错，而误勾是不可逆的——勾掉的步骤会从待办里永久消失。

### 符号是怎么判定「真的被定义」的

不是「文件里出现过这个词」。三类定义形态都算，且**引用不算**：

```ts
interface Intent {
  undo: () => void          // ✅ interface 成员
}
const intent = {
  undo: () => { step() }    // ✅ 对象字面量属性
}
class M {
  performAction(a: () => void) {   // ✅ 方法简写，无关键字前缀
  }
}

intent.undo()               // ❌ 调用不是定义
// interface undo 由 store 提供   ❌ 注释里的引用不是定义
```

后两种写法在 TS / Vue / 现代 JS 里才是主流，而它们**一个关键字前缀都没有**。
只认「关键字 + 空格 + 名字」的检测器会把一个写满 `undo: () => void` 的仓库
判成「符号全缺」，于是接续会话重做已经做完的工作——这是本工具遇到过的
真实误判。判定前会先把注释挖空（用等长空白替换以保留行列位置），
因为**假阳性比假阴性更危险**：它会让回填勾掉从未实现的步骤，待办永久消失。

计划文档自身被排除在符号检索之外。否则文档里写的
``- Produces `undo` `` 会被搜到，变成「已实现」的证据，自己满足自己。

检索失败（三条后端全挂）与「查过、确实没有」是两回事：前者不允许判「完成」，
因为勾选会写进计划文档且不可逆。

## 会话体检的判据

**判据是上下文占用，不是文件体积。** 占用数就写在转录里，不需要猜：

| 来源 | 占用 | 上限 |
|---|---|---|
| Claude Code | `message.usage` 的 `input_tokens` + 两种 cache 读入 | 转录里**没有**，按绝对阈值判 |
| Codex | `payload.info.last_token_usage.total_tokens` | `payload.info.model_context_window`（实测 121600） |

Codex 那一格取 `total_tokens` 而不是 `input_tokens`，是为了跟 Codex 自己对齐：
它显示占用率时走 `TokenUsage::tokens_in_context_window()`，实现就是 `total_tokens`。
`TokenUsage` 有六个字段（input / cached_input / cache_write_input / output /
reasoning_output / total），只取 input 会漏掉输出与推理——**推理密集的会话因此
被系统性低估**，而这类会话恰恰最需要交接。同时仍然用 `last_token_usage` 而不是
`total_token_usage`：后者是全会话累计，会远超窗口，当占用用会永远判成爆满。

有上限就算真占用率（≥90% 立刻交接，≥75% 尽快，≥55% 留意）；
没有上限就拿占用量对照按 200k 窗口折算的阈值——对更大的窗口偏保守，
宁可早提醒也不要漏掉真要满的。

**读不到正文时如实说不知道。** 压缩归档的会话（`.jsonl.zst`）在本机缺少 zstd
解压实现时，判定是「读不到正文」而不是任何一个风险区间。不走体积兜底是因为
压缩后的体积与阈值不可比：zstd 通常压到十分之一上下，一个 27 MB 的爆满会话在
盘上只剩 2 MB 多，体积判据会说「留意」甚至「健康」——正好把最该交接的会话判成
最不需要交接的。排序上它落在「留意」与「健康」之间：不算健康，也不挤掉有真实
证据的行。

**窗口大小可以自己声明。** 任何单一绝对阈值都不可能同时对 128k 和 1M 窗口正确：
实测本机一个 Claude 会话 102365 token，按 200k 折算判「健康」；若模型其实是
128k 窗口，那已经 80%、该判「尽快交接」；若是 1M 则只有 10%，「健康」是对的。
同一个数字，结论相反。

```bash
# Windows（当前窗口）
set AGENT_HANDOFF_CONTEXT_WINDOW=1000000
# Windows（持久，开新窗口也生效）
setx AGENT_HANDOFF_CONTEXT_WINDOW 1000000
# Linux / macOS
export AGENT_HANDOFF_CONTEXT_WINDOW=1000000
```

优先级：**转录自己写的上限 > 你声明的 > 按 200k 折算的兜底**。转录里的是实测值，
所以 Codex 会话不受这个变量影响。写了非法值（负数、零、小数、非数字）按没写处理
——一个笔误不该把整张体征表判成健康。

工具不去猜模型型号：模型会换，而你知道自己在用什么。官方也从未公布固定的压缩
触发阈值，且自己在「把 1M 窗口按 200k 算」这件事上出过 bug。

**压缩过就按「满过」对待。** 自动压缩只在上下文装不下时才触发，
所以它是最硬的证据，比任何百分比都清楚。压缩一次即「尽快交接」，
两次以上「立刻交接」——每次压缩都在丢更早的历史。

| 来源 | 压缩事件 |
|---|---|
| Claude Code | `type:"system"` + `subtype:"compact_boundary"`，带 `compactMetadata.preTokens` |
| Codex | `type:"compacted"` 记录 |

### 为什么不用体积

体积与真实占用严重脱钩。本机实测四个转录：

| 转录 | 体积 | 实际占用 | 压缩 | 按体积会判 | 实际该判 |
|---|---|---|---|---|---|
| A | 1.0 MB | 194,183 token | 0 | 健康 | **立刻交接** |
| B | 2.0 MB | 172,490 token | **10 次** | 留意 | **立刻交接** |
| C | 27.4 MB | 710,340 token | 0 | 立刻交接 | 立刻交接 |
| D（Codex） | 6.4 MB | 121,407 / 121,600 = **99.8%** | 1 次 | 尽快交接 | **立刻交接** |

B 最能说明问题：**它体积小恰恰因为压缩一直在丢历史**。越小可能丢得越多，
而体积判据会把这种最该交接的会话标成「留意」。

体积仍然保留为**兜底**：转录里读不到 token 数时（旧格式、被截断的文件），
它是唯一可用的信号。下面这张图是那套兜底阈值的实测依据：

<p align="center">
  <img src="docs/img/bands.svg" alt="转录体积与实测致命错误率：1 MB 以下 0%，1 MB 起 17%，3 MB 起 30%，8 MB 起 100%" width="880">
</p>

**致命错误**指真的杀死过会话的签名（`content-blocked`、供应商熔断、
无可用渠道、图片尺寸超限），且必须出现在**错误载荷字段**里。
在整行原文上裸匹配会把「讨论这些词」当成「发生了这些事」——实测 14 个主转录的
239 个裸匹配命中里，94 个来自 assistant 正文、83 个来自 user 正文
（你自己的 CLAUDE.md 里写着「出现 content-blocked、熔断时…」也会被算进去）。

**中断轮次**（`turn_aborted`）单独计数：实测 Codex 侧 `is_error` 在 40 个
rollout 里只有 3 次，而 `turn_aborted` 有 6 次。把半成品当成已完成，
是交接里最贵的误判。

## 会话内容是怎么传承的

勾选会话后，工具从转录里提取**会话自己写下的记录**，而不是靠猜：

| 来源 | 内容 | 为什么要它 |
|---|---|---|
| Codex `compacted` 事件 | 模型在上一次压缩时写的交接摘要，**全部窗口** | 含仓库路径、真实 HEAD、已读文档、任务范围 |
| `compacted.replacement_history` | 被摘要替换掉的**用户原话**，逐字 | 摘要是转述，转述会丢措辞里的约束 |
| Claude `ai-title` | 一句话话题摘要 | 人认会话靠这个，不是靠 ID 前八位 |
| Claude `last-prompt` | 最后一次用户输入 | 说明会话停在哪 |

**压缩摘要必须保留每一个窗口。** 实测本机 70 个带压缩的 rollout（52 个是
多窗口，最多 19 个窗口）：`window_number` 递增、`previous_window_id` 串成链，
每个窗口只总结它自己那一段，**没有任何样本**的末窗逐字包含首窗。
只留最后一个会丢掉中位 **78%**、p90 96% 的具体事实（commit sha、文件路径、
测试计数），其中 11 个 rollout 的「用户目标 / 红线约束」只出现在早期窗口——
恰恰是最不能丢的部分。保留全部窗口后实测丢失率 **0%**（70/70）。

摘要也不截断：实测 62/70 个末窗超过 4000 字符，中位会被切掉 2925 字符、
最多 13241 字符，而切掉的往往正是结论与待办。完整摘要写进交接**文档**
（文件多几十 KB 无所谓），提示词里只放话题与路径。

### 多个会话汇总成一份提示词

可以一次勾多个会话（体检列表与找会话结果都能勾），它们汇总进**同一份**提示词，
每个会话各带六项定位信息：

```text
前序会话（共 3 段）。它们的完整摘要在交接文件里，先读那份再动手：
  · Codex｜画布撤销重做｜2026-08-23 17:15:30
      转录：~\.codex\sessions\2026\08\23\rollout-...-01a02de7-....jsonl
      会话 ID：01a02de7-55f3-7c62-93b0-59897b54736e
      工作目录：E:\output\kirara-ai\kirara-ai3.3.0b8
      原生续接（无损，先试这个）：codex resume 01a02de7-55f3-7c62-93b0-59897b54736e
      深度链接：codex://threads/01a02de7-55f3-7c62-93b0-59897b54736e
      若已打包带走（--export-bundle）：包内 sessions/01a02de7-.../ 有完整对话
```

六项各有用途，缺一样接续会话就会卡住：

| 项 | 用途 |
|---|---|
| 话题 | 认得出是哪一段工作 |
| 转录路径 | 去读原文 |
| 会话 ID | 喂给 `--find`、贴进 issue |
| 工作目录 | cd 到对的地方——多个会话可能在不同子目录里跑过，只给仓库根不够 |
| 续接命令 | 无损回去，**首选路径**；这份交接是退路 |
| 包内四件套位置 | 读完整对话（`sessions/<id>/session.md`） |

**深度链接指向这个会话本身，不是它 fork 自的那个。** 这一点曾经出错：
`thread_id`（派生来源）优先于 `session_id`，于是第三个会话的链接指向第二个的 ID
——点开是别人的对话，而链接语法合法所以不报错。只有 Codex 注册了 URI scheme，
Claude Code 没有，那边不给链接而不是编一个 `claude://`。

跨 APP 混选没问题（Claude Code 与 Codex 可以同时勾），每条按自己 APP 的形态给
续接命令。但**跨项目会明确提示**：交接固化的是一个仓库的状态，把两个现场汇总进
一份提示词，接续会话会拿 A 项目的结论去改 B 项目的代码。提示而不拦——
同一份工作跨两个仓库（前后端分离、主仓 + 插件仓）是真实场景。

## 转录存在哪里，占了多少

**转录不会因为项目在 D/E/F 盘而跟着搬家。** 实测本机 E 盘上的项目，转录仍在
C 盘家目录下——盘符只出现在转录内容记录的 `cwd` 里，不影响存储位置：

| 应用 | 默认位置 | 环境变量改位置 |
|---|---|---|
| Claude Code | `~/.claude/projects/<cwd 的 slug>/*.jsonl` | `CLAUDE_CONFIG_DIR` |
| Codex | `~/.codex/sessions/年/月/日/rollout-*.jsonl` | `CODEX_SESSIONS_ROOT`、`CODEX_HOME` |
| Codex（归档） | `~/.codex/archived_sessions/rollout-*.jsonl` | 同上 |
| Codex（压缩归档） | 同上，但扩展名是 `.jsonl.zst` | 同上 |

会变的是**数据目录本身**：两个应用都支持用环境变量整体挪走（笔记本 C 盘小、
指向 D 盘是常见做法）。工具优先读这些变量，家目录仍作兜底——两处都可能有
历史记录，只认一边就是丢会话。Codex 的解析顺序是 `CODEX_SESSIONS_ROOT`（直接
指向会话目录本身）→ `CODEX_HOME/sessions` → `~/.codex/sessions`。
WSL 里还会去 `/mnt/c/Users/<name>` 找宿主机的记录。
搬机器时怎么指过去，见[换电脑](#换电脑)。

**压缩归档的 Codex 会话。** Codex 会把 7 天前的 rollout 原地 zstd 压缩成
`.jsonl.zst`。本工具两种扩展名都扫，读取时自动解压。解压能力按可信度依次尝试
Python 3.14 的标准库 `compression.zstd`、`zstandard`、`pyzstd`——**都不装也能用**：
会话仍然出现在列表里，判定标注为「读不到正文」而不是假装它很健康
（压缩后的体积只有原文的十分之一上下，拿它对照体积阈值会把最该交接的会话
判成最不需要交接的）。想读到正文，装其中任一个即可：

```bash
pip install zstandard        # 或 pyzstd；Python 3.14 及以上自带，无需安装
```

这是本工具唯一的可选依赖，且**不写进 `dependencies`**：零运行时依赖是刻意取舍——
工具运行在刚崩掉的环境里，任何要 pip 装的东西都是一条失败路径。

`--sweep` 报告占用，**不删任何文件**：

```bash
agent-handoff --sweep
```

它只读文件元信息、不读内容，这是它快的唯一原因。实测本机 423 个转录 1.09 GB：

| 做法 | 耗时 |
|---|---|
| 只遍历目录 | 8 ms |
| 遍历 + `stat`（`--sweep` 用这个） | **10 ms** |
| 读每个文件前 64 KB | 219 ms（慢 22 倍） |

**按体积排行，不按时间过期**——实测本机「超过 30 天」是 0 个，而单个 90 MB
的文件占了总量 8%。真正吃磁盘的永远是少数几个文件。

报三类可安全回收的，各自附上「为什么安全」：

| 分类 | 为什么可以回收 |
|---|---|
| 子代理转录 | 子代理自己的工作日志，不是你参与过的对话——它本来就无法续接 |
| 已归档会话 | 已在 Codex 里归档，续接不了；但正文还在 |
| 空会话 | 不到 32 KB，装不下一轮有内容的对话 |

三类互斥计数：一个既小又属于子代理的转录只算一次，不会让「可回收」被重复累加。

加 `--by-repo` 按仓库聚合（要读转录才能拿到 cwd，实测 24 秒 vs 12 毫秒，
所以是独立开关）。它给出的信息很直接——本机 E 盘那两个 kirara-ai 项目
占了 828 MB，是总量的 76%。

> **为什么不自动删。** 转录里可能存着那段工作的唯一记录，而删除不可逆。
> 判断哪些真能丢需要人看一眼，所以工具只给可审阅的清单，由你决定。
> 这与它其余部分的立场一致：只给证据，不做不可逆动作。

## 换电脑

转录是普通文件，拷到新机器就能读——工具会认出它们不是本机产生的，据此调整。

**认外来靠结构，不靠猜用户名。** 硬编码用户名清单换台机器就失效，所以判据是
路径里的家目录段形状：`<盘符>:\Users\<名字>`、`/home/<名字>`、`/Users/<名字>`、
`/mnt/<盘>/Users/<名字>`。其中的名字不是本机任何一个（`~` 的目录名，加上
`USERNAME` / `USER` / `LOGNAME`——WSL 下两侧名字可能不同，都算本机）就是外来。

认出外来后有三处不同：

| | 本机会话 | 外来会话 |
|---|---|---|
| 上下文判定 | 正常 | **一样正常**（token 数与机器无关） |
| 原生续接命令 | 给出 | **不给**——应用只索引自己数据目录里的会话，命令一定报「找不到」 |
| 路径 | 直接可用 | 明确标注「这是那台机器上的路径」，不让你照着去找文件 |
| 内容传承 | 可勾选 | **一样可勾选**——那正是迁机时最想带走的东西 |

**别人的用户名也会脱敏。** 交接文档里 `D:\Users\bob\myproj` 变成
`D:\Users\_USER_\myproj`，只换名字那一段、保留其余路径（你仍要靠它判断那台机器上
的项目在哪）。Claude 的 slug 目录名 `D--Users-bob-myproj` 同样处理——用户名嵌在
`-` 分隔的一段里，漏掉它就会从转录路径漏出去。

分隔符混用（`C:\Users/devin/proj`）、POSIX 壳的写法（Git Bash 的
`/c/Users/devin`、WSL 的 `/mnt/c/Users/devin`）、少见的家目录布局
（`/export/home/`、`C:\Documents and Settings\`、`\\?\C:\Users\`）都覆盖了。

**密钥也会脱敏，而且是第一道。** 文档要写「用户问了什么」「最后一句输入」
「压缩摘要」，而人在会话里粘过 API key、`Authorization: Bearer …`、数据库口令
是常态。凭据一旦被自动提交进 git 历史，删文件也去不掉——只能改写历史或轮换密钥。
所以这一道不是可选项：

| 形态 | 例子 | 文档里 |
|---|---|---|
| OpenAI / Anthropic | `sk-ant-api03-…` | `sk-***` |
| GitHub | `ghp_…`、`github_pat_…` | `gh*_***` |
| Slack / AWS / Google | `xoxb-…`、`AKIA…`、`AIza…` | `xox*-***` 等 |
| HTTP 认证头 | `Authorization: Bearer eyJ…` | `Bearer ***` |
| PEM 私钥 | `-----BEGIN … PRIVATE KEY-----` | 整块替换 |
| URL 口令 | `postgres://admin:pw@host` | `postgres://admin:***@host` |
| 键值写法 | `api_key: "…"`、`TOKEN=…` | `api_key: ***` |

**保留前缀而不是整段打码**：`sk-***` 告诉你该去轮换哪一个密钥，`***` 什么都没说。

**只认有明确前缀的形态，不猜高熵字符串。** 那类启发式会把 commit sha、正常的
base64、长文件名一起打码，把文档毁掉。值为纯数字时一律不动——`max_tokens=8192`、
`token_count: 123` 是模型配置的常态，而上下文占用恰恰是本工具最核心的判据。
代价是自造格式的私有 token 可能漏掉；这是刻意的取舍，一份处处是 `***` 的交接
文档等于没有文档。

**提示词不脱敏，文档才脱敏。** 提示词是你在本机粘进新会话的，需要真实路径才能
用；文档会进 git、可能被推到公开仓库。两者受众不同。

**要在新机器上接着干活，看提示词里的仓库身份**，那部分本来就是便携的：

```
仓库身份（换机器时用这个定位，不要依赖上面的本机路径）：
  https://github.com/you/proj.git @ eb8a6aa2217a
```

没有远程时它会明说「这个仓库只存在于本机，接续必须在同一台机器上进行」，
并给出未推送的提交数——别以为在新机器上 clone 就能拿到那些改动。

### 怎么把工具和会话都搬过去

**最省事的办法：打个包带走。**

```bash
# 在旧机器上：交接的同时打包（勾选的转录会被复制进包里）
agent-handoff /path/to/project --pick-sessions --export-bundle

# 拷走整个包目录（默认在 ~/.agent-handoff/bundles/<仓库名>-<日期>/），然后在新机器上：
agent-handoff --import-bundle ~/bundles/myproj-2026-08-23
```

包里有四样东西：`manifest.json`（含 `schema_version`）、`handoff/`（交接文档 +
开场提示词）、`transcripts/`（**勾选转录的副本**）、`sessions/<id>/`（**每个会话
自己的四件套**）。

每会话四件套各回答一个问题：

| 文件 | 回答什么 |
|---|---|
| `resume.txt` | 怎么**无损**回到这个会话（首选路径） |
| `session.md` | 这个会话说过什么，可直接粘给新会话 |
| `locate.txt` | 工作目录、会话 ID、深度链接 |
| `meta.json` | 同一批信息的机器可读版本 |

`session.md` **分级带**：用户原话与助手结论按原文进，工具调用压成一行
「名字 -> 结果摘要」，thinking / reasoning 不进。依据是实测的体积分布——
一份 13452 行的 Codex rollout 里 `function_call` 与其输出占 6746 行（50%），
而真正的对话消息只有 1614 行。实测压缩比：30 MB 的 Claude 转录 → 95 KB，
90 MB 的 Codex → 959 KB。超限时先丢最早的工具摘要再截人话，并明说丢了多少。

harness 注入的样板不会被当成用户原话：斜杠命令回显、后台任务通知、plugin
清单都以 `user` 身份出现在转录里，实测一份转录里 `<command-name>` 有 28 次而
真实诉求只有 23 条。过滤后实测 116 条用户轮全部是真实诉求。

为什么必须带副本：交接文档里的转录路径是**这台机器上的位置**，不是内容。
`~/.claude/projects/C--Users-devin-proj/<id>.jsonl` 这个路径里，目录名本身
编码了源机的 cwd——换机器后那个目录不存在，新会话拿到路径也读不到东西。
只给路径不给内容，就是「换机必然失效」的根本原因。

包里的路径存成占位符（`{CLAUDE_ROOT}/…`、`{CODEX_ROOT}/…`、
`{CODEX_ARCHIVED_ROOT}/…`），导入时**按目标机器自己的根重新解析**，不做字符串
替换——新机器的 `CLAUDE_CONFIG_DIR` 可能指向完全不同的位置，甚至另一个盘。
本机没有对应根时不编造路径，只告诉你用包内副本。

包默认写在**仓库外**（`~/.agent-handoff/bundles/`）：里面有转录副本，而转录可能
含你在会话里粘过的任何东西，默认写进仓库就等于默认交给 `git add -A`。要放进
仓库请显式给路径。

`--import-bundle` **只读**：不往 `~/.claude` 或 `~/.codex` 写任何东西。要不要把
副本放进去由你决定——那会改变那个 app 的会话列表。（Claude Code 自 v2.1.205 起
也明确禁止篡改会话转录文件。）

> **包里的转录副本没有脱敏。** 交接文档与提示词经过脱敏（家目录、他机用户名、
> 密钥形态），但 `transcripts/` 里是**原样字节**——副本的价值就是保真，脱过的
> 转录作为「那段工作的记录」已经失真。这意味着里面可能有你当时粘进会话的密钥、
> 令牌、口令。`manifest.json` 的 `warning` 字段写着这件事，命令行在真带了副本时
> 也会提醒。**分享包之前先自己看一眼。**

---

**不打包也能搬：没有任何一处写死用户名或盘符。** 运行时每个位置都是当场算出来的：
家目录来自 `expanduser("~")`，检出根来自启动脚本自己的位置，会话目录来自
`CODEX_HOME` / `CLAUDE_CONFIG_DIR` 或家目录。整个目录拷到另一台电脑
（用户名不同、盘符不同）直接可用，不改一行配置。

把别的电脑的 `.codex` / `.claude` 拷过来后，用环境变量指过去——路径可以在
任何盘、任何目录名下：

```bash
# Windows（cmd）
set "CODEX_HOME=D:\from-old-laptop\.codex"
set "CLAUDE_CONFIG_DIR=D:\from-old-laptop\.claude"
agent-handoff E:\output\myproj --pick-sessions

# POSIX
export CODEX_HOME=/mnt/backup/from-old-laptop/.codex
export CLAUDE_CONFIG_DIR=/mnt/backup/from-old-laptop/.claude
./scripts/agent-handoff.sh ~/proj/myapp --pick-sessions
```

指过去之后本机家目录**仍然会扫**，两边的会话一起列出、一起可勾选——迁移是
「多一处」不是「换一个」，否则搬完就看不到本机历史了。

工具自己怎么搬，三选一，都不用改文件：

| 做法 | 命令 |
|---|---|
| 装成命令（推荐） | `pip install -e .`，之后 `agent-handoff` 在任何目录可用 |
| 直接跑检出 | `python -m agent_handoff.cli`（在检出根，或先设 `PYTHONPATH=<检出>/src`） |
| 用启动脚本 | `scripts/agent-handoff.sh`；它按自己的位置找检出 |

把包装脚本放进 `~/bin` 这类 PATH 目录时，用 `AGENT_HANDOFF_HOME` 指向检出：

```bash
export AGENT_HANDOFF_HOME=/opt/agent-handoff-project   # 或 D:\tools\agent-handoff-project
```

## 仓库身份 vs 本机路径

提示词同时给出两样东西：

```text
接续 myproject。仓库 E:/output/myproject，分支 main，HEAD 1edd107840d5。
仓库身份（换机器时用这个定位，不要依赖上面的本机路径）：
  https://github.com/you/myproject.git @ 1edd107840d564691f92470e4d99e2b283f1a8f5
```

路径是「它在这台机器上的位置」，remote URL + 完整 sha 才是**身份**。
新会话在另一台机器、容器、WSL 或 Codespaces 里打开时，路径不存在；
而同一个 remote 下的两个工作副本（`proj-a5` 与 `proj-b8`）也只能靠路径区分。
没有远程时工具会明说「只存在于本机，接续必须在同一台机器上」。

**未推送的提交会被单独声明**，因为它们传不过去——新会话在别处 clone
只会拿到远程有的东西。判定用 `rev-list --count HEAD --not --remotes`
而不是 `@{u}..HEAD`：后者依赖远程跟踪引用是否新鲜，FETCH_HEAD 陈旧时会偏小
（实测某仓库 `ahead` 显示 0，而它的 HEAD 在任何远程上都不存在）。

## 并发写入保护

两个会话抢一个工作树是唯一真能丢工作的失败模式：我们的 `git add` 盖掉对方暂存的内容，或者对方的 `commit --amend` 重写我们刚做的提交。

**阻断信号**（默认停止，退出码 3）——只可能由另一个进程造成：

- 暂存区已有文件，而不是本次操作放进去的
- `index.lock` / `MERGE_HEAD` / `rebase-merge` / `CHERRY_PICK_HEAD` 存在

**提示信号**（继续，但记录进交接文件）：

- 两分钟内有 git 跟踪的文件被改动 —— 最常见的成因是你自己刚改完就来跑交接

分级的理由：把"刚改完"当阻断，会逼你每次都加 `--force`，而 `--force` 会连真正的阻断信号一起放过，反而更危险。

跑完还会再查一次：如果运行期间 HEAD 被别人的提交推进了，提示词里的 HEAD 已经过期，工具会明确警告。

## 提示词会过期

生成的提示词末尾带生成时间与对应的 HEAD。仓库此后又有提交时，重跑一次生成新的，不要复用旧的——旧提示词指向的 HEAD 可能已经不是你以为的那个状态。

## 相对原版的改动

功能与参数一个不少，退出码不变，环境变量（`PYTHONUTF8`、`PYTHONIOENCODING`）与全部中文注释保留。此外：

**跨平台**

- Linux / macOS / WSL 可用。原版的路径正则只认 `C:\...`，非 Windows 上永远推断不出仓库；`os.startfile` 只有 Windows 有
- venv 布局三种都认（`Scripts/python.exe`、`bin/python`、`bin/python3`）
- 含空格的解释器路径正确加引号——`C:\Program Files\...\python.exe` 在原版里会被 shell 拆成两个参数
- WSL 里会去 `/mnt/c/Users/*` 找宿主机的转录

**性能**

| 环节 | 原版 | 现在 |
|---|---|---|
| 符号检索 | 每符号一次 `rg` 进程（12 任务 × 6 符号 = 72 次） | 合成一条交替正则，1 次 |
| 转录扫描 | 每个文件读 3 遍 | 1 遍流式，深度信息拿全后走廉价分支 |
| 多个转录 | 串行 | 线程池并行 + mtime/size 缓存 |
| 找计划文档 | 每个 `.md` 读 60 KB | 体积初筛 + 分段读，只有候选付全额代价 |
| 测试命令 | 串行 | 互不依赖的命令并行 |

**修掉的缺陷**

- 计划文档的换行风格不再被破坏。原版读 CRLF、写 LF，回填一个复选框会让 git diff 显示整份文件都变了
- 受保护文件的排除改用 `:(exclude,literal)` 长写法。原版的 `:!path` 在旧 git 上不支持，且路径含 `[`、`*` 时会被当通配，排除失效——私有文件会被提交
- 空仓库不再被误判。原版的 `git()` 失败与空输出都返回空串，`rev-parse HEAD` 在没有提交的仓库里两种情况分不开
- 并发检测不再被构建产物、缓存和工具自己上一轮的产物误报
- `**Files:` 段的结束判断统一用去缩进后的行，缩进过的 `  **Constraints:**` 不再让后续文件被算进上一个任务
- 整个流程共用一个时间戳。原版调六次 `datetime.now()`，跨午夜运行时文件名日期与文档标题日期会不一致
- 测试摘要认 pytest / vitest / jest / cargo / go，并用上了原版收下却从没使用的 `name` 参数
- 子模块脏了会被报出来——父仓库的提交不带它，接续会话看到不一致的树
- stderr 也钉到 UTF-8。原版只处理 stdout，GBK 控制台下中文报错会触发 UnicodeEncodeError，错误信息把错误自己吃掉了
- 超时的命令保留已产出的部分输出，那部分往往正是失败原因

## 开发

```bash
pip install -e ".[dev]"
pytest              # 全部测试
ruff check src tests
python scripts/check_i18n.py   # 三语文案对齐校验
```

CI 在 Linux / Windows / macOS × Python 3.9–3.13 上跑。

## 许可

MIT

