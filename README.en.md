# agent-handoff

[简体中文](README.md) · [繁體中文](README.zh-Hant.md) · **English**

When a session dies, move the progress out of the chat and into the repository.

AI coding sessions die suddenly — an upstream 400, a provider cutting you off, a context limit. What dies is not the code; the code is on disk. What dies is **everything that only existed in that conversation**: the goal, the approaches already ruled out, the hard limits, what to do next. A fresh session starts from nothing, so it redoes finished work, or edits the very file you said never to touch.

Run this once before starting the new session and it freezes those things into the repository:

1. **Commit a snapshot** — automatically excluding files the plan declares user-owned
2. **Backfill the plan** — tick checkboxes from objective evidence (files exist, symbols actually defined)
3. **Write the handoff** — a handoff Markdown plus an opening prompt you paste straight into the new session

It **hardcodes no project knowledge**: project name, paths, task names, and test commands are all inferred from the repository itself.

<p align="center">
  <img src="docs/img/gui-light.png" alt="Session vitals (light)" width="820">
</p>
<details>
<summary>Dark mode</summary>
<p align="center">
  <img src="docs/img/gui-dark.png" alt="Session vitals (dark)" width="820">
</p>
</details>

---

## Install

Needs Python 3.9+ and git. No third-party runtime dependencies — this tool runs *after* the environment has already gone wrong, so anything requiring `pip install` is one more way to fail.

```bash
pip install -e .
```

Or skip installing and run it directly:

```bash
# Windows
scripts\agent-handoff.cmd .
# Linux / macOS / WSL
./scripts/agent-handoff.sh .
```

## Use

### Web interface (recommended)

```bash
agent-handoff --gui
```

Your browser opens automatically. Switch between three languages at any time; light, dark, or follow the system. The server binds `127.0.0.1` only and validates every request with a one-shot token — it can run `git commit` against an arbitrary path, so that is not optional.

### Interactive menu (no flags to remember)

```bash
python -m agent_handoff.menu
# or double-click scripts\双击运行.cmd on Windows, ./scripts/run.sh on Linux
```

### Command line

```bash
# Which session needs a handoff? (read-only, touches no repository)
agent-handoff --vitals

# Cannot tell which conversation a screenshot came from?
# Search by the first characters of an id, a directory name, or a word from the prompt
agent-handoff --find 01a00e83
agent-handoff --find workflow

# Dry run: show what would happen
agent-handoff /path/to/project --dry-run

# For real
agent-handoff /path/to/project

# In a hurry for a new session; skip the tests
agent-handoff /path/to/project --skip-tests
```

### Every flag

| Flag | What it does |
|---|---|
| `repo` | Repository path; defaults to the current directory |
| `--plan PATH` | Plan document path; omit to auto-detect the newest checkbox-bearing task document |
| `--out PATH` | Handoff file output path; defaults to the plan document's directory |
| `-m, --message MSG` | Commit message; omit to generate one |
| `--no-commit` | Do not commit; analyze and write the handoff file only |
| `--skip-tests` | Do not run tests (fast mode) |
| `--test-timeout N` | Timeout in seconds per test command; default 900 |
| `--vitals` | Only check local session transcripts and exit; never touches the repository |
| `--no-vitals` | Skip the session check (no vitals table in the handoff file) |
| `--find KEYWORD` | Locate a session by id, directory, or opening-prompt keyword |
| `--limit N` | Maximum transcripts to scan per agent; default 12 |
| `--force` | Ignore concurrent-write warnings and continue |
| `--dry-run` | Print what would happen without writing any file |
| `--lang {zh-Hans,zh-Hant,en}` | Interface and output language; omit to follow the system locale |
| `--gui` | Launch the local web interface |
| `--port N` | Web interface port; 0 picks a free port |
| `--no-browser` | Start the web interface without opening a browser |
| `--jobs N` | Parallelism; 0 decides from the CPU count |
| `--json` | Emit results as JSON for scripts to consume |

Exit codes: `0` success · `1` no session matched · `2` bad argument or environment · `3` stopped because a concurrent write was detected.

`AGENT_HANDOFF_LANG` overrides the system locale, which is handy for producing an English handoff on a Chinese-locale machine.

## How it learns your project

| Source | What it infers |
|---|---|
| git metadata | branch / HEAD / uncommitted changes / commits ahead of remote |
| `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod` | stack and test commands |
| Plan document `**Files:**` block | which files each task should produce |
| Plan document `**Interfaces:**` block | which symbols each task should produce |
| Plan document constraints block | which files must never be committed |
| Plan document Goal / Constraints sections | which sections the prompt tells the new session to read first |

Plan document format (it auto-detects the newest checkbox-bearing task document):

