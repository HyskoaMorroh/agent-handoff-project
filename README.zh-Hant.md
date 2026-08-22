<h1 align="center">agent-handoff</h1>

<p align="center"><b>工作階段卡死時，把進度從對話裡搬進儲存庫</b></p>

<p align="center">
  <a href="CHANGELOG.md"><img alt="版本" src="https://img.shields.io/badge/version-2.3.0-1F6B4F?style=flat-square"></a>
  <a href="pyproject.toml"><img alt="Python" src="https://img.shields.io/badge/python-3.9%20%E2%80%93%203.13-2F5473?style=flat-square"></a>
  <a href="tests/"><img alt="測試" src="https://img.shields.io/badge/tests-392%20passed-1F6B4F?style=flat-square"></a>
  <a href="pyproject.toml"><img alt="執行時依賴" src="https://img.shields.io/badge/runtime%20deps-0-7C6210?style=flat-square"></a>
  <a href="LICENSE"><img alt="授權" src="https://img.shields.io/badge/license-MIT-6B7B7E?style=flat-square"></a>
</p>

<p align="center">
  <a href="README.md">简体中文</a> · 繁體中文 · <a href="README.en.md">English</a>
</p>

AI 編碼工作階段會因為上游 400、供應商熔斷、上下文超限而突然死掉。死掉的不是程式碼——程式碼在磁碟上——死掉的是**只存在於那段對話裡的東西**：目標、已經排除的方案、紅線約束、下一步該做什麼。新工作階段從零開始，於是重做已完成的工作，或者改掉你明確說過不能動的檔案。

