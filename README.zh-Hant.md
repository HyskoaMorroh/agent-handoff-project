# agent-handoff

[简体中文](README.md) · **繁體中文** · [English](README.en.md)

工作階段卡死時，把進度從對話裡搬進儲存庫。

AI 編碼工作階段會因為上游 400、供應商熔斷、上下文超限而突然死掉。死掉的不是程式碼——程式碼在磁碟上——死掉的是**只存在於那段對話裡的東西**：目標、已經排除的方案、紅線約束、下一步該做什麼。新工作階段從零開始，於是重做已完成的工作，或者改掉你明確說過不能動的檔案。

這個工具在新工作階段開始前跑一次，把那些東西固化進儲存庫：

1. **提交快照** —— 自動排除計畫文件宣告為「使用者私有」的檔案
2. **回填計畫** —— 依客觀證據（檔案是否存在、符號是否真被定義）勾選核取方塊
3. **產生交接** —— 一份交接 Markdown + 一段可直接貼上的新工作階段開場提示詞

它**不硬編碼任何專案知識**：專案名、路徑、任務名、測試命令全部從儲存庫自身推斷。

<p align="center">
  <img src="docs/img/gui-light.png" alt="工作階段健檢介面（淺色）" width="820">
</p>
<details>
<summary>深色模式</summary>
<p align="center">
  <img src="docs/img/gui-dark.png" alt="工作階段健檢介面（深色）" width="820">
</p>
</details>

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

# 急著開新工作階段，略過測試
agent-handoff /path/to/project --skip-tests
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
| `--find KEYWORD` | 依工作階段 ID／目錄／開場提問關鍵字定位工作階段 |
| `--limit N` | 每個智慧代理最多掃描多少個最新記錄，預設 12 |
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

Task 1 的兩個檔案都存在、兩個符號都真的被 `def`／`class` 定義了，它才會把兩個 Step 勾上。檔案存在但符號沒定義 = 「部分完成」，不勾。

## 工作階段健檢的判據

體積分檔來自本機 54 個 Claude + 54 個 Codex 記錄的實測分佈，不是拍腦袋：

| 記錄體積 | 判定 | 實測 |
|---|---|---|
| ≥ 8 MB | **立即交接** | 該區間 100% 出現過致命錯誤 |
| ≥ 3 MB | 盡快交接 | 約三成出現過 |
| ≥ 1 MB | 留意 | 約一成七出現過 |
| < 1 MB | 健康 | 250 KB 以下無一出現過 |

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
