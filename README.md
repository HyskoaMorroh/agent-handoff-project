<h1 align="center">agent-handoff</h1>

<p align="center"><b>会话卡死时，把进度从对话里搬进仓库</b></p>

<p align="center">
  <a href="CHANGELOG.md"><img alt="版本" src="https://img.shields.io/badge/version-2.3.0-1F6B4F?style=flat-square"></a>
  <a href="pyproject.toml"><img alt="Python" src="https://img.shields.io/badge/python-3.9%20%E2%80%93%203.13-2F5473?style=flat-square"></a>
  <a href="tests/"><img alt="测试" src="https://img.shields.io/badge/tests-392%20passed-1F6B4F?style=flat-square"></a>
  <a href="pyproject.toml"><img alt="运行时依赖" src="https://img.shields.io/badge/runtime%20deps-0-7C6210?style=flat-square"></a>
  <a href="LICENSE"><img alt="许可" src="https://img.shields.io/badge/license-MIT-6B7B7E?style=flat-square"></a>
</p>

<p align="center">
  简体中文 · <a href="README.zh-Hant.md">繁體中文</a> · <a href="README.en.md">English</a>
</p>

AI 编码会话会因为上游 400、供应商熔断、上下文超限而突然死掉。死掉的不是代码——代码在磁盘上——死掉的是**只存在于那段对话里的东西**：目标、已经排除的方案、红线约束、下一步该做什么。新会话从零开始，于是重做已完成的工作，或者改掉你明确说过不能碰的文件。

<p align="center">
  <img src="docs/img/bands.svg" alt="转录体积与实测致命错误率：1 MB 以下 0%，1 MB 起 17%，3 MB 起 30%，8 MB 起 100%" width="880">
</p>

> **什么时候跑它**：上面这张图就是判据。体积越大，会话越可能已经出过致命错误——
> 8 MB 以上的转录，本机 108 份样本里**无一例外**都撞过。

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

**🖥 网页界面**（推荐）——最好看，三语随时切换，浅色/深色/跟随系统。

```bash
agent-handoff --gui
```

只绑定 `127.0.0.1`，每个请求校验一次性令牌。它能对任意路径跑 `git commit`，所以这不是可选项。

**📋 交互菜单**——不用记参数。把项目文件夹拖进窗口就能填路径。

```bash
python -m agent_handoff.menu
# Windows 双击 scripts\双击运行.cmd ；Linux / macOS 跑 ./scripts/run.sh
```

**⌨ 命令行**——适合写进脚本。加 `--json` 让输出被程序消费，退出码有语义（见下）。

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
```

### 全部参数

| 参数 | 作用 |
|---|---|
| `repo` | 仓库路径，默认当前目录 |
| `--plan PATH` | 计划文档路径；省略则自动探测最新的含复选框任务文档 |
| `--out PATH` | 交接文件输出路径；默认与计划文档同目录 |
| `-m, --message MSG` | 提交信息；省略则自动生成 |
| `--no-commit` | 不提交，只分析并生成交接文件 |
| `--skip-tests` | 不跑测试（快速模式） |
| `--test-timeout N` | 单条测试命令超时秒数，默认 900 |
| `--vitals` | 只体检本机会话转录并退出，不碰仓库 |
| `--no-vitals` | 跳过会话体检（交接文件里不含体征表） |
| `--find KEYWORD` | 按会话 ID / 目录 / 话题关键词定位会话 |
| `--limit N` | 每个智能体最多扫描多少个最新转录，默认 12 |
| `--pick-sessions` | 交互勾选要传承的会话（列出后按编号选择） |
| `--sessions PATH` | 直接指定要传承的转录；可重复，或用逗号分隔 |
| `--force` | 忽略并发写入警告强行继续 |
| `--dry-run` | 全程只打印将要做什么，不写任何文件 |
| `--lang {zh-Hans,zh-Hant,en}` | 界面与产出语言；省略则按系统区域设置 |
| `--gui` | 启动本地网页界面 |
| `--port N` | 网页界面端口；0 自动挑空闲端口 |
| `--no-browser` | 启动网页界面但不自动打开浏览器 |
| `--jobs N` | 并行度；0 按 CPU 核数自动决定 |
| `--json` | 以 JSON 输出结果，供脚本消费 |

退出码：`0` 成功 · `1` 未找到匹配的会话 · `2` 参数或环境错误 · `3` 检测到并发写入而停止。

环境变量 `AGENT_HANDOFF_LANG` 优先于系统区域设置，方便在中文系统上产出英文交接文档。

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

体积分档来自本机 54 个 Claude + 54 个 Codex 转录的实测分布，不是拍脑袋：

| 转录体积 | 判定 | 实测 |
|---|---|---|
| ≥ 8 MB | **立刻交接** | 该区间 100% 出现过致命错误 |
| ≥ 3 MB | 尽快交接 | 约三成出现过 |
| ≥ 1 MB | 留意 | 约一成七出现过 |
| < 1 MB | 健康 | 250 KB 以下无一出现过 |

体积只回答「还能撑多久」。已经出过致命错误或被打断的会话会被往上抬一档——
一个 0.9 MB 但真撞过熔断的会话不该显示「健康」。

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

