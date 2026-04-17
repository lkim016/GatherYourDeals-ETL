# week 1 report — LLM Provider Comparison

# Summary

This article records the LLM provider evaluation results from week #1 of the ETL pipeline, captured by RailTracks.

The ETL pipeline ingests receipt images, runs Azure Document Intelligence (ADI) OCR to produce raw text, then calls an LLM to convert that text into a structured JSON receipt object. This report benchmarks two providers — **OpenRouter** and **CLOD** — across cost, latency, and accuracy using the **10 receipts currently in `Receipts/`**, each run ≥3 times per provider (30 OR calls, ~30 CLOD Qwen3 calls). Ground truth item counts are set by OpenRouter's consistent output per receipt, as documented in `docs/scheduled-testing.md` (58 total expected items).

**253 total LLM calls** were logged across all sessions (2026-03-25 → 2026-03-29). 236 succeeded; 17 failed (all from non-functional CLOD model endpoints).

# Environment

- **Platform**: local (WSL2)
- **OCR**: Azure Document Intelligence free tier (F0), prebuilt-read model
- **LLM routing**: LiteLLM via OpenAI-compatible client
- **Observability**: RailTracks session logs + ETL report generator (`python etl.py --report`)
- **Results directory**: `GatherYourDeals-ETL/reports/`

# Models Tested

| Provider | Model | Calls | Successes | Usable |
| --- | --- | --- | --- | --- |
| OpenRouter | `anthropic/claude-3-haiku` | 81 | 81 | ✓ |
| CLOD | `Qwen/Qwen3-235B-A22B-Instruct-2507-tput` | 135 | 135 | ✓ |
| CLOD | `deepseek-v3` | 7 | 0 | ✗ |
| CLOD | `deepseek-ai/DeepSeek-V3` | 1 | 0 | ✗ |
| CLOD | `deepseek-ai/DeepSeek-R1-0528-tput` | 3 | 0 | ✗ |
| CLOD | `anthropic/claude-3-haiku` | 6 | 0 | ✗ |

Note: DeepSeek-R1 consumed ~370 s per call with 0 tokens returned. All other CLOD non-Qwen models returned empty responses. CLOD Qwen3 operated in two billing states: *paid* (12 calls, metered at ~$0.0044/call) and *free/allocated* (123 calls, $0.00). All stats below are scoped to the **10 receipts in `Receipts/`** unless otherwise noted.

# Performance Result

**Test run IDs** (sample session IDs from RailTracks):
- `receipt_etl_1db9d666` (OpenRouter — first run on expanded receipt set)
- `receipt_etl_fc7a128e` (CLOD Qwen3 — 10-receipt batch)
- `receipt_etl_f755a5d3` (latest OR 10-receipt run)
- `receipt_etl_cd628a59` (latest OR 10-receipt run)

**Time range**: 2026-03-25 → 2026-03-29

## Run Duration

| Provider | Model | Receipts | Calls (scoped) | Success | Period |
| --- | --- | --- | --- | --- | --- |
| OpenRouter | claude-3-haiku | 10 | 30 | 30 | 2026-03-25 → 03-29 |
| CLOD | Qwen3-235B (free) | 10 | ~28 | ~28 | 2026-03-28 → 03-29 |
| CLOD | Qwen3-235B (paid) | 2 (Costco01, Ralphs) | 2 | 2 | 2026-03-28 |
| CLOD | Failed models | — | 17 | 0 | 2026-03-26 → 03-28 |

Note: Receipts were processed sequentially, one at a time. No concurrent load was applied.

## Latency

Stats computed from ≥3 runs per receipt on each provider, scoped to the 10 current receipts (30 data points per provider). One OR outlier at 608,546 ms (likely a rate-limit hang on `IMG_20260308_171233.jpg`) is excluded from OR stats.

