<h1 align="center">agent-handoff</h1>

<p align="center"><b>When a session dies, move the progress out of the chat and into the repository</b></p>

<p align="center">
  <a href="CHANGELOG.md"><img alt="version" src="https://img.shields.io/badge/version-2.8.1-1F6B4F?style=flat-square"></a>
  <a href="pyproject.toml"><img alt="Python" src="https://img.shields.io/badge/python-3.9%20%E2%80%93%203.13-2F5473?style=flat-square"></a>
  <a href="tests/"><img alt="tests" src="https://img.shields.io/badge/tests-819%20passed-1F6B4F?style=flat-square"></a>
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

### A typical run

```bash
# 1. See which session needs handing off (read-only; touches no repository)
agent-handoff --vitals

# 2. Cannot tell which conversation a screenshot came from? Search by the first
#    few characters of the id, by directory name, or by a word from the prompt
agent-handoff --find 01a00e83
agent-handoff --find workflow

# 3. Dry run: print what would happen without writing any file
agent-handoff /path/to/project --dry-run

# 4. Pick the sessions to carry over, then run for real
agent-handoff /path/to/project --pick-sessions

# 5. How much disk do the transcripts use, and what is safe to drop?
#    (metadata only, milliseconds; deletes nothing)
agent-handoff --sweep
agent-handoff --sweep --by-repo          # also group by repository (reads transcripts, much slower)
agent-handoff --sweep --out disk.md      # export a markdown report
```

### Every flag

| Flag | What it does |
|---|---|
| `repo` | Repository path; defaults to the current directory |
| `--plan PATH` | Plan document path; omit to auto-detect the newest checkbox-bearing task document |
| `--out PATH` | Handoff file output path; defaults to the plan document's directory (see below about same-day reruns) |
| `-m, --message MSG` | Commit message; omit to generate one |
| `--no-commit` | Do not commit; analyze and write the handoff file only |
| `--skip-tests` | Do not run tests (fast mode) |
| `--test-timeout N` | Timeout in seconds per test command; default 900 |
| `--vitals` | Only check local session transcripts and exit; never touches the repository |
| `--doctor` | Check what this machine is missing, then exit; touches neither the repository nor any transcript |
| `--no-vitals` | Skip the session check (no vitals table in the handoff file) |
| `--sweep` | Report transcript disk usage and what is safe to reclaim, then exit; **never deletes anything** |
| `--by-repo` | With `--sweep`: also group usage by repository (reads transcripts, much slower) |
| `--find KEYWORD` | Locate a session by id, directory, or topic keyword; repeatable or comma-separated to find several at once |
| `--limit N` | Maximum transcripts to scan per agent; default 12 |
| `--pick-sessions` | Interactively tick which sessions to carry over |
| `--export-bundle [DIR]` | Pack a directory you can copy to another machine (**includes transcript copies**); omit the path to write under `~/.agent-handoff/bundles/` |
| `--import-bundle DIR` | Read a bundle, resolve its paths against this machine, and report; read-only, copies no transcript |
| `--sessions PATH` | Name transcripts to carry over; repeatable or comma-separated |
| `--force` | Ignore concurrent-write warnings and continue |
| `--dry-run` | Print what would happen without writing any file |
| `--lang {zh-Hans,zh-Hant,en}` | Interface and output language; omit to follow the system locale |
| `--gui` | Launch the local web interface |
| `--port N` | Web interface port; 0 picks a free port |
| `--no-browser` | Start the web interface without opening a browser |
| `--jobs N` | Parallelism; 0 decides from the CPU count |
| `--json` | Emit results as JSON for scripts to consume |
| `--version` | Print the version and exit |

Exit codes: `0` success · `1` no session matched · `2` bad argument or environment · `3` stopped because a concurrent write was detected.

`AGENT_HANDOFF_LANG` overrides the system locale, which is handy for producing an English handoff on a Chinese-locale machine.

