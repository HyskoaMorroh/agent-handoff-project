# agent-handoff

**简体中文** · [繁體中文](README.zh-Hant.md) · [English](README.en.md)

会话卡死时，把进度从对话里搬进仓库。

AI 编码会话会因为上游 400、供应商熔断、上下文超限而突然死掉。死掉的不是代码——代码在磁盘上——死掉的是**只存在于那段对话里的东西**：目标、已经排除的方案、红线约束、下一步该做什么。新会话从零开始，于是重做已完成的工作，或者改掉你明确说过不能碰的文件。

这个工具在新会话开始前跑一次，把那些东西固化进仓库：

1. **提交快照** —— 自动排除计划文档声明为"用户私有"的文件
2. **回填计划** —— 按客观证据（文件是否存在、符号是否真被定义）勾选复选框
3. **生成交接** —— 一份交接 Markdown + 一段可直接粘贴的新会话开场提示词

它**不硬编码任何项目知识**：项目名、路径、任务名、测试命令全部从仓库自身推断。

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

### 网页界面（推荐）

```bash
agent-handoff --gui
```

浏览器自动打开。三种语言随时切换，浅色/深色/跟随系统。服务只绑定 `127.0.0.1`，并用一次性令牌校验每个请求——它能对任意路径跑 `git commit`，所以这不是可选项。

### 交互菜单（不想记参数）

```bash
python -m agent_handoff.menu
# 或者 Windows 上双击 scripts\双击运行.cmd，Linux 上 ./scripts/run.sh
```

### 命令行

```bash
# 先看哪个会话该交接了（只读，不碰任何仓库）
agent-handoff --vitals

# 截图对不上是哪段对话？按 ID 前几位 / 目录名 / 提问关键词找
agent-handoff --find 01a00e83
agent-handoff --find 工作流

# 预演：只显示将要做什么
agent-handoff /path/to/project --dry-run

# 真的执行
agent-handoff /path/to/project

# 急着开新会话，跳过测试
agent-handoff /path/to/project --skip-tests
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
| `--find KEYWORD` | 按会话 ID / 目录 / 开场提问关键词定位会话 |
| `--limit N` | 每个智能体最多扫描多少个最新转录，默认 12 |
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

Task 1 的两个文件都存在、两个符号都真的被 `def`/`class` 定义了，它才会把两个 Step 勾上。文件存在但符号没定义 = "部分完成"，不勾。

## 会话体检的判据

体积分档来自本机 54 个 Claude + 54 个 Codex 转录的实测分布，不是拍脑袋：

| 转录体积 | 判定 | 实测 |
|---|---|---|
| ≥ 8 MB | **立刻交接** | 该区间 100% 出现过致命错误 |
| ≥ 3 MB | 尽快交接 | 约三成出现过 |
| ≥ 1 MB | 留意 | 约一成七出现过 |
| < 1 MB | 健康 | 250 KB 以下无一出现过 |

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

