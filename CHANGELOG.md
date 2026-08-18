# Changelog

本文件记录对用户可见的变化。格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.0.0] - 2026-08-19

从单文件脚本重写为可安装的跨平台项目。**命令行参数、语义与退出码与 1.x 完全一致**，
环境变量（`PYTHONUTF8`、`PYTHONIOENCODING`）与全部原始注释保留。

### 新增

- **本地网页界面**（`agent-handoff --gui`）。浅色为默认，深色与跟随系统可选。
  只绑定 `127.0.0.1`，每个请求校验一次性令牌，并检查 Origin/Host 以挡 DNS rebinding——
  它能对任意路径执行 `git commit`，所以这些不是可选项。
- **三种语言**：简体中文、繁體中文、English。界面、命令行输出、**以及生成的交接文档
  与开场提示词**全部跟随语言。`--lang` 指定，或 `AGENT_HANDOFF_LANG` 环境变量，
  或跟随系统区域设置。
- **会话按 APP 分组**，组内最近活动在前。组的顺序也按该组最近活动排。
- **交互菜单支持语言切换**，选择会被记住（`~/.config/agent-handoff/lang`）。
- `--json` 输出结构化结果供脚本消费。
- `--jobs` 控制并行度，`--port` / `--no-browser` 控制网页界面。
- 单文件图文说明 `docs/guide.html`，三种语言内嵌、零外链、双击即可打开。
- POSIX 启动脚本 `scripts/agent-handoff.sh` 与 `scripts/run.sh`。
- 测试摘要新增识别 jest、cargo、go；原有 pytest 与 vitest 的识别更细
  （区分 failed / error / skipped，并去重失败标识符）。
- 报告脏子模块：父仓库的提交不带子模块内部改动，接续会话会看到不一致的树。

### 修复

- **受保护文件的排除不再静默失效。** 原先用 `:!path` 简写，它在旧版 git 上不被支持，
  且当路径含 `[`、`*` 时会被当作通配符——排除失败，用户私有文件被提交上去。
  改用 `:(exclude,literal)` 长写法。
- **计划文档的换行风格不再被破坏。** 原先读 CRLF、写 LF，回填一个复选框会让
  git diff 显示整份文件都变了，接续会话完全看不出真正的改动是什么。
- **空仓库不再被误判。** 原先 `git()` 对「命令失败」和「输出为空」都返回空串，
  没有提交的仓库里 `rev-parse HEAD` 两种情况分不开。
- **Linux / macOS 上能推断出仓库了。** 原先路径正则只匹配 `C:\...` 形态；
  预筛白名单也漏掉 `/tmp`、`/workspace`、`/nix` 等前缀，改成结构判断。
- **并发检测不再误报。** 构建产物、缓存、工具自己上一轮的输出、以及受保护文件
  的改动都不再被当成「另一个会话在写」。信号分成阻断与提示两级：
  「两分钟内有文件被改」最常见的成因是用户自己刚改完，当作阻断会逼人每次都加
  `--force`，而 `--force` 会连真正的阻断信号一起放过。
- **提示词的两句话之间不再粘连**（`1 left.Do not redo Task 1.`）。
- **`**Files:` 段的结束判断**改用去缩进后的行，缩进过的 `  **Constraints:**`
  不再让后续文件被算进上一个任务。
- **整个流程共用一个时间戳。** 原先调用六次 `datetime.now()`，跨午夜运行时
  文件名的日期与文档标题的日期会不一致。
- **stderr 也钉到 UTF-8。** 原先只处理 stdout，GBK 控制台下中文报错会触发
  `UnicodeEncodeError`——错误信息把错误本身吃掉了。
- 含空格的解释器路径正确加引号（`C:\Program Files\...\python.exe` 原先被
  shell 拆成两个参数）。
- venv 布局三种都认：`Scripts/python.exe`、`bin/python`、`bin/python3`。
- 超时的命令保留已产出的部分输出，那部分往往正是失败原因所在。
- `os.startfile` 只有 Windows 有；macOS 走 `open`，Linux 走
  `xdg-open` / `gio` / `wslview`，无桌面环境时退回 `webbrowser`。
- `summarize_test_output` 用上了原先收下却从未使用的 `name` 参数。
- `print_session_card` 里 `extra` 变量的遮蔽。

### 性能

| 环节 | 1.x | 2.0 |
|---|---|---|
| 符号检索 | 每符号一次 `rg` 进程（12 任务 × 6 符号 = 72 次） | 合成一条交替正则，1 次调用 |
| 转录扫描 | 每个文件读 3 遍 | 1 遍流式；深度信息拿全后走廉价分支 |
| 多个转录 | 串行 | 线程池并行 + mtime/size 缓存 |
| 定位计划文档 | 每个 `.md` 读 60 KB | 体积初筛 + 分段读，只有候选付全额代价 |
| 测试命令 | 串行 | 互不依赖的命令并行 |

### 工程

- 拆成可导入的包，CLI 与网页界面共用 `core/`，两个前端不可能行为漂移。
- 294 项测试，Windows 与 Linux（Python 3.9 / 3.11）均通过。
- CI 覆盖 Linux / Windows / macOS × Python 3.9–3.13，另有三语文案对齐校验、
  端到端验证、以及说明文档新鲜度检查。
- 零第三方运行时依赖：这个工具在环境已经出问题之后才被运行，任何需要
  `pip install` 的东西都是一条新的失败路径。

[2.0.0]: https://github.com/devin/agent-handoff/releases/tag/v2.0.0