**A same-day rerun does not lose the previous file.** The handoff filename carries
only the date, so a second run overwrites the first. Before overwriting, the bytes
are compared: identical content is simply overwritten (most reruns are "adjust and
run again", where the newest file is exactly what you want), and only differing
content is preserved as `<name>.prev.md`. Just the most recent one is kept — a
growing chain becomes its own "which one do I read" problem, and real history
belongs to git.

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

### Documentation is not a plan document

**The output directory follows the plan document** (when `--out` is not given), so
"which file counts as the plan" decides directly where the handoff lands. Two
exclusions:

- **By name**: `README*`, `CHANGELOG*`, `CONTRIBUTING*`, `LICENSE*` never count,
  language variants such as `README.zh-Hant.md` included. Their nature is
  documentation; however plan-like the content looks, it is not a plan.
- **By fence**: Task headings and checkboxes inside a fenced code block (\`\`\` or
  ~~~) do not count. A fence is the author's own marker for "this is an example",
  and respecting it beats stacking on another heuristic.

Why both are needed: this very README *demonstrates* the plan format above,
complete with `### Task 1: Build the data layer` and four `- [ ] **Step N**` lines.
Measured without the exclusions, the three READMEs were the *only* candidates, the
most recently edited one won, and the output directory became the repository root
instead of `docs/` — triggered by nothing more than "the README was edited today".

Deliberately *not* required: "must have a goal section". A plan document with only
tasks and steps and no goal section is perfectly valid, and that rule would lock
real plans out.

### The unit of judgement is the Task, not the Step

All of a Task's steps get ticked together, or none of them do. The evidence comes
only from that Task's `**Files:**` and `**Interfaces:**` declarations — **file names
and symbols mentioned in step text do not count as evidence**.

```markdown
### Task 1: core

**Files:**
- Create: `mod.py`

**Interfaces:**
- Produces: `alpha`

- [ ] **Step 1** define alpha
- [ ] **Step 2** define gamma       ← gamma is not declared under Interfaces
```

In that Task `mod.py` exists and `alpha` really is defined, so the Task counts as
complete and **both steps get ticked** — including the one whose `gamma` does not
exist yet. That is not a misjudgement; it judges by declaration, and `gamma` never
entered the evidence set.

To have step 2 judged on its own, split it into its own Task, or declare `gamma`
under `**Interfaces:**`. The unit is the Task because steps are natural language,
and guessing a symbol name out of "define gamma" is bound to be wrong — while a
wrong tick is not reversible: a ticked step disappears from the todo list for good.

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

### Which repo a session was editing is decided by evidence, not by the launch directory

A session can start in directory A and spend the whole time editing repo B — you
paste another project's path in to ask about it, or the launch directory is just a
container. The tool used to have only two levels of evidence: `cwd` walked up to a
`.git`, falling back to "the first path mentioned in the text". **That answers
"where it started", not "what it was editing".**

Measured on this machine, the two disagree far more often than they agree:

| Side | Sample | Agreed |
|---|---|---|
| Claude Code | 12 transcripts with file-write evidence | **1** |
| Codex | 151 rollouts carrying `exec_command.workdir` | **16** |

One concrete case: a session made 614 tool calls, of which **258 file writes all
landed in one repo and not one landed in the directory `cwd` pointed at**. The repo
behind "Hand off this repository" was one the session never changed a single byte
of — copy that into a new session and it edits the wrong project.

Codex is worse: it sets `cwd` to its own session sandbox
(`~/Documents/Codex/<date>/<name>`), which contains no `.git` at all, so repo
inference fell straight through to the weakest "mentioned in text" evidence.

Attribution is now **layered by evidence**:

| Rank | Evidence | Means |
|---|---|---|
| Strongest | Codex `turn_context.workspace_roots` | the harness declared these roots itself |
| Strong | `file_path` of `Edit` / `Write` / `MultiEdit` / `NotebookEdit`, and `apply_patch` | **it changed this file** |
| Medium | Codex `exec_command` `workdir` | a command actually ran in this directory |
| Weak | paths from `Read` / `Grep` / `Glob` | it looked at this file |
| Weaker | `cwd` plus a walk up to `.git` | where it was launched |
| Weakest | paths mentioned in the text | it said this path out loud |

Rank comes first, hit count only breaks ties within a rank. One real file write says
more about what a session was editing than a hundred mentions of a path.

**"Read files" needs at least 5 hits to become the verdict.** Reading another repo is
normal — comparing against a reference implementation, checking a doc, following a
dependency's source. Measured: a session that spent its entire length discussing a
different project's deployment was attributed to this repo purely because it
incidentally read 2 files here; whereas genuinely working in a repo produces reads in
the dozens or hundreds (61, 95, 105 in real transcripts). Two reads are noise, not
attribution.

The verdict carries a confidence, and **an honest "uncertain" beats a confident wrong
answer**:

| Confidence | When |
|---|---|
| certain | a single strong signal (write / workspace / exec) |
| probably | the top strong signal leads the runner-up by 3x or more |
| uncertain | a close race, or only weak signals (launch directory, mentions) |
| no evidence | nothing was collected at all |

Working across repos (a main repo plus a plugin repo, a split front and back end) is a
real situation. When the counts are close the tool does **not** pretend to know — the
candidate list opens by default so you can decide.

Every piece of evidence is inspectable: which kind, how many hits, which repo, and
sample files. The card, the CLI card and the handoff document all list it, from the
same source of truth.

**The launch directory is not replaced, it just stops being the verdict.** Native
resume (`--resume`) has to run from the launch directory — both apps index sessions by
directory. When the two differ the card shows both, says so explicitly, and offers two
hand-off buttons: one for the repo being edited, one for the directory you can resume
in. Neither is preselected and neither is hidden.

Evidence collection has its own line budget (1500 lines, far larger than the 260/400
used for the identity card) because **file writes usually happen in the later part of a
session** — read the code, discuss the approach, then start changing things. Past the
budget the verdict still stands but is marked "only the first N lines were read",
because "no write evidence" and "no write evidence seen" are different claims.

### `cwd` is a per-line snapshot, not a session-level constant

`cwd` can change within one session, so the tool collects **every** value and ranks
them by how often each appears rather than taking the first one. Measured across 66
transcripts that carry a `cwd`:

| Situation | Count |
|---|---|
| One value only | 44 |
| Several values, differing only in drive-letter case | 15 |
| **Several values, genuinely different directories** | **7** |

The most extreme one bounces between `C:\Users\devin` and nine temp subdirectories;
another switches between two projects **19 times** (1417 lines pointing at the launch
directory, 93 at the project actually being edited). Taking only the first value gives
you "where it started" — and in a VS Code multi-root workspace that is just
`folders[0]`, unrelated to which project is being edited.

When a session genuinely moved between repos the card says "directories it sat in
(changed partway; this is one of several)" instead of "launched in" — the latter is
simply inaccurate in that case. Drive-letter case differences do not count as a move:
those come from two writers (VS Code's `fsPath` and realpath normalisation).

Collecting `cwd` is **not** subject to the line budget, unlike tool evidence. It costs
one substring check plus one length-capped regex (only the first 32 KB of a line, since
the p99 offset across 33655 `cwd`-bearing lines is 16102 bytes); a 6.5 MB transcript
scans fully in 43 ms. Not scanning it all does not merely give an incomplete answer, it
gives a **wrong** one: directory moves tend to happen late in a session, exactly where
the budget cuts off, so the tool would claim "it stayed in one directory" when the
opposite is true.

### Which root cwd comes from in a multi-root workspace

The following was read out of the Claude Code VS Code extension's own code and is
**not documented officially**:

- cwd is `workspaceFolders[0]` — the **first** entry under `folders` in the
  `.code-workspace` file. Not the active editor, and no prompt to choose.
- The remaining roots become `--add-dir` arguments to the CLI, so their
  `.claude/skills/` load but their `CLAUDE.md` does not (by default).
- Switching the active editor does **not** change cwd, for open or newly created
  sessions.
- The extension exposes no cwd / workingDirectory setting of any kind.
- VS Code's workspace context is a per-window singleton, so there is architecturally no
  way to give different extensions different working directories.

So if you launch in one directory and work in another, the transcript's `cwd` keeps
pointing at the launch directory.

**The tool discovers this by itself; there is nothing to configure.** It finds which
repos are actually open side by side in one window from two read-only sources:

| Source | What it is |
|---|---|
| `~/.claude/ide/*.lock` | A lock file the extension writes per VS Code window, carrying that window's full `workspaceFolders` array. **A fact the upstream wrote down**, not an inference. |
| `*.code-workspace` | Your hand-written workspace definition. Outlives lock files, and readable even when the tool never coexisted with that window. |

Lock files often survive after VS Code exits, and that helps: they record a grouping
that once existed, and historical sessions ran under exactly that grouping.

Once recognised, a `cwd` that lands on a workspace's **first** root drops to a
`workspace_cwd` rank — below plain `cwd`. So when two repos both have only cwd
evidence, the one from a single-root window wins: it carries real information, whereas
a workspace's first root only says "listed first".

**Only the first root is demoted, never the later ones.** The extension only ever sets
`folders[0]` as cwd, so a later root appearing in the cwd position means the session was
not opened from that multi-root window at all — there, cwd is trustworthy. Demoting
unconditionally would mislabel a **correct** cwd from a single-root window.

The wording changes in all three surfaces too — card, CLI card, handoff document. Not
"launched in" but "workspace root (listed first in a multi-root workspace; not evidence
it was edited)", plus the other roots in that workspace. That sibling list matters most
when there is no behavioural evidence at all (a pure discussion or search session): it is
the only thing that lets you recognise "oh, that project".

`agent-handoff --doctor` reports it as well: which root cwd is pinned to, that the other
roots come in only via `--add-dir`, and what you can do about it. This check never
reports "blocking" — a multi-root workspace is a legitimate setup, not a misconfiguration;
it earns a place in the checklist because it is a **silent** source of distortion.

To make `cwd` itself correct (more thorough than inferring from evidence), three routes:

1. **Move the target project to the top of `folders`** — cheapest, one JSON line. The
   cost: another extension that also takes the first root gets the mirror-image problem,
   and this relies on an undocumented implementation detail.
2. **Run `/cd <path>` in-session** (needs Claude Code v2.1.169+) — documented behaviour,
   reloads the new directory's `CLAUDE.md`, and `--resume` finds the session from there.
   Has to be done once per new session.
3. **One window per project** — clumsiest and most reliable; `cwd` is right from the start.

Workspace discovery measured at **0.12 s** (5 lock files plus a depth-3 search for
`.code-workspace` from the home directory), done once per scan and reused for every
transcript.

### "Last active" comes from the transcript, not the file time

A file's modification time is widely disconnected from the moment a session
actually stopped, and it doubles as the sort key — getting it wrong pushes the
session that needs a handoff out of sight. Measured locally:

| Side | Sample | Drift |
|---|---|---|
| Claude Code | 63 transcripts | 22 off by over a minute, 10 by over an hour, 3 by over a day, worst **about 2.8 days** |
| Codex | 324 rollouts | **287 file times precede** the last record; **268 of those equal the session's start** |

Claude drifts late because subagent sidecar writes, cloud sync and backup software
all push mtime forward; Codex drifts early because its mtime *is* the creation
moment — using it as "last active" shows the earliest activity instead.

The damage to ordering is measurable: across 492 sessions the two criteria agree on
position for **only 122 of them, with a maximum shift of 93 places**.

The last record's timestamp is now read from a 32 KB tail. Why 32 KB — 8 KB hit
only 57 of 63 locally, 32 KB hit all of them; and a single record can be enormous
(the largest line on this machine is 3.4 MB), so a smaller window can land entirely
inside one line. If the first window misses, it doubles, up to 512 KB.

The first line of a tail window is almost always a fragment, so it is dropped
before parsing: keeping it risks exposing some earlier timestamp from inside a
record that got cut in half. Compared against a full line-by-line parse over 463
real transcripts, **462 agreed exactly**.

Compressed transcripts (`.jsonl.zst`) are not tail-read — decompressing an entire
zstd stream for one timestamp is not worth it; that case falls back to the file
time and says so explicitly. The web UI, the CLI card and the handoff document all
state whether the time came from the transcript or from a fallback: a timestamp
with no stated origin gets read as established fact.

### Context fullness

**The verdict is based on context fullness, not file size.** The token counts are
written in the transcript itself, so there is nothing to guess:

| Source | Usage | Limit |
|---|---|---|
| Claude Code | `message.usage`: `input_tokens` + both cache-read fields | **not recorded**; absolute thresholds are used instead |
| Codex | `payload.info.last_token_usage.total_tokens` | `payload.info.model_context_window` (121600 measured) |

The Codex row reads `total_tokens` rather than `input_tokens` to match what Codex
itself does: its own fullness display goes through
`TokenUsage::tokens_in_context_window()`, which is `total_tokens`. `TokenUsage`
has six fields (input / cached_input / cache_write_input / output /
reasoning_output / total), so taking only input drops output and reasoning —
**reasoning-heavy sessions get systematically under-reported**, and those are
exactly the ones most worth handing off. It still uses `last_token_usage` rather
than `total_token_usage`: the latter is a whole-session cumulative figure that
far exceeds the window, so using it as fullness would report every session as
overflowing.

With a limit, the real percentage is used (≥90% hand off now, ≥75% soon,
≥55% watch). Without one, the usage figure is compared against thresholds scaled
for a 200k window — deliberately conservative for larger windows, because warning
early costs less than missing a session that is about to fill.

**When the content cannot be read, it says so instead of guessing.** For a
compressed archive (`.jsonl.zst`) with no zstd decompressor available, the
verdict is "content unreadable" rather than any risk band. Falling back to file
size would be worse than useless here: zstd typically compresses to about a
tenth, so a 27 MB session that was completely full occupies barely 2 MB on disk,
and the size heuristic would call it "watch" or even "healthy" — ranking the
session that most needs handing off as the one that least does. In the ordering
it sits between "watch" and "healthy": not counted as healthy, but not
displacing rows backed by real evidence either.

**You can declare the window size yourself.** No single absolute threshold can be
right for both a 128k and a 1M window: one local Claude session measured 102365
tokens, which the 200k-scaled thresholds call "healthy". If that model actually has
a 128k window, it is already at 80% and should read "hand off soon"; at 1M it is
only 10% and "healthy" is correct. Same number, opposite conclusions.

```bash
# Windows (current shell)
set AGENT_HANDOFF_CONTEXT_WINDOW=1000000
# Windows (persistent, applies to new shells too)
setx AGENT_HANDOFF_CONTEXT_WINDOW 1000000
# Linux / macOS
export AGENT_HANDOFF_CONTEXT_WINDOW=1000000
```

Precedence: **the limit written in the transcript > the one you declare > the
200k-scaled fallback.** The transcript figure is measured, so Codex sessions are
unaffected by this variable. An invalid value (negative, zero, fractional,
non-numeric) is treated as unset — a typo must not quietly mark the whole vitals
table healthy.

The tool does not try to guess the model: models change, and you know which one you
are running. Anthropic has never published a fixed compaction trigger threshold
either, and has shipped bugs from treating a 1M window as 200k.

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

### The reasoning behind a band is expandable

Raise rules used to be invisible in the UI: a session showing 30% fullness would
be judged "hand off now" and the user could only assume the tool was guessing.
Yet raising is the hardest part of this judgement — it rests on **something that
already happened** (a compaction means it *was* full), which beats any fullness
estimate.

Every session card now carries an expandable "Why this band", holding structured
facts rather than sentences:

- **Where the primary basis comes from**: fullness ratio / absolute token count /
  size fallback / content unreadable.
- **Where the context ceiling came from**: the transcript itself (measured) or
  `AGENT_HANDOFF_CONTEXT_WINDOW` (declared — a wrong declaration skews the whole
  fullness column).
- **Which raise rule actually fired**, and which band it raised to. Rules that did
  not fire are not listed — listing them only suggests they applied.
- Whether the final band came from a raise rather than the primary basis. That one
  is called out separately because it is the *only* reason a session with
  reasonable-looking numbers gets flagged as dangerous.

The CLI card, the web UI and the handoff document all read the same evidence, so
"the UI says compaction, the CLI says file size" cannot happen.

### How the context filled up

`tokens` only answers "how full did it get at peak". At the same 97%, climbing
steadily and doubling in the last two turns are different situations — the latter
means one recent step pushed in an enormous amount of content (a large file was
read, a log was pasted), and that is precisely what the next session should avoid.
Same for compactions: knowing "compacted 3 times" matters less than knowing "all
three landed in the last ten turns", which is the shape of thrashing.

Session cards therefore carry a fullness sparkline sampled per turn, with
compactions drawn as vertical lines (crossing one means the raw history before it
has been replaced by a summary). When the context ceiling is known the Y axis uses
it, so two sessions' charts are directly comparable; when it is not, the measured
peak is used and the chart shows relative trend only — the tooltip says which.

Sampling caps at 120 points and thins evenly beyond that, but **the endpoints and
every compaction survive**: the last point is "how full it is now", the first is
the baseline, and compactions are the only events on the line.

The chart is hand-drawn inline SVG (this project has zero dependencies and no
build step), not `<canvas>`: canvas content does not exist at all for screen
readers. The chart itself is `aria-hidden` and purely a visual supplement — the
same information exists as text in the fullness badge and the compaction count, so
it is never the only channel carrying it.

## Check what this machine is missing first

```bash
agent-handoff --doctor
```

Almost every failure in this tool used to be silent: something was missing from
the environment, and then one step's result quietly came back empty. `--doctor`
lays the checklist out before anything runs, touching neither the repository nor
any transcript:

| Check | What breaks without it |
|---|---|
| Python version | Below the declared minimum, behaviour is unpredictable |
| git on PATH, and its version | The commit snapshot, plan backfill and concurrency check all stop working |
| zstd decompression, and which implementation answered | `.jsonl.zst` is unreadable (Codex compresses rollouts older than 7 days in place) |
| stdout is UTF-8 | Transcripts always hold emoji and non-Latin text; the wrong encoding crashes midway |
| Temp directory is writable | Atomic writes cannot get a temporary file |
| Each agent data root | Existence, transcript count, how many are compressed; a directory that exists but holds nothing is called out separately |
| `CLAUDE_CONFIG_DIR` / `CODEX_HOME` / `CODEX_SESSIONS_ROOT` | Set but pointing at a missing directory is harder to spot than not set at all |
| `AGENT_HANDOFF_CONTEXT_WINDOW` | A non-positive integer silently drops fullness back to absolute thresholds |
| Multi-root workspace | cwd is pinned to the first root under `folders`, regardless of which project is being edited (never reported as blocking, only explained) |

The overall level is the worst item. **"To watch" never makes the exit code
non-zero** — a missing zstd only means compressed archives are unreadable, which
should not block the whole run; only genuinely blocking items are blocking.

The web UI has a matching "Check" view sharing exactly the same criteria. Paths
and environment variable values are redacted before display there: screenshots and
screen recordings carry the username away with them.

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

### Several sessions merged into one prompt

You can tick several sessions at once (from the vitals list or from search
results). They merge into **one** prompt, each carrying six locators:

```text
Previous sessions (3). Their full digests are in the handoff file - read that first:
  · Codex | canvas undo/redo | 2026-08-23 17:15:30
      transcript: ~\.codex\sessions\2026\08\23\rollout-...-01a02de7-....jsonl
      session id: 01a02de7-55f3-7c62-93b0-59897b54736e
      working directory: E:\output\kirara-ai\kirara-ai3.3.0b8
      native resume (lossless, try this first): codex resume 01a02de7-55f3-...
      deep link: codex://threads/01a02de7-55f3-7c62-93b0-59897b54736e
      if exported with --export-bundle: sessions/01a02de7-.../ holds the full conversation
```

Each locator answers a different question; miss one and the next session stalls:

| Locator | What it is for |
|---|---|
| Topic | recognising which piece of work this was |
| Transcript path | reading the original |
| Session id | feeding `--find`, pasting into an issue |
| Working directory | `cd` to the right place — several sessions may have run in different subdirectories, so the repo root alone is not enough |
| Resume command | going back losslessly — the **preferred path**; this handoff is the fallback |
| Bundle artifacts path | reading the full conversation (`sessions/<id>/session.md`) |

**A deep link points at the session itself, not the one it was forked from.**
This was a real bug: `thread_id` (the derivation source) took priority over
`session_id`, so the third session's link pointed at the second session's id —
opening someone else's conversation, and the link is syntactically valid so
nothing errors. Only Codex registers a URI scheme; Claude Code has none, so no
link is given there rather than inventing a `claude://` that does nothing.

Mixing apps is fine (Claude Code and Codex can be ticked together) — each gets the
resume command in its own app's form. But **mixing projects is called out**: a
handoff freezes one repository's state, and merging two scenes into one prompt
leaves the next session applying project A's conclusions to project B's code. It
warns rather than blocks, because one piece of work spanning two repositories
(split front/back end, main repo plus plugin repo) is a real situation.

## Where transcripts live, and how much they use

**Transcripts do not follow a project onto another drive.** Measured here: a
project on `E:` still has its transcripts under the home directory on `C:` — the
drive letter only shows up in the `cwd` recorded *inside* the transcript.

| App | Default location | Env var to move it |
|---|---|---|
| Claude Code | `~/.claude/projects/<cwd slug>/*.jsonl` | `CLAUDE_CONFIG_DIR` |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | `CODEX_SESSIONS_ROOT`, `CODEX_HOME` |
| Codex (archived) | `~/.codex/archived_sessions/rollout-*.jsonl` | same |
| Codex (compressed archive) | same, but the extension is `.jsonl.zst` | same |

What *does* move is the **data directory itself**: both apps let you relocate it
wholesale, and pointing it at a second drive is common on laptops with a small
system disk. Those variables take priority; the home directory is still
scanned as a fallback, because history can exist in both places and reading only
one of them loses sessions. Codex resolves in this order:
`CODEX_SESSIONS_ROOT` (which points at the sessions directory itself) →
`CODEX_HOME/sessions` → `~/.codex/sessions`. Under WSL it also looks in
`/mnt/c/Users/<name>` for the host's records.

**Compressed Codex archives.** Codex compresses rollouts older than 7 days in
place into `.jsonl.zst`. This tool scans both extensions and decompresses on
read, trying Python 3.14's standard-library `compression.zstd`, then
`zstandard`, then `pyzstd`. **It works with none of them installed**: the
session still appears in the list, with its verdict marked "content unreadable"
rather than pretending it is healthy — a compressed transcript is roughly a
tenth of its original size, so judging it by file size would rank the session
that most needs handing off as the one that least does. To read the content,
install any one of them:

```bash
pip install zstandard        # or pyzstd; Python 3.14+ ships it, nothing to install
```

This is the tool's only optional dependency, and it is deliberately **not** in
`dependencies`: zero runtime dependencies is a considered trade-off — this tool
runs in an environment that just broke, and anything requiring a pip install is
one more way to fail.

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

Mixed separators (`C:\Users/devin/proj`), POSIX-shell spellings (Git Bash's
`/c/Users/devin`, WSL's `/mnt/c/Users/devin`) and the less common home layouts
(`/export/home/`, `C:\Documents and Settings\`, `\\?\C:\Users\`) are all covered.

**Credentials are redacted too, and that pass runs first.** The document has to
carry what you asked, your last prompt and the compaction summaries — and pasting
an API key, an `Authorization: Bearer …` header or a database password into a
session is routine. Once a credential lands in git history, deleting the file does
not remove it: you have to rewrite history or rotate the secret. So this pass is
not optional:

| Shape | Example | In the document |
|---|---|---|
| OpenAI / Anthropic | `sk-ant-api03-…` | `sk-***` |
| GitHub | `ghp_…`, `github_pat_…` | `gh*_***` |
| Slack / AWS / Google | `xoxb-…`, `AKIA…`, `AIza…` | `xox*-***`, etc. |
| HTTP auth header | `Authorization: Bearer eyJ…` | `Bearer ***` |
| PEM private key | `-----BEGIN … PRIVATE KEY-----` | whole block replaced |
| URL password | `postgres://admin:pw@host` | `postgres://admin:***@host` |
| Key/value form | `api_key: "…"`, `TOKEN=…` | `api_key: ***` |

**The prefix survives instead of the whole string being starred out**: `sk-***`
tells you which secret to rotate; `***` tells you nothing.

**Only clearly-prefixed shapes are matched — no high-entropy guessing.** That kind
of heuristic would star out commit SHAs, ordinary base64 and long filenames, and
ruin the document. Purely numeric values are always left alone: `max_tokens=8192`
and `token_count: 123` are normal model config, and context usage is this tool's
central signal. The cost is that a home-grown token format may slip through — a
deliberate trade, because a document full of `***` is not a document.

**Prompts are not redacted; documents are.** You paste the prompt into a new
session on this machine and it needs real paths. The document goes into git and may
be pushed to a public repo. Different audiences.

**To actually continue the work on the new machine, use the repo identity in the
prompt** — that part was portable all along:

```
Repo identity (use this on another machine, not the local path above):
  https://github.com/you/proj.git @ eb8a6aa2217a
```

With no remote it says so plainly — this repo exists only on that machine, so
continue there — and reports how many commits are unpushed, so you do not assume
a fresh clone will carry them.

### Moving the tool and the sessions across

**The easy way: pack a bundle and take it with you.**

```bash
# On the old machine: hand off and pack in one go (picked transcripts get copied in)
agent-handoff /path/to/project --pick-sessions --export-bundle

# Copy the bundle directory over (defaults to ~/.agent-handoff/bundles/<repo>-<date>/),
# then on the new machine:
agent-handoff --import-bundle ~/bundles/myproj-2026-08-23
```

Each picked session also gets its own `sessions/<id>/` directory, four files each
answering one question:

| File | Answers |
|---|---|
| `resume.txt` | how to get back **losslessly** (the preferred path) |
| `session.md` | what this session said, ready to paste into a new one |
| `locate.txt` | working directory, session id, deep link |
| `meta.json` | the same facts, machine-readable |

`session.md` is **tiered**, not everything and not just a digest: user asks and
assistant conclusions go in verbatim, tool calls collapse to one line each
(`name(args) -> result summary`), thinking / reasoning is left out. The split follows
measured volume — in one 13452-line Codex rollout, `function_call` plus its output
took 6746 lines (50%) while actual conversation messages were 1614. Measured
compression: a 30 MB Claude transcript → 95 KB, a 90 MB Codex one → 959 KB.
Over the limit, the earliest tool summaries go first and the drop is stated.

**Arguments are kept, because a tool name alone carries no information.** Keeping
only the name tells the next session "the last one used Edit"; what it needs to
know is *which file was edited* — `Edit` appears 43 times in one measured
transcript, and without arguments those 43 lines together say as much as one.
Arguments are picked by information value, not dict order: `file_path`,
`command` and `pattern` answer "done to what" and go in first; bulk bodies like
`content` and `new_string` go last — whatever they produced is already on disk,
and the current file is more trustworthy than a historical copy in a transcript.

**Failed tool results get 5× the room** (2000 chars vs 400 for success) and are
marked with `!!` instead of `->`. A successful output is process; a failed one is
the wall the next session is about to walk into — it carries the path, the line
number, and the reason. MCP tools are named `server/tool`, since two servers can
otherwise expose indistinguishable tool names.

Harness boilerplate is not mistaken for what the user said: slash-command echoes,
background task notices and plugin listings all appear under the `user` role — in
one transcript `<command-name>` showed up 28 times against 23 real asks. After
filtering, all 116 user turns measured were real asks.

A bundle holds three things: `manifest.json` (with a `schema_version`),
`handoff/` (the document plus the opening prompt), and `transcripts/`
(**copies of the picked transcripts**).

Why the copies are mandatory: the transcript paths in a handoff document are
positions *on that machine*, not content. In
`~/.claude/projects/C--Users-devin-proj/<id>.jsonl`, the directory name itself
encodes the source machine's cwd — after a move that directory does not exist,
so the path leads nowhere. Giving the path without the content is exactly why a
handoff "stops working on the other machine".

Paths inside a bundle are stored as placeholders (`{CLAUDE_ROOT}/…`,
`{CODEX_ROOT}/…`, `{CODEX_ARCHIVED_ROOT}/…`) and **re-resolved against the target
machine's own roots** on import — no string substitution, because the new
machine's `CLAUDE_CONFIG_DIR` may point somewhere entirely different, even to
another drive. When no matching root exists here, no path is invented; you are
told to use the bundled copy.

Bundles are written **outside the repository** by default
(`~/.agent-handoff/bundles/`): they contain transcript copies, and a transcript
may hold anything you ever pasted into a session — writing them into the repo by
default would hand them straight to `git add -A`. Pass an explicit path to put one
in the repo.

`--import-bundle` is **read-only**: nothing is written to `~/.claude` or
`~/.codex`. Whether to put the copies there is your call — it changes that app's
session list. (Claude Code has also forbidden tampering with session transcript
files since v2.1.205.)

> **The transcript copies in a bundle are not redacted.** The handoff document and
> the prompt are (home directory, other machines' user names, secret shapes), but
> `transcripts/` holds **verbatim bytes** — the whole value of a copy is fidelity,
> and a redacted transcript has already lost what made it a record of that work.
> That means it can contain API keys, tokens, or passwords you pasted into those
> sessions. The `warning` field in `manifest.json` says so, and the CLI reminds you
> whenever copies were actually carried. **Review before sharing a bundle.**

---

**You can also move without a bundle: nothing is hardcoded to a user name or a
drive letter.** Every location is
worked out at runtime: the home directory from `expanduser("~")`, the checkout
root from the launcher's own location, the session directories from
`CODEX_HOME` / `CLAUDE_CONFIG_DIR` or the home directory. Copy the whole
directory to another computer — different user name, different drive — and it
runs unchanged.

Once the other machine's `.codex` / `.claude` are copied over, point the
environment variables at them. The path can be on any drive, under any name:

```bash
# Windows (cmd)
set "CODEX_HOME=D:\from-old-laptop\.codex"
set "CLAUDE_CONFIG_DIR=D:\from-old-laptop\.claude"
agent-handoff E:\output\myproj --pick-sessions

# POSIX
export CODEX_HOME=/mnt/backup/from-old-laptop/.codex
export CLAUDE_CONFIG_DIR=/mnt/backup/from-old-laptop/.claude
./scripts/agent-handoff.sh ~/proj/myapp --pick-sessions
```

The local home directory is **still scanned** after you point those elsewhere —
both sets are listed together and both are tickable. Migrating adds a location
rather than replacing one; otherwise you would lose sight of this machine's own
history the moment you copied someone else's in.

Three ways to move the tool itself, none of which need a file edited:

| Approach | Command |
|---|---|
| Install it (recommended) | `pip install -e .`, then `agent-handoff` works from any directory |
| Run the checkout | `python -m agent_handoff.cli` (from the checkout root, or set `PYTHONPATH=<checkout>/src`) |
| Use the launcher | `scripts/agent-handoff.sh` — it locates the checkout from its own path |

When a wrapper script lives in a PATH directory such as `~/bin`, point
`AGENT_HANDOFF_HOME` at the checkout:

```bash
export AGENT_HANDOFF_HOME=/opt/agent-handoff-project   # or D:\tools\agent-handoff-project
```

## Repo identity vs local path

### The resume command carries a directory change, because resume goes by directory

`claude --resume <id>` / `codex resume <id>` only finds the session when run from the
**launch directory** — both apps index sessions by directory. And the repo a session was
editing is often not that directory, so copying a bare command and pasting it where you
happen to be gives you "session not found".

So there is a second button, "copy resume command (with cd)":

```text
pushd "e:\output\proj" && claude --resume 809adf54-2839-4eab-9c9f-e17c3841ee22
```

It uses `pushd`, not `cd`, because **cmd's `cd` changes the directory but not the
drive**. Run `cd "E:\proj"` while sitting on C: and the prompt looks right while the
working directory is still on C:, so the resume runs in the wrong project — worse than
an error, because it looks like it worked. `pushd` changes both, and is an alias for
`Push-Location` in PowerShell, so one form covers all three shells.

If the path contains a newline or a double quote, **this command is not offered**:
multi-line clipboard content is executed line by line when pasted into a terminal, and
quotes cannot contain a newline. The plain command is unchanged; both are available.

### Across machines, go by identity, not by path

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
