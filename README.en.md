<h1 align="center">agent-handoff</h1>

<p align="center"><b>When a session dies, move the progress out of the chat and into the repository</b></p>

<p align="center">
  <a href="CHANGELOG.md"><img alt="version" src="https://img.shields.io/badge/version-2.3.0-1F6B4F?style=flat-square"></a>
  <a href="pyproject.toml"><img alt="Python" src="https://img.shields.io/badge/python-3.9%20%E2%80%93%203.13-2F5473?style=flat-square"></a>
  <a href="tests/"><img alt="tests" src="https://img.shields.io/badge/tests-392%20passed-1F6B4F?style=flat-square"></a>
  <a href="pyproject.toml"><img alt="runtime deps" src="https://img.shields.io/badge/runtime%20deps-0-7C6210?style=flat-square"></a>
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-MIT-6B7B7E?style=flat-square"></a>
</p>

<p align="center">
  <a href="README.md">简体中文</a> · <a href="README.zh-Hant.md">繁體中文</a> · English
</p>

AI coding sessions die suddenly — an upstream 400, a provider cutting you off, a context limit. What dies is not the code; the code is on disk. What dies is **everything that only existed in that conversation**: the goal, the approaches already ruled out, the hard limits, what to do next. A fresh session starts from nothing, so it redoes finished work, or edits the very file you said never to touch.

