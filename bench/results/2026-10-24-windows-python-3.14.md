# Benchmark snapshot — Windows / Python 3.14

Run date: 2026-10-24. Host: Windows 11 x64, Python 3.14.6, SQLite 3.50.4. All records were synthetic and held in temporary SQLite databases. This is a local snapshot, not a cross-machine performance target.

| Global records | Project records | DB size | FTS search p95 | Hook empty project p95 | Hook populated project p95 | Candidate dedupe p95 | MCP start + list p95* | MCP search p95* | Migration v5→v6 p95** | Backup p95 | Restore dry-run p95 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 10 | 0.75 MB | 1.63 ms | 5.28 ms | 6.90 ms | 6.60 ms | 961 ms | 59.99 ms | 125 ms | 139 ms | 249 ms |
| 10,000 | 100 | 6.49 MB | 4.76 ms | 8.11 ms | 18.96 ms | 8.82 ms | 942 ms | 7.04 ms | 241 ms | 288 ms | 526 ms |
| 100,000 | 1,000 | 64.85 MB | 12.07 ms | 17.86 ms | 61.13 ms | 7.48 ms | 1,034 ms | 12.11 ms | 858 ms | 1,108 ms | 1,484 ms |

`p95` uses 15 samples for search, hooks, initialization and dedupe; 3 for write, backup, restore and migration; MCP uses 2 fresh server launches per size, so its p95 is only a rough indicator. Migration timing includes copying the synthetic database before applying v6. DB size includes both global and project rows.

The benchmark measures both a new project namespace and one populated with 1% as many approved project records as global records. At 100k global rows, hook p95 was 17.86 ms with no project records and 61.13 ms with 1,000 project records. The populated result remains slower and should guide future profiling; no latency target was specified by the audit.

Earlier hook measurements in this file used synthetic content that placed the word `fixture` in every global record. Since prompt retrieval uses prefix matching, the benchmark prompt token `Fix` matched `fixture` and caused broad irrelevant results. Those earlier hook timings are invalid and are not comparable to this corrected run. The current fixture uses non-overlapping global content and explicitly checks that the prompt retrieves project-scoped records only in the populated case.

The v6 namespace/id index was added to speed candidate dedupe. An earlier v5 run reported 347.61 ms p95 and the corrected v6 run reports 7.48 ms on 100k global rows, but the synthetic content changed between runs; this is indicative evidence, not an apples-to-apples percentage comparison.

FTS5 was available at all sizes. Backup verification passed, and restore dry-run preserved all integer knowledge IDs. The complete JSON output is reproducible with:

```powershell
.venv\Scripts\python.exe -m bench.benchmark_memory
```
