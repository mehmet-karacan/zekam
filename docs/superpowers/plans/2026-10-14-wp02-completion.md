# WP-02 Proje Yerel Baslangic, Izinler ve Ortak MCP Uygulama Plani

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** WP-02 kapsamindaki proje-yerel giris, istemci yetenek matrisi, host-native izin siniri, ortak MCP akisi ve legacy sahiplik planini test edilebilir kanitla kapatmak.

**Architecture:** Koken kanonik `AGENTS.md` olarak kalir; alt dizin girisleri generator tarafindan ince yonlendirici olur. Istemci farklari `ClientCapabilityProfile` ve mevcut integration plan/receipt tipleri ile kaydedilir. MCP ve global legacy akislari plan -> sahiplik -> apply -> readback -> rollback zincirinde, credential kopyalamadan calisir.

**Tech Stack:** Python 3.11+, pytest, SQLite operational store, mevcut client/mcp integration application katmani, OpenCode JSON permissions.

**Spec:** `AKTIF_GOREV.md` WP-02.01..WP-02.05 ve `docs/CLI_ENTEGRASYON_POLITIKASI.md`.

## Global Constraints

- Gercek kaynak kokunde calis; kopya, mirror, detached worktree veya gecici proje klonu olusturma.
- Secret, PII, ham transcript ve kullanici credential'ini prompt, log, artifact veya Git'e alma.
- Global HOME/data tasimasi veya yeni canli provider/model kurulumu yapma.
- Commit mesaji Turkce anlamli ve ASCII-only olsun; her tamamlanan asama push edilsin.
- Gercek host binary kabul kaniti yoksa kutuyu kapatma; capability matrisi dosya tespiti ile native kabul kanitini ayri tut.

## Review Focus

- Koken disi dizinde girisin inert kalmasi: `test_natural_language_workspace_entry.py` ile sinanir.
- Istemci surumu yokken ready/observed iddiasi: `test_client_capability_matrix.py` ile sinanir.
- Dogrudan ve dolayli shell publication ayrimi: `test_opencode_native_policy.py` ve `test_publish_authorization_boundary.py` ile sinanir.
- MCP drift/conflict ve rollback sonrasi readback: `test_client_integration_sync.py` ve `test_cli_mcp.py` ile sinanir.
- Legacy artifact sahipligi belirsizken tasima yapilmamasi: integration plan testleri ve salt-okunur inventory kaniti ile sinanir.

---

### Task 1: Proje-yerel giris ve istemci kanit matrisi

**Files:**
- Modify: `src/zekam/application/client_instruction_bootstrap.py` only if a missing generator invariant is found.
- Modify: `src/zekam/application/capability_profile.py` only if a missing version/evidence field is found.
- Test: `tests/unit/test_project_local_entry_bootstrap.py`, `tests/e2e/test_natural_language_workspace_entry.py`, `tests/unit/test_client_capability_matrix.py`.
- Docs: `AKTIF_GOREV.md`, generated `AKTIF_GOREV.yaml`.

**Interfaces:**
- Consumes existing canonical root and subdirectory entry generator and `ClientCapabilityMatrix`.
- Produces deterministic duplicate-free plan/apply/readback evidence; unavailable native clients remain explicitly unobserved.

- [x] Step 1: Run the three target test groups and record exact pass/skip output.
- [x] Step 2: Add only the smallest missing provider-free assertion or adapter evidence for root, subdirectory, outside-workspace inert behavior, and duplicate instruction ownership.
- [x] Step 3: Re-run targeted tests and package validator.
- [x] Step 4: Update WP-02.01/02 notes only for behavior proven by tests; keep real-host blockers open when binaries are absent.
- [ ] Step 5: Commit with an ASCII Turkish message and push.

### Task 2: Host-native shell and publication boundary

**Files:**
- Modify: `opencode.json` or `src/zekam/application/opencode_*` only when a failing test identifies a concrete direct-policy gap.
- Test: `tests/unit/test_opencode_native_policy.py`, `tests/security/test_publish_authorization_boundary.py`, `tests/e2e/test_cli_opencode_windows.py`.
- Docs: `docs/CLI_ENTEGRASYON_POLITIKASI.md`, `AKTIF_GOREV.md`, generated `AKTIF_GOREV.yaml`.

**Interfaces:**
- Direct shell allow and direct `git push`/publish deny remain host-native.
- Indirect shell publication is reported as an explicit limitation unless a deterministic host guard can be tested without a live provider.

- [x] Step 1: Run direct policy, security boundary, and Windows CLI tests.
- [x] Step 2: Fix a concrete direct-policy failure if present; do not claim indirect coverage from prompt text.
- [x] Step 3: Add/update a provider-free limitation test or evidence note for scanner-bypassed commands.
- [x] Step 4: Re-run tests and package validator; update WP-02.03 status honestly.
- [x] Step 5: Commit and push.

### Task 3: MCP projection and legacy ownership plan

**Files:**
- Modify: `src/zekam/application/mcp_integrations.py` only for a concrete plan/apply/readback/rollback or reparse gap.
- Modify: `src/zekam/application/client_integrations.py` only for exact ownership/drift/conflict evidence gaps.
- Test: `tests/unit/test_mcp_integration.py`, `tests/integration/test_client_integration_sync.py`, `tests/e2e/test_cli_mcp.py`, relevant legacy migration tests.
- Docs: `docs/CLI_ENTEGRASYON_POLITIKASI.md`, `AKTIF_GOREV.md`, generated `AKTIF_GOREV.yaml`.

**Interfaces:**
- One canonical MCP registry is projected per supported host; Gemini or unavailable clients are represented as unsupported/unobserved, never as ready.
- Apply is receipt-bound and readback verified; drift/conflict blocks overwrite; rollback restores prior bytes; no credentials are copied or removed.
- Global legacy cleanup remains exact-ownership plan-only when no portable owned artifact exists.

- [x] Step 1: Run unit, integration, E2E MCP, and legacy migration tests.
- [x] Step 2: Close only concrete failures with deterministic tests; preserve unsupported host boundaries.
- [x] Step 3: Capture read-only global ownership inventory and exact migration plan evidence without changing HOME.
- [x] Step 4: Re-run all target tests and package validator.
- [ ] Step 5: Commit and push.

### Task 4: Final WP-02 acceptance and evidence projection

**Files:**
- Modify: `AKTIF_GOREV.md` and generated `AKTIF_GOREV.yaml`.
- Test: complete WP-02 target matrix plus package validator.

- [x] Step 1: Run all WP-02 target tests in one command and save only tracked evidence updates.
- [x] Step 2: Check every WP-02 checkbox against an exact test, receipt, or real-host observation; leave unsupported acceptance unchecked.
- [x] Step 3: Regenerate `AKTIF_GOREV.yaml` from the canonical Markdown source.
- [x] Step 4: Run `git diff --check`, package validator, and final status inspection excluding preexisting user untracked files.
- [x] Step 5: Commit and push the final evidence update.