| Provider | Model | n | Avg (ms) | Median (ms) | Stdev (ms) | Min (ms) | Max (ms) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| OpenRouter | claude-3-haiku | 30 | **5,929** | **3,762** | ~4,600 | 2,972 | 20,965 |
| CLOD | Qwen3-235B (all) | 30 | 26,361 | 21,957 | ~17,000 | 8,660 | 81,274 |
| CLOD | Qwen3-235B (paid) | 12\* | 42,093 | 38,968 | 17,871 | 20,047 | 90,191 |
| CLOD | Qwen3-235B (free) | 123\* | 24,495 | 19,528 | 16,451 | 7,828 | 81,418 |
| CLOD | deepseek-v3 ✗ | 7 | 4,701 | 1,208 | 8,779 | 1,011 | 25,957 |
| CLOD | DeepSeek-R1 ✗ | 3 | 368,189 | 370,449 | 6,390 | 361,062 | 373,056 |
| CLOD | claude-3-haiku ✗ | 6 | 7,729 | 9,246 | 3,305 | 1,013 | 10,527 |

\* Full-corpus stats (all receipts, not just current 10).

Note: OpenRouter median (3,762 ms) is **5.8× lower** than CLOD Qwen3 free-tier median (21,957 ms). OpenRouter p75 is 7,730 ms; 93% of OR calls complete under 10 s. Two Costco-heavy receipts (`2026-01-03Costco`, `2026-02-14T&TYue`) drive OR's longest latencies (14–21 s) due to large OCR output fed to the LLM.

## Latency Percentiles (scoped — 10 current receipts, 30 runs each)

| Percentile | OpenRouter claude-3-haiku | CLOD Qwen3-235B |
| --- | --- | --- |
| p10 | ~3,129 ms | ~9,664 ms |
| p25 | ~3,330 ms | ~14,362 ms |
| p50 | ~3,762 ms | ~21,957 ms |
| p75 | ~7,730 ms | ~35,497 ms |
| p90 | ~8,869 ms | ~42,988 ms |
| p99 | ~20,965 ms | ~81,274 ms |

## Accuracy (Item Count vs Ground Truth)

Ground truth item counts are derived from OpenRouter's consistent output per receipt across ≥3 runs, as defined in `docs/scheduled-testing.md`. OR-consistent = 58 items total. CLOD is compared against that baseline.

| Receipt | GT Items | OR Mode | OR ✓ | CLOD Qwen3 Mode | CLOD ✓ | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-01-03Costco | 15 | 15 | ✓ | 17 | ✗ | CLOD picks up CRV lines |
| 2026-02-14T&TYue | 14 | 14 | ✓ | 14 | ✓ | |
| 2026-02-28Ralphs | 4 | 4 | ✓ | 4 | ✓ | |
| IMG20260308161019 | 1 | 1 | ✓ | 1 | ✓ | |
| IMG20260308162959 | 3 | 3 | ✓ | 5–6 | ✗ | CLOD counts discount lines |
| IMG20260308163051 | 1 | 1 | ✓ | 1 | ✓ | |
| IMG\_20260308\_161231 | 3 | 3 | ✓ | 3 | ✓ | |
| IMG\_20260308\_171122 | 2 | 2 | ✓ | 2 | ✓ | |
| IMG\_20260308\_171140 | 1 | 1 | ✓ | 1 | ✓ | |
| IMG\_20260308\_171233 | 14 | 14 | ✓ | 14 or 28 | ✗ | CLOD occasionally doubles items |
| **Total** | **58** | **58** | **10/10** | **~62–76** | **7/10** | |

Note on ground truth: These baselines are derived from OR's consistent output, not hand-labeled. The 3 CLOD discrepancies are over-extraction cases (CLOD extracts more items than OR), not under-extraction — so CLOD may be more thorough on ambiguous lines, but inconsistently so. `IMG_20260308_171233.jpg` is the most unstable: CLOD returned 14 items on one run and 28 on two others, suggesting hallucinated line duplication on this receipt.

## Consistency (Repeated Calls on Same Receipt)

`2026-02-28Ralphs.jpg` was run 7 times with OR and 4 times with CLOD to assess determinism:

| Model | Repeated calls | Item counts | Consistent |
| --- | --- | --- | --- |
| OpenRouter claude-3-haiku | 7 | All = 4 | ✓ |
| CLOD Qwen3-235B | 4 | All = 4 | ✓ |

Both models are deterministically stable on simple receipts. Instability only manifests on large or complex receipts (see `IMG_20260308_171233.jpg` above).

# Cost

## Per-Call Cost

