# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Agent Office — a pixel-office dashboard that watches Claude Code / Codex workers running in tmux and visualizes their status. Currently a Phase 1 MVP (watcher + dashboard); Phase 2 (PM bot spawning workers from a task queue) and Phase 3 (Discord integration, auto quota updates) are on the roadmap in `office/README.md`. Documentation, comments, and UI text are in Korean.

## Running

```bash
python3 office/office.py    # → http://localhost:8765
```

No dependencies — Python 3.9+ stdlib only. There is no build step, linter, or test suite. Without tmux (or with no workers) it serves an empty office, so it runs fine in this environment for smoke-testing. Env vars: `OFFICE_PORT` (8765), `OFFICE_POLL` (poll interval secs, 5), `OFFICE_STALL_SECS` (stall threshold, 600).

`office/spawn.sh <name> <workdir> ["prompt"]` starts a worker: a tmux session named `agent-<name>` running `claude`.

## Architecture

Two files do all the work:

- **`office/office.py`** — single-file server. A background collector thread loops every `OFFICE_POLL` seconds and assembles a global `_state` dict (guarded by `_lock`) from four sources:
  1. **Agents**: `tmux list-panes -a` + `capture-pane`, classifying each pane as busy/idle/stalled/unknown by matching `BUSY_MARKERS`/`IDLE_MARKERS`.
  2. **Usage**: sums today's token usage and estimated cost from `~/.claude/projects/**/*.jsonl` transcripts, using the `PRICE` table (USD per MTok per model family).
  3. **Quota**: `office/quota.json` (manually maintained; see `quota.json.example`), enriched by `with_pace()` which computes the recommended pace = fraction of the quota window elapsed, derived from `resets_at` and `window_days`/`window_hours`.
  4. **Tasks**: `office/tasks.json` (see `tasks.json.example`).

  The HTTP handler serves `office/web/` statically plus one JSON endpoint, `GET /api/state`.

- **`office/web/index.html`** — self-contained frontend (inline CSS/JS, no framework). Polls `/api/state` and renders the pixel office on a `<canvas>` plus panels for usage, quota gauges, and the task board.

`office/quota.json` and `office/tasks.json` are runtime config read fresh on every poll — absent files just mean empty sections.

## Load-bearing design decisions

- **Busy detection is restricted to the bottom `STATUS_TAIL_LINES` (8) lines of the pane** (`classify()` in office.py). Do not widen matching to the whole screen: workers quoting phrases like "esc to interrupt" in report text caused false busy positives in the original system this was reverse-engineered from. Stall detection, by contrast, deliberately hashes the *entire* pane content.
- **Stall state** (`_pane_memory`) means: busy markers present but the screen unchanged for `OFFICE_STALL_SECS`.
- Pace-gauge semantics (marker position, `초과 +X.X%p` / `잔여권고` display, edge cases around window rollover) are specified in `plans/2026-07-12-agent-office-work-plan.md` — check it before changing quota/pace logic.

## Repo conventions

- `plans/` holds dated work-planning documents (`YYYY-MM-DD-*.md`) that record context, specs, and acceptance criteria; the README's roadmap section reflects them. When implementing a planned feature, read the corresponding plan first.
- Keep office.py dependency-free (stdlib only) and the frontend self-contained — that constraint is intentional for the MVP.
