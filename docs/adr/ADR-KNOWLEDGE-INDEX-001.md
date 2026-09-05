# ADR-KNOWLEDGE-INDEX-001: Local knowledge index selection

- Status: Accepted for bounded macOS fixture; Windows/scale deferred
- Date: 2026-09-02
- Authority: `AKTIF_GOREV.md` WP-01 and technology gate 8.2

## Context

The required production knowledge index must be persistent and offline, apply
project/realm filters before candidate limiting, bind every hit back to source
and citation locators, isolate embedding profiles, activate generations
atomically, detect stale/corrupt state, and rebuild from source manifests. A
successful import or vector query is insufficient.

The shortlist is LanceDB local, Zvec in-process, and SQLite FTS5 plus
`sqlite-vec`. Qdrant local/edge is a reference candidate only if the three main
candidates do not produce a clear decision. Brute-force or feature-hash vectors
are test baselines, not semantic production engines.

## Measured evidence

With explicit user approval, LanceDB 0.37.1, Zvec 0.7.0 and sqlite-vec 0.1.9
were installed in isolated temporary Mac environments. Production dependencies
were not changed. The corpus contains 132 distinct chunks from current Zekam
sources and real 1024-dimensional `BAAI/bge-m3` vectors obtained from the
loopback Infinity provider. It covers Turkish, English, PL/SQL identifiers,
paths/functions, Jira IDs, typos, semantic paraphrase, exact/semantic conflict,
no-answer and cross-project leakage. Feature-hash vectors were not used.

All three candidates passed Mac persistence/restart, project prefiltering,
citation binding, atomic generation activation, update, offline execution and
50,000-row rebuild/corruption probes. At 250,001 rows, all achieved fusion
Recall@10 1.0, MRR 0.9375, citation precision 1.0 and no-answer
precision/recall 1.0. Their measured build / p50 / p95 / footprint results were:

| Candidate | Build | p50 | p95 | Footprint |
|---|---:|---:|---:|---:|
| LanceDB | 16.47 s | 11.49 ms | 72.59 ms | 2.11 GB |
| sqlite-vec | 54.98 s | 183.78 ms | 199.14 ms | 1.56 GB |
| Zvec | 12.10 s | 44.57 ms | 3229.43 ms | 1.08 GB |

The scale profile cyclically repeats the 132 real vectors; it measures actual
record scale, persistence and retrieval mechanics, but not 250,000 unique
semantic neighborhoods. LanceDB emitted empty-cluster warnings under this
duplicate-heavy profile. These limitations remain part of the evidence.

Persistent SQLite FTS5 supplied exact/lexical retrieval. At 250,000 rows it
retained lexical Recall@10 0.75 and exact Top-1 1.0, but duplicate-heavy OR/BM25
queries measured p95 2801.86 ms. Lexical no-answer precision alone was 0.333;
the combined dense/exact/lexical fusion achieved 1.0 precision and recall.

The existing user-modified project knowledge index is not acceptance evidence:
its write path uses real BGE while the query path still contains a feature-hash
vector route, so the two paths can occupy different vector spaces. WP-06 must
not treat that fallback as semantic dense retrieval.

## Decision

For the user-approved Mac-first phase, select SQLite FTS5 plus sqlite-vec as the
provisional knowledge stack. It keeps exact, lexical and vector generations
inside one familiar recovery boundary and has the smallest direct production
dependency surface. Its measured 250,000-row latency is a declared risk, not a
hidden pass. Current implementation work uses only a small, source-verifiable
real-BGE fixture; it does not embed the user's full corpus or claim scale
acceptance. Global selection remains blocked on:

1. Windows installation and runtime evidence;
2. Windows parity for query quality, isolation, crash/restart and rebuild;
3. deferred large-corpus and stress measurements.

## Consequences

- `pyproject.toml` remains unchanged.
- Documentation claims and wheel presence are never scored as runtime passes.
- The existing deterministic vector fallback cannot win the bake-off.
- WP-06 may reuse corpus and grading fixtures, but cannot bypass this ADR.