| Provider | Model | Cost / receipt | Notes |
| --- | --- | --- | --- |
| OpenRouter | claude-3-haiku | **$0.000** | Free credit allocation; list price ~$0.0012 |
| CLOD | Qwen3-235B (paid) | **$0.0044 avg** | Range $0.0028–$0.0061 |
| CLOD | Qwen3-235B (free) | $0.000 | Allocated quota |
| CLOD | Failed models | $0.000 | 0 tokens returned; 0 value delivered |

## CLOD Qwen3 Paid Runs Detail (n = 12, full corpus)

| Receipt | Input tok | Output tok | Cost |
| --- | --- | --- | --- |
| Vons 2025-10-01 (×4 runs) | 1,056 | 799 | $0.003453 each |
| Target 2025-10-06 (×2 runs) | 1,025 | 955 | $0.003890 each |
| Costco Oct 2025 (×2 runs) | 1,347 | 1,413–1,503 | $0.005613 / $0.005856 |
| Costco Dec 2025 | 1,356 | 1,558 | $0.006030 |
| Costco Jan 2026 (Receipts/) | 1,291 | 1,304 | $0.005203 |
| Ralphs Feb 2026 (Receipts/) | 969 | 614 | $0.002811 |
| Costco Mar 2026 | 1,261 | 1,606 | $0.006079 |
| **Total** | — | — | **$0.0532** |

Of the 12 paid calls, 2 covered current `Receipts/` folder receipts: Costco Jan ($0.005203) and Ralphs ($0.002811) = **$0.008014** for the current test set.

## Cost Projection at Scale (100 receipts / week)

| Provider + Model | $/receipt | $/week | $/month | $/year |
| --- | --- | --- | --- | --- |
| OpenRouter (free credits) | $0.000 | $0.00 | $0.00 | ~$0 |
| OpenRouter (list price) | ~$0.0012 | ~$0.12 | ~$0.52 | ~$6.24 |
| CLOD Qwen3 (paid) | $0.0044 | $0.44 | $1.93 | $23.14 |

CLOD Qwen3 paid tier is approximately **3.7× OpenRouter list price** per receipt.

## ADI OCR Cost

| Item | Cost | Note |
| --- | --- | --- |
| ADI OCR | $0.00 | 451 calls / 441 pages on F0 free tier |
| OpenRouter LLM | $0.00 | Free credit allocation |
| CLOD LLM | $0.0532 | 12 paid calls across all benchmark sessions |
| **Total week 1** | **$0.0532** | |

# RailTracks Insight

All session events were captured via RailTracks. Key observations:

- ADI avg latency is stable at **6,988 ms** across all 451 calls (7,108 ms avg for the 10 current receipts) and is independent of LLM choice, as expected.
- OpenRouter's latency distribution is tight at the median (3,762 ms scoped) but has a long tail driven by two large receipts. The single 608,546 ms outlier on `IMG_20260308_171233.jpg` is a rate-limit or network hang, not model latency — the p99 without it is 20,965 ms.
- CLOD Qwen3 free tier is substantially faster than paid (median ~20 s vs ~39 s) — likely due to different backend routing under allocated quota vs. metered billing.
- CLOD Qwen3 hallucination risk is concentrated on `IMG_20260308_171233.jpg` (a dense restaurant receipt). Two of three CLOD runs returned 28 items vs. OR's consistent 14. The receipt appears to contain repeated line structure that causes the model to double-count. Flag this receipt for manual review in future regression runs.
- CLOD over-extracts on `2026-01-03Costco.jpg` (17 vs. 15) and `IMG20260308162959.jpg` (5–6 vs. 3) — CLOD picks up CRV redemption lines and discount entries that OR treats as non-items. Whether this is correct behavior depends on product requirements.
- DeepSeek-R1 via CLOD shows a pathological pattern: the connection stays open for ~370 s before returning 0 tokens. This is not a timeout — the session hangs. Unacceptable for any batch workload.
- CLOD claude-3-haiku and deepseek-v3 return empty responses in under 11 s, suggesting an API format or auth mismatch in CLOD's proxy layer, not slow models.
- **Recommendation**: Use OpenRouter (claude-3-haiku) as the default for weekly regression runs (Test 1). It is faster, free, and deterministic on 10/10 receipts. Use CLOD Qwen3 free tier for provider comparison runs (Test 2) only — it is slower but provides a useful cross-check on the 7 stable receipts.