```markdown
**Goal:** One sentence on what this project is for.

## Global Constraints
- `docs/LOGO.jpg` is a user-owned file and must not be committed.

### Task 1: Build the data layer

**Files:**
- Create: `src/db/schema.py`
- Modify: `src/config.py`

**Interfaces:**
- Produces: `create_schema`, `Migration`

- [ ] **Step 1** Define the table structure
- [ ] **Step 2** Write the migration script
```

Both files must exist *and* both symbols must actually be defined by a `def`/`class` before it ticks the two steps. Files present but symbols undefined means "partial" — no ticks.

## Where the vitals thresholds come from

The size bands come from the measured distribution of 54 Claude and 54 Codex transcripts on one machine, not from guessing:

| Transcript size | Verdict | Measured |
|---|---|---|
| ≥ 8 MB | **hand off now** | 100% of this band hit a fatal error |
| ≥ 3 MB | hand off soon | about a third did |
| ≥ 1 MB | watch | about 17% did |
| < 1 MB | healthy | nothing under 250 KB did |

## Concurrent-write protection

Two sessions racing on one worktree is the single failure mode that genuinely loses work: our `git add` buries what they staged, or their `commit --amend` rewrites the commit we just made.

**Blocking signals** (stop by default, exit code 3) — only another process can cause these:

- files already staged that this run did not stage
- `index.lock` / `MERGE_HEAD` / `rebase-merge` / `CHERRY_PICK_HEAD` present

**Advisory signals** (continue, but record them in the handoff):

- a git-tracked file modified within the last two minutes — most often because *you* just finished editing and came straight here

Why the split: treating "just edited" as blocking would force `--force` on every run, and `--force` waves through the real blocking signals too. That is worse.

It checks again afterwards: if someone else's commit moved HEAD while it ran, the HEAD in the prompt is already stale and the tool says so explicitly.

## Prompts expire

The generated prompt ends with its generation time and the HEAD it corresponds to. Once the repository takes new commits, rerun for a fresh prompt instead of reusing the old one — the old prompt's HEAD may no longer be the state you think it is.

## What changed from the original

Every feature and flag is still here, exit codes are unchanged, and the environment variables (`PYTHONUTF8`, `PYTHONIOENCODING`) and all original comments are preserved. Beyond that:

**Cross-platform**

- Works on Linux / macOS / WSL. The original's path regex only matched `C:\...`, so it could never infer a repository off Windows, and `os.startfile` exists only on Windows
- Recognizes all three venv layouts (`Scripts/python.exe`, `bin/python`, `bin/python3`)
- Quotes interpreter paths containing spaces — `C:\Program Files\...\python.exe` was split into two arguments by the shell in the original
- Inside WSL it looks under `/mnt/c/Users/*` for the host's transcripts

**Performance**

| Step | Original | Now |
|---|---|---|
| Symbol search | one `rg` process per symbol (12 tasks × 6 symbols = 72) | one alternation regex, 1 call |
| Transcript scan | 3 full passes per file | 1 streaming pass, then a cheap branch once identity is complete |
| Many transcripts | serial | thread pool plus an mtime/size cache |
| Finding the plan | read 60 KB of every `.md` | size prefilter plus chunked reads; only candidates pay full price |
| Test commands | serial | independent commands in parallel |

**Defects fixed**

- The plan document's newline style survives. The original read CRLF and wrote LF, so ticking one checkbox made git diff show the entire file as changed
- Protected-file exclusion uses the long `:(exclude,literal)` form. The original's `:!path` is unsupported on older git and is treated as a glob when the path contains `[` or `*` — the exclusion silently fails and the private file gets committed
- Empty repositories are no longer misread. The original's `git()` returned `""` for both failure and empty output, so `rev-parse HEAD` in a repo with no commits was indistinguishable from success
- Concurrency detection no longer fires on build artifacts, caches, or the tool's own output from the previous run
- The `**Files:` block terminator is checked against the stripped line, so an indented `  **Constraints:**` no longer folds subsequent files into the previous task
- One timestamp for the whole run. The original called `datetime.now()` six times, so a run across midnight produced a filename whose date disagreed with the document title
- Test summaries understand pytest / vitest / jest / cargo / go, and actually use the `name` parameter the original accepted but never read
- Dirty submodules are reported — the parent commit does not carry them, so the next session sees an inconsistent tree
- stderr is pinned to UTF-8 too. The original handled only stdout, so a Chinese error message on a GBK console raised UnicodeEncodeError and the error ate itself
- A timed-out command keeps whatever output it produced, which is usually exactly where the failure is

## Development

```bash
pip install -e ".[dev]"
pytest              # full suite
ruff check src tests
python scripts/check_i18n.py   # translation parity check
```

CI runs on Linux / Windows / macOS × Python 3.9–3.13.

## License

MIT
