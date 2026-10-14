# WP-03 Natural Request Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close WP-03 with deterministic project/context resolution, scoped retrieval, native-friendly entry behavior, and citation-bound natural answers.

**Architecture:** Keep routing, scope resolution, retrieval, and answer validation as separate read-only application layers. The CLI composes those layers, exposes human-readable output by default, and exposes machine schemas only when `--json` is requested. Native client discovery remains instruction driven by the canonical workspace entry and is documented only with observed evidence.

**Tech Stack:** Python 3.12, Typer, SQLite operational store, embedded SQLite RAG, pytest, Ruff.

**Spec:** `AKTIF_GOREV.md` WP-03.02 through WP-03.05 and the repository `AGENTS.md` contract.

## Global Constraints

- Work only in the real source root; do not create a mirror or worktree.
- Keep routing, retrieval, and answer evaluation read-only and authority-free.
- Do not start provider calls, indexing, embedding, or mutation from an ambiguous request.
- Do not put secrets, PII, raw transcripts, or user artifacts in tracked files.
- Commit messages are meaningful Turkish ASCII and each meaningful stage is pushed.

## Review Focus

- Explicit, route, Jira, Work, and session signals conflict: clarify without querying.
- A multi-project comparison keeps results and citations separated by project.
- Missing or unreadable indexes report a natural-language failure without starting indexing.
- A native-friendly default CLI path must not print JSON, UUIDs, or digests to the user.
- A proposed explanation is accepted only with locator-bound citations and never claims mutation success.

### Task 1: Scope and evidence closure

**Files:**
- Modify: `src/zekam/application/natural_request_scope.py` only if a failing precedence test identifies a gap.
- Test: `tests/unit/test_natural_request_scope.py`, `tests/integration/test_natural_request_evidence.py`.

**Interfaces:** Consume `RequestRoute`, verified `ContextSignal`, and operational readback evidence. Produce a selected or clarification-required `NaturalRequestScope` with no authority.

- [ ] Run the focused scope/evidence tests and record any concrete gap.
- [ ] Add a regression test before changing code when a gap exists.
- [ ] Implement the smallest deterministic fix and rerun the focused tests.
- [ ] Commit the verified change with an ASCII Turkish message.

### Task 2: Canonical CLI retrieval and native-friendly rendering

**Files:**
- Modify: `src/zekam/interfaces/cli/main.py`.
- Test: `tests/e2e/test_cli_request_route.py`, `tests/e2e/test_ask_real_dual_index.py`, and a new tracked CLI answer test if needed.

**Interfaces:** Consume `resolve_natural_request_scope`, `retrieve_natural_request`, and `answer_natural_request`. Produce one scoped retrieval envelope and a concise human-readable answer by default; preserve JSON only behind `--json`.

- [ ] Write failing tests for default human output and fail-closed unavailable/clarification output.
- [ ] Compose retrieval with the answer validator without invoking providers or mutations.
- [ ] Keep JSON output schema stable and include retrieval separately from answer text.
- [ ] Run targeted CLI and real dual-index tests, then commit and push.

### Task 3: Native discovery evidence and acceptance record

**Files:**
- Modify: `AKTIF_GOREV.md` and generated `AKTIF_GOREV.yaml` only after executable evidence is collected.
- Test: existing native workspace entry tests; add no fake provider/native transcript.

**Interfaces:** Use canonical `AGENTS.md` and thin client redirectors. Record whether a fresh native session actually selected the matching `zekam` command and summarized evidence.

- [ ] Inspect the installed native CLI capability without sending a mutating request.
- [ ] If a real read-only native probe is available, capture only bounded schema-free evidence; otherwise record the exact blocker.
- [ ] Close only the acceptance boxes proven by tests and keep external-host gaps explicit.
- [ ] Commit and push the evidence update.

### Task 4: WP-03 closeout and verification

**Files:** `AKTIF_GOREV.md`, `AKTIF_GOREV.yaml`, and the plan status.

- [ ] Run the complete WP-03 focused test set, Ruff, and diff checks.
- [ ] Regenerate the YAML projection from the Markdown authority.
- [ ] Verify Git status contains no protected user artifacts staged.
- [ ] Commit the closeout and push `main`.