> **什麼時候跑它**：當工作階段的上下文快滿了。工具直接讀記錄裡的 token 數
> （Claude 的 `message.usage`、Codex 的 `token_count` 事件），而不是猜——
> 已經壓縮過的工作階段一律按「滿過」對待，因為自動壓縮只在裝不下時才觸發。
> 詳見 [工作階段健檢的判據](#工作階段健檢的判據)。

## 它做四件事

| | 做什麼 | 憑什麼 |
|:--:|---|---|
| **1** | **提交快照** | 自動排除計畫文件宣告為「使用者私有」的檔案 |
| **2** | **回填計畫** | 依客觀證據勾選：檔案是否存在、符號是否**真被定義** |
| **3** | **傳承工作階段** | 勾選相關工作階段，把它們**自己寫下的**壓縮摘要與你的原話帶過去 |
| **4** | **產生交接** | 一份交接 Markdown + 一段可直接貼上的開場提示詞 |

它**不硬編碼任何專案知識**：專案名、路徑、任務名、測試命令全部從儲存庫自身推斷。

<p align="center">
  <img src="docs/img/gui-light.png" alt="工作階段健檢介面（淺色）" width="880">
  <br><sub>工作階段健檢 · 淺色</sub>
</p>

<details>
<summary><b>深色（跟隨系統）</b></summary>
<p align="center">
  <img src="docs/img/gui-dark.png" alt="工作階段健檢介面（深色）" width="880">
</p>
</details>

<details>
<summary><b>交接結果長什麼樣</b>（完成度判定 · 缺口 · 受保護檔案 · 開場提示詞）</summary>
<p align="center">
  <img src="docs/img/gui-result.png" alt="交接結果：完成度表格、缺口明細、受保護檔案與可複製的開場提示詞" width="880">
  <br><sub>注意 <code>Task 2</code>：檔案在、但它宣告的 <code>render_report</code> 沒定義 → 判「部分」，不勾。<br>
  這正是這個工具最該被看到的一條判據。</sub>
</p>
</details>

---

## 先讀這一節：什麼時候**不該**用它

同一個 APP、同一台機器、同一個供應商、上下文沒滿、工作階段檔案完好時——
**用原生續接，不要用這個工具**：

```bash
claude --resume          # 或 claude --continue / 工作階段內 /resume
codex resume             # 或 codex resume --last
```

原生續接恢復的是**完整對話歷史**（Claude Code 官方文件：「the full history,
including tool calls and results」），即原樣重放 token，**無損**。
本工具產出的是**有損摘要**，不可能等於無損重放。

它的價值只在原生續接**結構性做不到**的場景：

| 場景 | 為什麼原生做不到 |
|---|---|
| 上下文已耗盡 | 官方自己的解法也是壓縮摘要（閒置 1 小時 + 超 10 萬 token 時彈「Resume from summary」，等於跑 `/compact`） |
| 跨 APP（Claude Code ↔ Codex） | 兩邊格式與事件語義完全不同，**無任何官方匯入機制** |
| 換模型 / 換供應商 | 文件明列：模型在已退役、被 `--model` 覆蓋、或 Bedrock 等部署 ID 供應商上**不恢復** |
| 工作階段檔案損壞 | `Failed to resume the conversation`，結束碼 1 |
| 要主動丟棄被汙染的歷史 | 原生只能全帶或摘要；`/branch` 是複製而非裁剪 |
| 跨機器 | 記錄可以拷，但官方按 ID 查找只在「恰好一個專案持有該 ID」時才解析，手工副本會被判 not-found |
| plan mode / bypassPermissions / 背景工作 / MCP / CLI 啟動參數 | 文件明列**永不恢復**——寫進交接文件反而更可靠 |

### 哪些東西原理上傳不過去

工具會把這句話寫進產生的提示詞，免得接續工作階段把「摘要裡沒有」當成「沒發生過」：

- 模型內部推理狀態與 prompt 快取（`encrypted_content` 是伺服器端加密串，換工作階段即失效）
- 工具授權執行時狀態——新工作階段會重新彈權限確認
- MCP 連線與認證權杖（記錄只記工具**名字**，不記連線）
- 背景行程、監聽埠、已啟動的服務
- 被否決方案的推理過程——thinking 無簽章不可回放，且多在子代理記錄裡
  （實測子代理「過程:報告」= **124:1**，14.6 MB 過程 vs 118 KB 報告）

---

## 安裝

需要 Python 3.9+ 與 git。沒有第三方執行時依賴——這個工具在環境已經出問題之後才被執行，任何需要 `pip install` 的東西都是一條新的失敗路徑。

```bash
pip install -e .
```

或者不安裝，直接跑：

```bash
# Windows
scripts\agent-handoff.cmd .
# Linux / macOS / WSL
./scripts/agent-handoff.sh .
```

## 使用

### 網頁介面（推薦）

```bash
agent-handoff --gui
```

瀏覽器自動開啟。三種語言隨時切換，淺色／深色／跟隨系統。服務只繫結 `127.0.0.1`，並用一次性權杖驗證每個請求——它能對任意路徑跑 `git commit`，所以這不是可選項。

### 互動選單（不想記參數）

```bash
python -m agent_handoff.menu
# 或者 Windows 上雙擊 scripts\雙擊執行.cmd，Linux 上 ./scripts/run.sh
```

### 命令列

```bash
# 先看哪個工作階段該交接了（只讀，不動任何儲存庫）
agent-handoff --vitals

# 螢幕截圖對不上是哪段對話？依 ID 前幾位／目錄名／提問關鍵字尋找
agent-handoff --find 01a00e83
agent-handoff --find 工作流

# 預演：只顯示將要做什麼
agent-handoff /path/to/project --dry-run

# 真的執行
agent-handoff /path/to/project

# 勾選要傳承的工作階段：列出本機工作階段後按編號選
agent-handoff /path/to/project --pick-sessions

# 急著開新工作階段，略過測試
agent-handoff /path/to/project --skip-tests

# 記錄佔了多少磁碟？哪些可以安全丟掉？（只讀中介資料，毫秒級；不刪任何檔案）
agent-handoff --sweep
agent-handoff --sweep --by-repo          # 額外依儲存庫聚合（要讀記錄，慢很多）
agent-handoff --sweep --out disk.md      # 匯出 md 報告
```

### 全部參數

| 參數 | 作用 |
|---|---|
| `repo` | 儲存庫路徑，預設目前目錄 |
| `--plan PATH` | 計畫文件路徑；省略則自動探測最新的含核取方塊任務文件 |
| `--out PATH` | 交接檔案輸出路徑；預設與計畫文件同目錄 |
| `-m, --message MSG` | 提交訊息；省略則自動產生 |
| `--no-commit` | 不提交，只分析並產生交接檔案 |
| `--skip-tests` | 不跑測試（快速模式） |
| `--test-timeout N` | 單條測試命令逾時秒數，預設 900 |
| `--vitals` | 只健檢本機工作階段記錄並結束，不動儲存庫 |
| `--no-vitals` | 略過工作階段健檢（交接檔案裡不含體徵表） |
| `--sweep` | 報告記錄佔了多少磁碟、哪些可以安全回收，然後結束；**不刪任何檔案** |
| `--by-repo` | 配合 `--sweep`：額外依儲存庫聚合佔用（要讀記錄，慢很多） |
| `--find KEYWORD` | 依工作階段 ID／目錄／話題關鍵字定位工作階段 |
| `--limit N` | 每個智慧代理最多掃描多少個最新記錄，預設 12 |
| `--pick-sessions` | 互動勾選要傳承的工作階段（列出後按編號選擇） |
| `--sessions PATH` | 直接指定要傳承的記錄；可重複，或用逗號分隔 |
| `--force` | 忽略並行寫入警告強行繼續 |
| `--dry-run` | 全程只印出將要做什麼，不寫任何檔案 |
| `--lang {zh-Hans,zh-Hant,en}` | 介面與產出語言；省略則依系統地區設定 |
| `--gui` | 啟動本機網頁介面 |
| `--port N` | 網頁介面通訊埠；0 自動挑空閒通訊埠 |
| `--no-browser` | 啟動網頁介面但不自動開啟瀏覽器 |
| `--jobs N` | 並行度；0 依 CPU 核心數自動決定 |
| `--json` | 以 JSON 輸出結果，供腳本取用 |

結束碼：`0` 成功 · `1` 未找到符合的工作階段 · `2` 參數或環境錯誤 · `3` 偵測到並行寫入而停止。

環境變數 `AGENT_HANDOFF_LANG` 優先於系統地區設定，方便在中文系統上產出英文交接文件。

## 它怎麼知道你的專案

| 資訊來源 | 推斷出什麼 |
|---|---|
| git 中介資料 | 分支／HEAD／未提交變更／領先遠端多少提交 |
| `pyproject.toml`、`package.json`、`Cargo.toml`、`go.mod` | 技術棧與測試命令 |
| 計畫文件 `**Files:**` 段 | 每個任務應產出哪些檔案 |
| 計畫文件 `**Interfaces:**` 段 | 每個任務應產出哪些符號 |
| 計畫文件 約束段 | 哪些檔案不得提交 |
| 計畫文件 Goal / Constraints 段 | 提示詞裡要點名讓新工作階段先讀的段落 |

計畫文件格式（自動探測最新的含核取方塊任務文件）：

```markdown
**Goal:** 一句話說清這個專案要做什麼。

## Global Constraints
- `docs/LOGO.jpg` 是使用者私有檔案，不得提交。

### Task 1: 建立資料層

**Files:**
- Create: `src/db/schema.py`
- Modify: `src/config.py`

**Interfaces:**
- Produces: `create_schema`, `Migration`

- [ ] **Step 1** 定義表結構
- [ ] **Step 2** 寫遷移腳本
```

Task 1 的兩個檔案都存在、兩個符號都真的被定義了，它才會把兩個 Step 勾上。檔案存在但符號沒定義 = 「部分完成」，不勾。

解析器認得真實 markdown 的寫法差異，因為漏認不會報錯、只會靜默失真：
動詞可以加粗（`- **Modify**: x`）、冒號可省（`- Modify x`）、動詞可省
（`- \`x\` — 說明`）、一行可以列多個路徑、列表符 `-` `*` `+` 都算、
標題 1–6 級且允許縮排、`Task` 與 `Phase`/`階段` 都認、
`Produces` 之外還認 `Exports`/`Provides`/`提供`/`匯出`。

### 符號是怎麼判定「真的被定義」的

不是「檔案裡出現過這個詞」。三類定義形態都算，且**引用不算**：

```ts
interface Intent {
  undo: () => void          // ✅ interface 成員
}
const intent = {
  undo: () => { step() }    // ✅ 物件字面量屬性
}
class M {
  performAction(a: () => void) {   // ✅ 方法簡寫，無關鍵字前綴
  }
}

intent.undo()               // ❌ 呼叫不是定義
// interface undo 由 store 提供   ❌ 註解裡的引用不是定義
```

後兩種寫法在 TS / Vue / 現代 JS 裡才是主流，而它們**一個關鍵字前綴都沒有**。
只認「關鍵字 + 空格 + 名字」的偵測器會把一個寫滿 `undo: () => void` 的儲存庫
判成「符號全缺」，於是接續工作階段重做已經做完的工作——這是本工具遇到過的
真實誤判。判定前會先把註解挖空（用等長空白替換以保留行列位置），
因為**假陽性比假陰性更危險**：它會讓回填勾掉從未實作的步驟，待辦永久消失。

計畫文件自身被排除在符號檢索之外。否則文件裡寫的
``- Produces `undo` `` 會被搜到，變成「已實作」的證據，自己滿足自己。

檢索失敗（三條後端全掛）與「查過、確實沒有」是兩回事：前者不允許判「完成」，
因為勾選會寫進計畫文件且不可逆。

## 工作階段健檢的判據

**判據是上下文佔用，不是檔案體積。** 佔用數就寫在記錄裡，不需要猜：

| 來源 | 佔用 | 上限 |
|---|---|---|
| Claude Code | `message.usage` 的 `input_tokens` + 兩種 cache 讀入 | 記錄裡**沒有**，按絕對閾值判 |
| Codex | `payload.info.last_token_usage.input_tokens` | `payload.info.model_context_window`（實測 121600） |

有上限就算真佔用率（≥90% 立即交接，≥75% 盡快，≥55% 留意）；
沒有上限就拿佔用量對照按 200k 視窗折算的閾值——對更大的視窗偏保守，
寧可早提醒也不要漏掉真要滿的。

**壓縮過就按「滿過」對待。** 自動壓縮只在上下文裝不下時才觸發，
所以它是最硬的證據，比任何百分比都清楚。壓縮一次即「盡快交接」，
兩次以上「立即交接」——每次壓縮都在丟更早的歷史。

| 來源 | 壓縮事件 |
|---|---|
| Claude Code | `type:"system"` + `subtype:"compact_boundary"`，帶 `compactMetadata.preTokens` |
| Codex | `type:"compacted"` 記錄 |

### 為什麼不用體積

體積與真實佔用嚴重脫鉤。本機實測四個記錄：

| 記錄 | 體積 | 實際佔用 | 壓縮 | 按體積會判 | 實際該判 |
|---|---|---|---|---|---|
| A | 1.0 MB | 194,183 token | 0 | 健康 | **立即交接** |
| B | 2.0 MB | 172,490 token | **10 次** | 留意 | **立即交接** |
| C | 27.4 MB | 710,340 token | 0 | 立即交接 | 立即交接 |
| D（Codex） | 6.4 MB | 121,407 / 121,600 = **99.8%** | 1 次 | 盡快交接 | **立即交接** |

B 最能說明問題：**它體積小恰恰因為壓縮一直在丟歷史**。越小可能丟得越多，
而體積判據會把這種最該交接的工作階段標成「留意」。

體積仍然保留為**墊底**：記錄裡讀不到 token 數時（舊格式、被截斷的檔案），
它是唯一可用的訊號。下面這張圖是那套墊底閾值的實測依據：

<p align="center">
  <img src="docs/img/bands.svg" alt="記錄體積與實測致命錯誤率：1 MB 以下 0%、1 MB 起 17%、3 MB 起 30%、8 MB 起 100%" width="880">
</p>

**致命錯誤**指真的殺死過工作階段的簽章（`content-blocked`、供應商熔斷、
無可用渠道、圖片尺寸超限），且必須出現在**錯誤載荷欄位**裡。
在整行原文上裸比對會把「討論這些詞」當成「發生了這些事」——實測 14 個主記錄的
239 個裸比對命中裡，94 個來自 assistant 正文、83 個來自 user 正文
（你自己的 CLAUDE.md 裡寫著「出現 content-blocked、熔斷時…」也會被算進去）。

**中斷輪次**（`turn_aborted`）單獨計數：實測 Codex 側 `is_error` 在 40 個
rollout 裡只有 3 次，而 `turn_aborted` 有 6 次。把半成品當成已完成，
是交接裡最貴的誤判。

## 工作階段內容是怎麼傳承的

勾選工作階段後，工具從記錄裡提取**工作階段自己寫下的記錄**，而不是靠猜：

| 來源 | 內容 | 為什麼要它 |
|---|---|---|
| Codex `compacted` 事件 | 模型在上一次壓縮時寫的交接摘要，**全部視窗** | 含儲存庫路徑、真實 HEAD、已讀文件、任務範圍 |
| `compacted.replacement_history` | 被摘要替換掉的**使用者原話**，逐字 | 摘要是轉述，轉述會丟措辭裡的約束 |
| Claude `ai-title` | 一句話話題摘要 | 人認工作階段靠這個，不是靠 ID 前八位 |
| Claude `last-prompt` | 最後一次使用者輸入 | 說明工作階段停在哪 |

**壓縮摘要必須保留每一個視窗。** 實測本機 70 個帶壓縮的 rollout（52 個是
多視窗，最多 19 個視窗）：`window_number` 遞增、`previous_window_id` 串成鏈，
每個視窗只總結它自己那一段，**沒有任何樣本**的末視窗逐字包含首視窗。
只留最後一個會丟掉中位 **78%**、p90 96% 的具體事實（commit sha、檔案路徑、
測試計數），其中 11 個 rollout 的「使用者目標 / 紅線約束」只出現在早期視窗——
恰恰是最不能丟的部分。保留全部視窗後實測丟失率 **0%**（70/70）。

摘要也不截斷：實測 62/70 個末視窗超過 4000 字元，中位會被切掉 2925 字元、
最多 13241 字元，而切掉的往往正是結論與待辦。完整摘要寫進交接**文件**
（檔案多幾十 KB 無所謂），提示詞裡只放話題與路徑。

## 記錄存在哪裡，佔了多少

**記錄不會因為專案在 D/E/F 磁碟而跟著搬家。** 實測本機 E 磁碟上的專案，記錄仍在
C 磁碟家目錄下——磁碟機代號只出現在記錄內容裡寫的 `cwd`，不影響儲存位置：

| 應用 | 預設位置 | 環境變數改位置 |
|---|---|---|
| Claude Code | `~/.claude/projects/<cwd 的 slug>/*.jsonl` | `CLAUDE_CONFIG_DIR` |
| Codex | `~/.codex/sessions/年/月/日/rollout-*.jsonl` | `CODEX_HOME` |
| Codex（歸檔） | `~/.codex/archived_sessions/rollout-*.jsonl` | 同上 |

會變的是**資料目錄本身**：兩個應用都支援用環境變數整體挪走（筆電系統磁碟小、
指向第二顆磁碟是常見做法）。工具優先讀這兩個變數，家目錄仍作墊底——兩處都可能有
歷史記錄，只認一邊就是丟工作階段。WSL 裡還會去 `/mnt/c/Users/<name>` 找主機的記錄。

`--sweep` 報告佔用，**不刪任何檔案**：

```bash
agent-handoff --sweep
```

它只讀檔案中介資料、不讀內容，這是它快的唯一原因。實測本機 423 個記錄 1.09 GB：

| 做法 | 耗時 |
|---|---|
| 只遍歷目錄 | 8 ms |
| 遍歷 + `stat`（`--sweep` 用這個） | **10 ms** |
| 讀每個檔案前 64 KB | 219 ms（慢 22 倍） |

**依體積排行，不依時間過期**——實測本機「超過 30 天」是 0 個，而單個 90 MB
的檔案佔了總量 8%。真正吃磁碟的永遠是少數幾個檔案。

報三類可安全回收的，各自附上「為什麼安全」：

| 分類 | 為什麼可以回收 |
|---|---|
| 子代理記錄 | 子代理自己的工作日誌，不是你參與過的對話——它本來就無法續接 |
| 已歸檔工作階段 | 已在 Codex 裡歸檔，續接不了；但正文還在 |
| 空工作階段 | 不到 32 KB，裝不下一輪有內容的對話 |

三類互斥計數：一個既小又屬於子代理的記錄只算一次，不會讓「可回收」被重複累加。

加 `--by-repo` 依儲存庫聚合（要讀記錄才能拿到 cwd，實測 24 秒 vs 12 毫秒，
所以是獨立開關）。它給出的資訊很直接——本機 E 磁碟那兩個 kirara-ai 專案
佔了 828 MB，是總量的 76%。

> **為什麼不自動刪。** 記錄裡可能存著那段工作的唯一記錄，而刪除無法復原。
> 判斷哪些真能丟需要人看一眼，所以工具只給可審閱的清單，由你決定。
> 這與它其餘部分的立場一致：只給證據，不做無法復原的動作。

## 換電腦

記錄是普通檔案，拷到新機器就能讀——工具會認出它們不是本機產生的，據此調整。

**認外來靠結構，不靠猜使用者名稱。** 硬編碼名單換台機器就失效，所以判據是
路徑裡的家目錄段形狀：`<磁碟機代號>:\Users\<名字>`、`/home/<名字>`、`/Users/<名字>`、
`/mnt/<磁碟>/Users/<名字>`。其中的名字不是本機任何一個（`~` 的目錄名，加上
`USERNAME` / `USER` / `LOGNAME`——WSL 下兩側名字可能不同，都算本機）就是外來。

認出外來後有三處不同：

| | 本機工作階段 | 外來工作階段 |
|---|---|---|
| 上下文判定 | 正常 | **一樣正常**（token 數與機器無關） |
| 原生續接命令 | 給出 | **不給**——應用只索引自己資料目錄裡的工作階段，命令一定報「找不到」 |
| 路徑 | 直接可用 | 明確標註「這是那台機器上的路徑」，不讓你照著去找檔案 |
| 內容傳承 | 可勾選 | **一樣可勾選**——那正是換機器時最想帶走的東西 |

**別人的使用者名稱也會脫敏。** 交接檔案裡 `D:\Users\bob\myproj` 變成
`D:\Users\_USER_\myproj`，只換名字那一段、保留其餘路徑（你仍要靠它判斷那台機器上
的專案在哪）。Claude 的 slug 目錄名 `D--Users-bob-myproj` 同樣處理——使用者名稱嵌在
`-` 分隔的一段裡，漏掉它就會從記錄路徑漏出去。

**要在新機器上接著做事，看提示詞裡的儲存庫身分**，那部分本來就是可攜的：

```
儲存庫身分（換機器時用這個定位，不要依賴上面的本機路徑）：
  https://github.com/you/proj.git @ eb8a6aa2217a
```

沒有遠端時它會明說「這個儲存庫只存在於本機，接續必須在同一台機器上進行」，
並給出未推送的提交數——別以為在新機器上 clone 就能拿到那些變更。

### 怎麼把工具和工作階段都搬過去

**沒有任何一處寫死使用者名稱或磁碟機代號。** 執行時每個位置都是當場算出來的：
家目錄來自 `expanduser("~")`，檢出根來自啟動腳本自己的位置，工作階段目錄來自
`CODEX_HOME` / `CLAUDE_CONFIG_DIR` 或家目錄。整個目錄拷到另一台電腦
（使用者名稱不同、磁碟機代號不同）直接可用，不改一行設定。

把別的電腦的 `.codex` / `.claude` 拷過來後，用環境變數指過去——路徑可以在
任何磁碟機、任何目錄名下：

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

指過去之後本機家目錄**仍然會掃**，兩邊的工作階段一起列出、一起可勾選——遷移是
「多一處」不是「換一個」，否則搬完就看不到本機歷史了。

工具自己怎麼搬，三選一，都不用改檔案：

| 做法 | 命令 |
|---|---|
| 裝成命令（推薦） | `pip install -e .`，之後 `agent-handoff` 在任何目錄可用 |
| 直接跑檢出 | `python -m agent_handoff.cli`（在檢出根，或先設 `PYTHONPATH=<檢出>/src`） |
| 用啟動腳本 | `scripts/agent-handoff.sh`；它按自己的位置找檢出 |

把包裝腳本放進 `~/bin` 這類 PATH 目錄時，用 `AGENT_HANDOFF_HOME` 指向檢出：

```bash
export AGENT_HANDOFF_HOME=/opt/agent-handoff-project   # 或 D:\tools\agent-handoff-project
```

## 儲存庫身分 vs 本機路徑

提示詞同時給出兩樣東西：

```text
接續 myproject。儲存庫 E:/output/myproject，分支 main，HEAD 1edd107840d5。
儲存庫身分（換機器時用這個定位，不要依賴上面的本機路徑）：
  https://github.com/you/myproject.git @ 1edd107840d564691f92470e4d99e2b283f1a8f5
```

路徑是「它在這台機器上的位置」，remote URL + 完整 sha 才是**身分**。
新工作階段在另一台機器、容器、WSL 或 Codespaces 裡打開時，路徑不存在；
而同一個 remote 下的兩個工作副本（`proj-a5` 與 `proj-b8`）也只能靠路徑區分。
沒有遠端時工具會明說「只存在於本機，接續必須在同一台機器上」。

**未推送的提交會被單獨聲明**，因為它們傳不過去——新工作階段在別處 clone
只會拿到遠端有的東西。判定用 `rev-list --count HEAD --not --remotes`
而不是 `@{u}..HEAD`：後者依賴遠端追蹤引用是否新鮮，FETCH_HEAD 陳舊時會偏小
（實測某儲存庫 `ahead` 顯示 0，而它的 HEAD 在任何遠端上都不存在）。

## 並行寫入保護

兩個工作階段搶一個工作樹是唯一真能遺失工作的失敗模式：我們的 `git add` 覆蓋對方暫存的內容，或者對方的 `commit --amend` 重寫我們剛做的提交。

**阻斷訊號**（預設停止，結束碼 3）——只可能由另一個行程造成：

- 暫存區已有檔案，而不是本次操作放進去的
- `index.lock`／`MERGE_HEAD`／`rebase-merge`／`CHERRY_PICK_HEAD` 存在

**提示訊號**（繼續，但記錄進交接檔案）：

- 兩分鐘內有 git 追蹤的檔案被變更 —— 最常見的成因是你自己剛改完就來跑交接

分級的理由：把「剛改完」當阻斷，會逼你每次都加 `--force`，而 `--force` 會連真正的阻斷訊號一起放過，反而更危險。

跑完還會再查一次：如果執行期間 HEAD 被別人的提交推進了，提示詞裡的 HEAD 已經過期，工具會明確警告。

## 提示詞會過期

產生的提示詞末尾帶產生時間與對應的 HEAD。儲存庫此後又有提交時，重跑一次產生新的，不要重複使用舊的——舊提示詞指向的 HEAD 可能已經不是你以為的那個狀態。

## 相對原版的變更

功能與參數一個不少，結束碼不變，環境變數（`PYTHONUTF8`、`PYTHONIOENCODING`）與全部中文註解保留。此外：

**跨平台**

- Linux／macOS／WSL 可用。原版的路徑正規表示式只認 `C:\...`，非 Windows 上永遠推斷不出儲存庫；`os.startfile` 只有 Windows 有
- venv 佈局三種都認（`Scripts/python.exe`、`bin/python`、`bin/python3`）
- 含空格的解譯器路徑正確加引號——`C:\Program Files\...\python.exe` 在原版裡會被 shell 拆成兩個參數
- WSL 裡會去 `/mnt/c/Users/*` 找宿主機的記錄

**效能**

| 環節 | 原版 | 現在 |
|---|---|---|
| 符號檢索 | 每符號一次 `rg` 行程（12 任務 × 6 符號 = 72 次） | 合成一條交替正規表示式，1 次 |
| 記錄掃描 | 每個檔案讀 3 遍 | 1 遍串流，深度資訊拿全後走廉價分支 |
| 多個記錄 | 串列 | 執行緒池並行 + mtime／size 快取 |
| 找計畫文件 | 每個 `.md` 讀 60 KB | 體積初篩 + 分段讀，只有候選付全額代價 |
| 測試命令 | 串列 | 互不依賴的命令並行 |

**修掉的缺陷**

- 計畫文件的換行風格不再被破壞。原版讀 CRLF、寫 LF，回填一個核取方塊會讓 git diff 顯示整份檔案都變了
- 受保護檔案的排除改用 `:(exclude,literal)` 長寫法。原版的 `:!path` 在舊 git 上不支援，且路徑含 `[`、`*` 時會被當通用字元，排除失效——私有檔案會被提交
- 空儲存庫不再被誤判。原版的 `git()` 失敗與空輸出都回傳空字串，`rev-parse HEAD` 在沒有提交的儲存庫裡兩種情況分不開
- 並行偵測不再被建置產物、快取和工具自己上一輪的產物誤報
- `**Files:` 段的結束判斷統一用去縮排後的行，縮排過的 `  **Constraints:**` 不再讓後續檔案被算進上一個任務
- 整個流程共用一個時間戳記。原版呼叫六次 `datetime.now()`，跨午夜執行時檔名日期與文件標題日期會不一致
- 測試摘要認 pytest／vitest／jest／cargo／go，並用上了原版收下卻從沒使用的 `name` 參數
- 子模組髒了會被報出來——父儲存庫的提交不帶它，接續工作階段看到不一致的樹
- stderr 也釘到 UTF-8。原版只處理 stdout，GBK 主控台下中文報錯會觸發 UnicodeEncodeError，錯誤訊息把錯誤自己吃掉了
- 逾時的命令保留已產出的部分輸出，那部分往往正是失敗原因

## 開發

```bash
pip install -e ".[dev]"
pytest              # 全部測試
ruff check src tests
python scripts/check_i18n.py   # 三語文案對齊驗證
```

CI 在 Linux／Windows／macOS × Python 3.9–3.13 上跑。

## 授權

MIT
