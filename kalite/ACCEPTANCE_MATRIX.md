# Zekam Acceptance Matrix

| Alan | Pozitif kabul | Negatif kabul | Kanıt |
|---|---|---|---|
| Başlangıç | Yeni model `00_BASLA` ile devam eder | Transcript olmadan kaybolmaz | E2E handoff |
| Proje alias | `gpu` exact projeye çözülür | İki eşit aday mutation yapmaz | resolver tests |
| Work | Defect revision/history | Vector status belirleyemez | DB/RLS tests |
| Subagent | Agentic işte child envelope | Coordinator child sayılmaz | harness tests |
| Paralellik | Ayrık iki step parallel | Aynı path write parallel değil | concurrency |
| Lease/fence | Current owner complete | Old fence reddedilir | PostgreSQL |
| Claim/receipt | Completed exact receipt | Claim/no receipt retry yok | crash E2E |
| Verifier | Bağımsız pass | Builder self-pass reddi | schema/DB |
| Sandbox | Allowlist patch | traversal/symlink/network deny | security |
| Inventory | 20 unique Model ID | duplicate ID reject | import |
| Health | tip-specific probe | health=capability değil | contract |
| Benchmark | 5+ trials | unsafe trial average ile gizlenmez | runner |
| Quota | trusted <%40/<%30 fallback | unknown guessed değil | router |
| Handoff | new client resume | active lease/approval taşınmaz | E2E |
| RAG | exact+FTS+dense+RRF | stale index kullanılmaz | golden |
| Citation | page/heading/line/object | uydurma locator yok | E2E |
| OCR | Türkçe image searchable | low confidence görünür | fixture |
| Code ingest | symbol/path incremental | code execute edilmez | security |
| DB metadata | object/dependency | row data default yok | adapter |
| Memory | reviewed scope search | raw output active olmaz | lifecycle |
| Mem0 | optional sync/fallback | second authority değil | adapter |
| Skill | evaluated approved active | self-promotion yok | lifecycle |
| Secret | adapter uses SecretRef | prompt/log/vector leak yok | canary |
| Scheduler | idempotent night job | duplicate effect yok | clock/E2E |
| Dashboard | read-only projection | bypass mutation yok | API |
| Backup | clean restore | active lease restored değil | DR |
| Commit | Turkish ASCII | non-ASCII/meaningless reject | hook |
| Rename | exact migration | old repo exists → blocked | readiness |
