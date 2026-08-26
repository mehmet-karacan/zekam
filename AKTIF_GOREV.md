# Zekam Aktif Görev Projeksiyonu

> Bu dosya kanonik PostgreSQL Work Graph'tan deterministik olarak üretilen, salt okunur bir projeksiyondur. Yetki vermez.

## Aktif iş

| Alan | Değer |
|---|---|
| Proje | `zekam` (`01a028b0-8ed6-752a-a09c-8e7ffd47fbe3`) |
| Work | `01a03cd8-9db2-7061-a040-e105b89d3032` — Zekam Memory Continuity Plane |
| Durum | `completed`; revision `6` |
| Work digest | `sha256:0b6bf8857cf2f0909e208f92f9b6e6be6d179ba00d59eaeff510519f353fefba` |
| Plan | rev `6` / `01a03d56-770f-71c1-be0a-343dd71e743d` |
| Run | `completed` / `a104ea1f-130f-4c00-963a-49f9a593f300` |
| Source HEAD | `b8d970cddd2841b95cf71de6eac53f6134e734c6` |
| Source tree | `sha256:9f723998ade76766730a49116f8b91fc94fff846d9b008bed6e6e6ac9289ac82` |
| Memory | migration `55`, mode `enforced`, hooks current |
| Kabul | `12/12` doğrulandı |
| Yetki | `false`; approval devralınmadı |

## Plan adımları

| Adım | Açıklama | Etki |
|---|---|---|
| `runtime-receipt-reconciliation-audit` | Audit the preserved bootstrap receipt and zero-live-lease state | `none` |
| `acceptance-evidence-finalization` | Bind full quality, PostgreSQL, security, package and rollback evidence | `process-run` |
| `independent-verification` | Verify exact acceptance evidence with a separate model and execution identity | `process-run` |
| `enforcement-transition` | Apply shadow to enforced only with snapshot-bound verifier and exact authorization | `database-write` |
| `continuity-finalization` | Persist terminal continuity receipts and final Work evidence | `database-write` |

## Kabul kriterleri

- [x] Single-writer containment, exact local/remote source revision and clean logical-resource ownership are proven.
- [x] Canonical Work, Intent, Plan, run/step/checkpoint/receipt chain and continuity packet remain consistent.
- [x] Current migrations and additive Memory Continuity migration pass fresh, upgrade and bounded rollback rehearsal.
- [x] Session lifecycle, hydration, close and compaction contracts are strict, authority-free and receipt-bound.
- [x] All 20 Memory Contract invariants have deterministic evaluation and passing evidence.
- [x] Candidate-only compiler passes provenance, quarantine, conflict, watermark, replay and no-self-promotion gates.
- [x] Hook uniqueness, origin/recursion guards and supported multi-harness lifecycle contracts pass.
- [x] History import preview, second-consent, filters, collision, integrity and private-data negative tests pass.
- [x] Read-only projections are deterministic, privacy-filtered and fresh against source, migration and DB revision.
- [x] Security, secret-history/public-leak, package validation and full quality suites pass.
- [x] Independent verifier is separate from the builder and approves exact acceptance evidence.
- [x] Final remote drift check passes; no push, PR, merge or live provider call occurs.

## Süreklilik ve güvenlik

- Projection receipt: `sha256:d0ea6e157ef721b603b8537226c03d2ddadb12c40c3fa198f60fb050e8506419`
- Hook set: `sha256:92bf59c00cf870c09a67bd26817c1509e28f8e3d855c3cbdce5bb52bb568026f`
- Açık receipt'siz claim: `0`
- Pending/recovery job: `0/0`
- Bloklu runtime kaydı: `1`
- Eski Global DoD çalışması korunmuştur; yeniden uygulanmamıştır.
- `GLOBAL_DOD_DURUM.md`: `sha256:2c598eb82ec3ce59d01e39878803d65e7b8d73ddb7b28b703e54a1f84d172ba7`
- `SURUM_RAPORU.md`: `sha256:a78af42dc844464ae30840a82f55d0ea4c5a2c358115c5c7d718249d1d2a4b57`

## Sonraki güvenli adım

Root projection parity doğrulamasını tamamla; ardından tam kabul testlerine geç.

Projection digest: `sha256:5640dc145d7e6c9dad9ba3bc6b3280f11117cd7a0fca09b15ea0fddd49382956`