> **When to run it**: when a session's context is filling up. The tool reads the
> token counts straight out of the transcript (Claude's `message.usage`, Codex's
> `token_count` events) rather than guessing — and a session that has already
> compacted is treated as having hit the limit, because automatic compaction only
> fires when things no longer fit. See
> [where the vitals thresholds come from](#where-the-vitals-thresholds-come-from).

## It does four things

| | What | On what evidence |
|:--:|---|---|
| **1** | **Commit a snapshot** | Excluding files the plan declares user-owned |
| **2** | **Backfill the plan** | Tick from objective evidence: files exist, symbols **actually defined** |
| **3** | **Carry sessions over** | Tick the relevant sessions; the digests they wrote **themselves**, plus your verbatim asks |
| **4** | **Write the handoff** | A handoff Markdown plus an opening prompt you paste straight in |

It **hardcodes no project knowledge**: project name, paths, task names, and test commands are all inferred from the repository itself.

<p align="center">
  <img src="docs/img/gui-light.png" alt="Session vitals (light)" width="880">
  <br><sub>Session vitals · light</sub>
</p>

<details>
<summary><b>Dark (follows the system)</b></summary>
<p align="center">
  <img src="docs/img/gui-dark.png" alt="Session vitals (dark)" width="880">
</p>
</details>

<details>
<summary><b>What a finished handoff looks like</b> (completion verdicts · gaps · protected files · opening prompt)</summary>
<p align="center">
  <img src="docs/img/gui-result.png" alt="Handoff result: completion table, gap detail, protected files, and the copyable opening prompt" width="880">
  <br><sub>Look at <code>Task 2</code>: the file exists, but the <code>render_report</code> it declares is not defined → <b>partial</b>, not ticked.<br>
  That is the single most important judgement this tool makes.</sub>
</p>
</details>

---

## Read this first: when *not* to use it

Same app, same machine, same provider, context not exhausted, session file intact —
**use native resume, not this tool**:

```bash
claude --resume          # or claude --continue / /resume in-session
codex resume             # or codex resume --last
```

Native resume restores the **full conversation history** (Claude Code docs:
"the full history, including tool calls and results") — a verbatim token replay,
**lossless**. What this tool produces is a **lossy digest**. A digest cannot equal
a verbatim replay, and no amount of engineering changes that.

Its value is confined to what native resume **structurally cannot** do:

| Situation | Why native cannot |
|---|---|
| Context already exhausted | The official answer is also a digest — idle 1h + over 100k tokens offers "Resume from summary", which is `/compact` |
| Across apps (Claude Code ↔ Codex) | Different formats and event semantics; **no official import mechanism** |
| Different model / provider | Docs state the model is not restored when retired, overridden by `--model`, or on deployment-ID providers such as Bedrock |
| Corrupted session file | `Failed to resume the conversation`, exit 1 |
| Deliberately discarding poisoned history | Native gives all-or-digest; `/branch` copies rather than trims |
| Across machines | Transcripts copy, but the official ID lookup resolves only when exactly one project holds that ID; a hand-copied file reads as not-found |
| plan mode / bypassPermissions / background tasks / MCP / CLI startup flags | Docs list these as **never restored** — a written handoff is actually more reliable |

### What cannot travel, in principle

The tool writes this into the generated prompt so the next session does not read
"absent from the digest" as "did not happen":

- Model-internal reasoning state and prompt cache (`encrypted_content` is
  server-side encrypted and dies with the session)
- Tool-approval runtime state — the new session will prompt again
- MCP connections and auth tokens (transcripts record tool *names*, not connections)
- Background processes, listening ports, already-started services
- The reasoning behind rejected approaches — thinking carries no signature and
  cannot be replayed, and most of it lives in subagent transcripts
  (measured process:report ratio **124:1** — 14.6 MB of process, 118 KB of reports)

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

# Tick which sessions to carry over: lists local sessions, you pick by number
agent-handoff /path/to/project --pick-sessions

# In a hurry for a new session; skip the tests
agent-handoff /path/to/project --skip-tests

# How much disk do the transcripts use? What is safe to throw away?
# Metadata only, so it finishes in milliseconds. It never deletes anything.
agent-handoff --sweep
agent-handoff --sweep --by-repo        # also group by repository (reads transcripts, much slower)
agent-handoff --sweep --out disk.md   # export a markdown report
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
| `--sweep` | Report transcript disk usage and what is safe to reclaim, then exit; **never deletes anything** |
| `--by-repo` | With `--sweep`: also group usage by repository (reads transcripts, much slower) |
| `--find KEYWORD` | Locate a session by id, directory, or topic keyword |
| `--limit N` | Maximum transcripts to scan per agent; default 12 |
| `--pick-sessions` | Interactively tick which sessions to carry over |
| `--sessions PATH` | Name transcripts to carry over; repeatable or comma-separated |
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

Both files must exist *and* both symbols must actually be defined before it ticks the two steps. Files present but symbols undefined means "partial" — no ticks.

The parser accommodates how real markdown is written, because failing to
recognise a form does not raise — it silently distorts completion:
the verb may be bold (`- **Modify**: x`), the colon optional (`- Modify x`),
the verb absent (`- \`x\` — note`), several paths may share a line,
`-` `*` `+` all count as list markers, headings may be levels 1–6 and indented,
`Task` and `Phase` both count, and `Exports`/`Provides` join `Produces`.

### How a symbol counts as "actually defined"

Not "the word appears in the file". Three definition forms count, and
**references do not**:

```ts
interface Intent {
  undo: () => void          // ✅ interface member
}
const intent = {
  undo: () => { step() }    // ✅ object-literal property
}
class M {
  performAction(a: () => void) {   // ✅ method shorthand, no keyword prefix
  }
}

intent.undo()               // ❌ a call is not a definition
// interface undo comes from the store   ❌ a comment reference is not a definition
```

The last two forms are the mainstream in TS / Vue / modern JS, and **none of them
carries a keyword prefix**. A detector that only accepts "keyword + space + name"
judges a repository full of `undo: () => void` as "all symbols missing", so the
next session redoes finished work — a real misjudgement this tool hit.
Comments are blanked before matching (replaced with equal-length whitespace so
line and column offsets survive), because **false positives are worse than false
negatives**: they let the backfill tick steps that were never implemented, and the
to-do disappears for good.

The plan document itself is excluded from the symbol search. Otherwise
``- Produces `undo` `` inside it becomes evidence that `undo` exists — the plan
satisfying itself.

A failed search (all three backends down) is not the same as "searched, absent":
the former is never allowed to mark a task complete, because ticking writes to the
plan document and is irreversible.

## Where the vitals thresholds come from

**The verdict is based on context fullness, not file size.** The token counts are
written in the transcript itself, so there is nothing to guess:

| Source | Usage | Limit |
|---|---|---|
| Claude Code | `message.usage`: `input_tokens` + both cache-read fields | **not recorded**; absolute thresholds are used instead |
| Codex | `payload.info.last_token_usage.input_tokens` | `payload.info.model_context_window` (121600 measured) |

With a limit, the real percentage is used (≥90% hand off now, ≥75% soon,
≥55% watch). Without one, the usage figure is compared against thresholds scaled
for a 200k window — deliberately conservative for larger windows, because warning
early costs less than missing a session that is about to fill.

**Compaction means the context already filled up.** Automatic compaction only
fires when the context can no longer fit, which makes it the hardest evidence
available — harder than any percentage. One compaction reads as "hand off soon",
two or more as "hand off now", because every compaction drops older history.

| Source | Compaction event |
|---|---|
| Claude Code | `type:"system"` + `subtype:"compact_boundary"`, carrying `compactMetadata.preTokens` |
| Codex | `type:"compacted"` records |

### Why not size

Size and real usage come apart badly. Four transcripts measured on one machine:

| Transcript | Size | Actual usage | Compactions | Size would say | Actually |
|---|---|---|---|---|---|
| A | 1.0 MB | 194,183 tokens | 0 | healthy | **hand off now** |
| B | 2.0 MB | 172,490 tokens | **10** | watch | **hand off now** |
| C | 27.4 MB | 710,340 tokens | 0 | hand off now | hand off now |
| D (Codex) | 6.4 MB | 121,407 / 121,600 = **99.8%** | 1 | hand off soon | **hand off now** |

B makes the point: **it is small precisely because compaction kept discarding
history.** Smaller can mean more was lost, and the size heuristic labels exactly
this kind of session "watch".

Size is kept as the **fallback** for transcripts that record no token counts
(older formats, truncated files) — there it is the only signal available. The
chart below is the measured basis for those fallback thresholds:

<p align="center">
  <img src="docs/img/bands.svg" alt="Transcript size vs measured fatal-error rate: 0% under 1 MB, 17% from 1 MB, 30% from 3 MB, 100% from 8 MB" width="880">
</p>

**Fatal** means a signature that genuinely killed sessions (`content-blocked`,
provider breaker, no channel available, image dimensions exceeded) — and it must
appear in an **error payload field**. Matching raw lines counts *discussion* of
those words as *occurrences*: of 239 raw matches across 14 main transcripts,
94 came from assistant prose and 83 from user prose (your own CLAUDE.md saying
"when you see content-blocked…" gets counted too).

**Aborted turns** (`turn_aborted`) are counted separately: `is_error` fired only
3 times across 40 Codex rollouts while `turn_aborted` fired 6. Treating
half-finished work as finished is the most expensive misread in a handoff.

## How session content travels

Once you tick sessions, the tool extracts **what the sessions wrote about
themselves** rather than guessing:

| Source | Content | Why it matters |
|---|---|---|
| Codex `compacted` events | the handoff digest the model wrote at compaction — **every window** | carries repo path, real HEAD, documents read, task scope |
| `compacted.replacement_history` | the **user's own words**, verbatim | a digest is a paraphrase, and paraphrase drops constraints in the wording |
| Claude `ai-title` | one-line topic | this is how a human recognises a session, not by eight hex digits |
| Claude `last-prompt` | last user input | says where the session stopped |

**Every compaction window must be kept.** Across 70 rollouts with compaction
(52 multi-window, up to 19 windows): `window_number` increments,
`previous_window_id` forms a chain, each window summarises only its own stretch,
and **no sample's last window contains its first verbatim**. Keeping only the last
loses a median of **78%** (p90 96%) of concrete facts — commit shas, file paths,
test counts — and in 11 rollouts the *user's goal and hard limits* appear only in
an early window, which is precisely what must not be lost. With every window kept,
measured loss is **0%** (70/70).

Digests are also no longer truncated: 62 of 70 final windows exceeded 4000
characters, with a median of 2925 and up to 13241 characters cut — and what gets
cut is usually the conclusions and the to-do list. The full digest goes into the
handoff **document** (a file can afford tens of KB); the prompt carries only
topics and paths.

## Where transcripts live, and how much they use

**Transcripts do not follow a project onto another drive.** Measured here: a
project on `E:` still has its transcripts under the home directory on `C:` — the
drive letter only shows up in the `cwd` recorded *inside* the transcript.

| App | Default location | Env var to move it |
|---|---|---|
| Claude Code | `~/.claude/projects/<cwd slug>/*.jsonl` | `CLAUDE_CONFIG_DIR` |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | `CODEX_HOME` |
| Codex (archived) | `~/.codex/archived_sessions/rollout-*.jsonl` | same |

What *does* move is the **data directory itself**: both apps let you relocate it
wholesale, and pointing it at a second drive is common on laptops with a small
system disk. Those two variables take priority; the home directory is still
scanned as a fallback, because history can exist in both places and reading only
one of them loses sessions. Under WSL it also looks in `/mnt/c/Users/<name>` for
the host's records.

`--sweep` reports usage and **deletes nothing**:

```bash
agent-handoff --sweep
```

It reads file metadata only, never contents — that is the whole reason it is
fast. Measured on 423 transcripts totalling 1.09 GB:

| Approach | Time |
|---|---|
| Directory walk only | 8 ms |
| Walk + `stat` (what `--sweep` does) | **10 ms** |
| Reading the first 64 KB of each file | 219 ms (22x slower) |

**Ranked by size, not by age.** Measured here, "older than 30 days" is 0 files,
while a single 90 MB file accounts for 8% of the total. The disk is always eaten
by a handful of files.

Three categories are reported as safe to reclaim, each with its reason:

| Category | Why it is safe |
|---|---|
| Subagent transcripts | A subagent's own working log, not a conversation you had — you can never resume one |
| Archived sessions | Already archived in Codex, so it cannot be resumed; the text is still there |
| Empty sessions | Under 32 KB, too small to hold a turn with any content |

The categories are mutually exclusive: a transcript that is both small and a
subagent's is counted once, so "reclaimable" is never double-counted.

Add `--by-repo` to group by repository (it has to read transcripts to get the
cwd — 24 s versus 12 ms — hence a separate flag). What it shows is blunt: on this
machine two kirara-ai projects on `E:` account for 828 MB, 76% of the total.

> **Why it does not delete for you.** A transcript may hold the only record of
> that work, and deletion cannot be undone. Deciding what is genuinely
> disposable needs a human to look, so the tool produces a reviewable list and
> leaves the choice to you — the same stance as the rest of it: give evidence,
> take no irreversible action.

## Moving to a new machine

Transcripts are ordinary files: copy them across and the tool reads them. It
recognises that they were not produced here, and adjusts.

**Detection is structural, not a guess at usernames.** A hardcoded list of names
breaks on the next machine, so the test is the shape of the home-directory
segment in the path: `<drive>:\Users\<name>`, `/home/<name>`, `/Users/<name>`,
`/mnt/<drive>/Users/<name>`. If the name in there is not one of this machine's
(the basename of `~`, plus `USERNAME` / `USER` / `LOGNAME` — under WSL the two
sides can differ and both count as local), the transcript is foreign.

Three things then change:

| | Local session | Foreign session |
|---|---|---|
| Context verdict | normal | **equally normal** — token counts do not depend on the machine |
| Native resume command | offered | **withheld** — the app indexes only its own data directory, so the command would always fail |
| Paths | directly usable | labelled as belonging to that machine, so you do not go looking for files that are not here |
| Carrying content over | tickable | **equally tickable** — that is exactly what you want when moving machines |

**Other people's usernames are redacted too.** In the handoff document
`D:\Users\bob\myproj` becomes `D:\Users\_USER_\myproj` — only the name segment,
the rest of the path stays (you still need it to work out where that project sat
on that machine). Claude's slug directory name `D--Users-bob-myproj` is handled
as well: the username is embedded in a `-`-separated segment there, and missing
it leaks the name through the transcript path.

**To actually continue the work on the new machine, use the repo identity in the
prompt** — that part was portable all along:

```
Repo identity (use this on another machine, not the local path above):
  https://github.com/you/proj.git @ eb8a6aa2217a
```

With no remote it says so plainly — this repo exists only on that machine, so
continue there — and reports how many commits are unpushed, so you do not assume
a fresh clone will carry them.

## Repo identity vs local path

The prompt states both:

```text
Resume myproject. Repo E:/output/myproject, branch main, HEAD 1edd107840d5.
Repo identity (use this on another machine, not the local path above):
  https://github.com/you/myproject.git @ 1edd107840d564691f92470e4d99e2b283f1a8f5
```

A path is "where it sits on this machine"; the remote URL plus full sha is the
**identity**. On another machine, in a container, under WSL, or in Codespaces the
path does not exist — and two working copies of the same remote (`proj-a5` and
`proj-b8`) can only be told apart by path. With no remote, the tool says plainly
that the repo exists only on this machine.

**Unpushed commits are declared separately**, because they do not travel — a fresh
clone gets only what the remote has. The count comes from
`rev-list --count HEAD --not --remotes` rather than `@{u}..HEAD`: the latter
depends on how fresh the remote-tracking ref is and undercounts when FETCH_HEAD is
stale (one measured repo reported `ahead` 0 while its HEAD existed on no remote).

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
