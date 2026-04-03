#!/usr/bin/env python3
"""
Experiment 1 — Horizontal Scaling: Worker Parallelism vs. Throughput
=====================================================================
Measures how ETL pipeline throughput and latency change as the number
of concurrent asyncio workers increases from 1 → 2 → 4 → 8.

All provider calls are replaced with async sleep stubs that sample from
lognormal distributions calibrated to the observed baseline latencies
(run_baseline.sh, 2026-03-31):

  Stage            P50 (ms)   P95 (ms)   Source
  ───────────────  ─────────  ─────────  ─────────────────────────────
  OCR (ADI)          5 000     11 000    baseline logs, 27-call sample
  LLM (OpenRouter)   3 718     10 080    baseline logs, 27-call sample
  LLM (CLOD)         3 509      9 242    baseline logs, 27-call sample
  Geocode (Maps)       500      1 200    estimated (not in baseline)

Azure DI concurrency cap is simulated as asyncio.Semaphore(15), mirroring
the documented S0 tier limit. At N ≤ 8 workers this limit is not reached;
run with --workers 1 2 4 8 16 to observe its effect.

Cost: $0.00 — no real API calls are made.

Usage
-----
  # Default: 50 receipts, workers=[1,2,4,8], 3 averaged runs, openrouter latencies
  python experiments/exp1_worker_scaling.py

  # Larger corpus, more runs
  python experiments/exp1_worker_scaling.py --receipts 100 --runs 5

  # Include N=16 to observe ADI rate-limit effect
  python experiments/exp1_worker_scaling.py --workers 1 2 4 8 16

  # Simulate CLOD latency distribution
  python experiments/exp1_worker_scaling.py --provider clod

  # Fast iteration mode (10x speedup, speedup ratios identical to 1.0 scale)
  python experiments/exp1_worker_scaling.py --latency-scale 0.1
"""

import argparse
import asyncio
import json
import math
import random
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Lognormal parameters calibrated to observed baseline data
# ---------------------------------------------------------------------------
# For a lognormal variable X:
#   P50 = exp(mu)           → mu = ln(P50)
#   P95 = exp(mu + 1.645σ)  → sigma = (ln(P95) − ln(P50)) / 1.645

def _ln_params(p50_ms: float, p95_ms: float) -> tuple[float, float]:
    mu    = math.log(p50_ms)
    sigma = (math.log(p95_ms) - mu) / 1.645
    return mu, sigma

_OCR_MU,  _OCR_SIGMA  = _ln_params(p50_ms=5_000, p95_ms=11_000)   # Azure DI
_OR_MU,   _OR_SIGMA   = _ln_params(p50_ms=3_718, p95_ms=10_080)   # OpenRouter
_CLOD_MU, _CLOD_SIGMA = _ln_params(p50_ms=3_509, p95_ms= 9_242)   # CLOD
_GEO_MU,  _GEO_SIGMA  = _ln_params(p50_ms=  500, p95_ms= 1_200)   # Azure Maps

# Azure DI S0 tier: ~15 concurrent requests before throttling
_ADI_MAX_CONCURRENT = 15

# Latency scale factor — set by CLI, applied in mock stages.
# 1.0 = production-realistic (~45 min for full sweep at 50r × 3 runs × 4 worker counts)
# 0.1 = 10x faster for development iteration; speedup ratios are identical
_LATENCY_SCALE = 1.0


# ---------------------------------------------------------------------------
# Mock pipeline stages
# ---------------------------------------------------------------------------

async def _mock_ocr(image_name: str, adi_sem: asyncio.Semaphore) -> tuple[str, float]:
    """
    Simulates Azure Document Intelligence OCR.
    Acquires the ADI concurrency semaphore before sleeping, modelling the
    provider-side concurrency cap. Holds the lock for the duration of the
    simulated OCR call (i.e., while the image is being processed).
    No real API call is made — cost is $0.00.
    """
    async with adi_sem:
        latency_ms = random.lognormvariate(_OCR_MU, _OCR_SIGMA) * _LATENCY_SCALE
        await asyncio.sleep(latency_ms / 1000)
    return f"mock_ocr:{image_name}", latency_ms


async def _mock_llm(ocr_text: str, provider: str) -> tuple[dict, float]:
    """Simulates LLM structuring (OpenRouter or CLOD). No real API call — cost is $0.00."""
    mu, sigma = (_CLOD_MU, _CLOD_SIGMA) if provider == "clod" else (_OR_MU, _OR_SIGMA)
    latency_ms = random.lognormvariate(mu, sigma) * _LATENCY_SCALE
    await asyncio.sleep(latency_ms / 1000)
    return {"storeName": "Mock Store", "items": [{"productName": "Item A"}]}, latency_ms


async def _mock_geocode() -> tuple[float, float, float]:
    """Simulates Azure Maps geocoding. No real API call — cost is $0.00."""
    latency_ms = random.lognormvariate(_GEO_MU, _GEO_SIGMA) * _LATENCY_SCALE
    await asyncio.sleep(latency_ms / 1000)
    return 34.0522, -118.2437, latency_ms


# ---------------------------------------------------------------------------
# Full pipeline for one receipt
# ---------------------------------------------------------------------------

async def _process_one(
    image_name: str,
    provider: str,
    adi_sem: asyncio.Semaphore,
    worker_sem: asyncio.Semaphore,
) -> dict:
    """
    Run the full 3-stage pipeline for one receipt under the worker concurrency
    limit. Returns per-stage timings in milliseconds.
    """
    async with worker_sem:
        t_start = time.monotonic()

        _, ocr_ms = await _mock_ocr(image_name, adi_sem)
        _, llm_ms = await _mock_llm(f"mock_ocr:{image_name}", provider)
        _, _, geo_ms = await _mock_geocode()

        total_ms = (time.monotonic() - t_start) * 1000
        return {
            "image_name": image_name,
            "ocr_ms":     round(ocr_ms,   1),
            "llm_ms":     round(llm_ms,   1),
            "geo_ms":     round(geo_ms,   1),
            "total_ms":   round(total_ms, 1),
        }


# ---------------------------------------------------------------------------
# Single experiment run — all receipts at a fixed worker count
# ---------------------------------------------------------------------------

async def _run_once(
    image_names: list[str],
    n_workers: int,
    provider: str,
) -> dict:
    """
    Process all receipts concurrently, bounded by n_workers.
    Returns aggregate stats for this run.
    """
    adi_sem    = asyncio.Semaphore(_ADI_MAX_CONCURRENT)
    worker_sem = asyncio.Semaphore(n_workers)

    wall_start = time.monotonic()
    results = await asyncio.gather(
        *[_process_one(name, provider, adi_sem, worker_sem) for name in image_names]
    )
    wall_ms = (time.monotonic() - wall_start) * 1000

    def pct(values: list[float], p: int) -> float:
        s   = sorted(values)
        idx = max(0, min(int(len(s) * p / 100), len(s) - 1))
        return round(s[idx], 1)

    totals = [r["total_ms"] for r in results]
    ocrs   = [r["ocr_ms"]   for r in results]
    llms   = [r["llm_ms"]   for r in results]

    return {
        "n_workers":      n_workers,
        "n_receipts":     len(results),
        "wall_time_s":    round(wall_ms / 1000, 2),
        "throughput_rpm": round(len(results) / (wall_ms / 1000) * 60, 1),
        "e2e_p50_ms":     pct(totals, 50),
        "e2e_p95_ms":     pct(totals, 95),
        "ocr_p50_ms":     pct(ocrs, 50),
        "ocr_p95_ms":     pct(ocrs, 95),
        "llm_p50_ms":     pct(llms, 50),
        "llm_p95_ms":     pct(llms, 95),
        "per_receipt":    list(results),
    }


# ---------------------------------------------------------------------------
# Sweep across worker counts, averaging over multiple runs
# ---------------------------------------------------------------------------

async def sweep(
    n_receipts: int,
    worker_counts: list[int],
    n_runs: int,
    provider: str,
) -> list[dict]:
    """
    For each worker count, run the experiment n_runs times and average the
    scalar metrics. Returns one averaged result dict per worker count.
    """
    image_names = [f"receipt_{i:03d}.jpg" for i in range(n_receipts)]
    all_results = []

    for n_workers in worker_counts:
        run_stats = []
        print(f"\n  [N={n_workers} workers]")
        for run_idx in range(n_runs):
            print(f"    run {run_idx + 1}/{n_runs} … ", end="", flush=True)
            stats = await _run_once(image_names, n_workers, provider)
            run_stats.append(stats)
            print(
                f"{stats['throughput_rpm']:.1f} rpm  "
                f"e2e_p50={stats['e2e_p50_ms']:.0f}ms  "
                f"wall={stats['wall_time_s']:.1f}s"
            )

        def avg(key):
            return round(statistics.mean(r[key] for r in run_stats), 1)

        all_results.append({
            "n_workers":      n_workers,
            "n_receipts":     n_receipts,
            "n_runs":         n_runs,
            "provider":       provider,
            "wall_time_s":    round(statistics.mean(r["wall_time_s"]    for r in run_stats), 2),
            "throughput_rpm": avg("throughput_rpm"),
            "e2e_p50_ms":     avg("e2e_p50_ms"),
            "e2e_p95_ms":     avg("e2e_p95_ms"),
            "ocr_p50_ms":     avg("ocr_p50_ms"),
            "ocr_p95_ms":     avg("ocr_p95_ms"),
            "llm_p50_ms":     avg("llm_p50_ms"),
            "llm_p95_ms":     avg("llm_p95_ms"),
            "speedup":        None,  # filled below
        })

    # Speedup relative to N=1 baseline
    baseline_rpm = all_results[0]["throughput_rpm"]
    for r in all_results:
        r["speedup"] = round(r["throughput_rpm"] / baseline_rpm, 2) if baseline_rpm else None

    return all_results


# ---------------------------------------------------------------------------
# Output — table + saved files
# ---------------------------------------------------------------------------

def _print_table(results: list[dict]):
    print()
    header = (
        f"{'Workers':>8}  {'Throughput':>14}  {'Speedup':>8}  "
        f"{'E2E P50':>10}  {'E2E P95':>10}  "
        f"{'OCR P50':>10}  {'LLM P50':>10}  {'Wall (s)':>10}"
    )
    print(header)
    print("─" * len(header))
    for r in results:
        print(
            f"{r['n_workers']:>8}  "
            f"{r['throughput_rpm']:>12.1f}rpm  "
            f"{r['speedup']:>7.2f}x  "
            f"{r['e2e_p50_ms']:>9.0f}ms  "
            f"{r['e2e_p95_ms']:>9.0f}ms  "
            f"{r['ocr_p50_ms']:>9.0f}ms  "
            f"{r['llm_p50_ms']:>9.0f}ms  "
            f"{r['wall_time_s']:>9.1f}s"
        )
    print()


def _save_results(results: list[dict], provider: str):
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    n  = results[0]["n_receipts"]

    # Raw JSON — for further analysis or plotting
    json_path = reports_dir / f"exp1_{provider}_{n}r_{ts}.json"
    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Results (JSON) → {json_path}")

    # Markdown summary
    md_lines = [
        "# Experiment 1 — Worker Parallelism vs. Throughput",
        "",
        f"**Date**: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}  ",
        f"**Provider**: {provider}  ",
        f"**Receipts per run**: {n}  ",
        f"**Runs averaged**: {results[0]['n_runs']}  ",
        "**Cost**: $0.00 (mock simulation — latencies calibrated to 2026-03-31 baseline)  ",
        f"**ADI concurrency cap simulated**: {_ADI_MAX_CONCURRENT} concurrent requests  ",
        "",
        "## Results",
        "",
        "| Workers | Throughput (rpm) | Speedup | E2E P50 (ms) | E2E P95 (ms) | OCR P50 (ms) | LLM P50 (ms) | Wall (s) |",
        "|--------:|----------------:|--------:|-------------:|-------------:|-------------:|-------------:|---------:|",
    ]
    for r in results:
        md_lines.append(
            f"| {r['n_workers']} "
            f"| {r['throughput_rpm']:.1f} "
            f"| {r['speedup']:.2f}x "
            f"| {r['e2e_p50_ms']:.0f} "
            f"| {r['e2e_p95_ms']:.0f} "
            f"| {r['ocr_p50_ms']:.0f} "
            f"| {r['llm_p50_ms']:.0f} "
            f"| {r['wall_time_s']:.1f} |"
        )

    md_lines += [
        "",
        "## Methodology",
        "",
        "Provider calls are replaced with `asyncio.sleep` stubs that draw latency",
        "from lognormal distributions fit to observed P50/P95 values:",
        "",
        f"| Stage | P50 (ms) | P95 (ms) | μ | σ |",
        f"|-------|--------:|--------:|------|------|",
        f"| OCR (Azure DI) | 5000 | 11000 | {_OCR_MU:.3f} | {_OCR_SIGMA:.3f} |",
        f"| LLM (OpenRouter) | 3718 | 10080 | {_OR_MU:.3f} | {_OR_SIGMA:.3f} |",
        f"| LLM (CLOD) | 3509 | 9242 | {_CLOD_MU:.3f} | {_CLOD_SIGMA:.3f} |",
        f"| Geocode (Maps) | 500 | 1200 | {_GEO_MU:.3f} | {_GEO_SIGMA:.3f} |",
        "",
        f"The ADI concurrency cap is modelled as `asyncio.Semaphore({_ADI_MAX_CONCURRENT})`. "
        f"At N ≤ 8 workers this limit is not reached; run with `--workers 1 2 4 8 16` "
        "to observe its throttling effect.",
        "",
        "Speedup is computed relative to the N=1 (sequential) baseline.",
    ]

    md_path = reports_dir / f"exp1_{provider}_{n}r_{ts}.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Report (MD)    → {md_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global _LATENCY_SCALE

    parser = argparse.ArgumentParser(
        description="Experiment 1: Worker parallelism vs. throughput (zero-cost simulation)"
    )
    parser.add_argument(
        "--receipts", type=int, default=49,
        help="Number of simulated receipts per run (default: 49)",
    )
    parser.add_argument(
        "--workers", type=int, nargs="+", default=[1, 2, 4, 8],
        help="Worker counts to sweep (default: 1 2 4 8). "
             "Use --workers 1 2 4 8 16 to observe ADI rate-limit effect.",
    )
    parser.add_argument(
        "--runs", type=int, default=3,
        help="Runs per worker count to average over (default: 3)",
    )
    parser.add_argument(
        "--provider", choices=["openrouter", "clod"], default="openrouter",
        help="LLM provider to simulate — affects LLM latency distribution (default: openrouter)",
    )
    parser.add_argument(
        "--latency-scale", type=float, default=1.0, dest="latency_scale",
        help=(
            "Multiply all simulated latencies by this factor. "
            "1.0 = production-realistic (~45 min full sweep). "
            "0.1 = 10x faster for development; speedup ratios are identical (default: 1.0)."
        ),
    )
    args = parser.parse_args()

    _LATENCY_SCALE = args.latency_scale

    est_min = (args.receipts * 9.2 * args.runs * len(args.workers) * args.latency_scale) / 60
    print(f"\nExperiment 1 — Worker Parallelism vs. Throughput")
    print(f"  receipts={args.receipts}  workers={args.workers}  "
          f"runs={args.runs}  provider={args.provider}  latency-scale={args.latency_scale}")
    print(f"  Cost: $0.00 (mock simulation — no real API calls)")
    print(f"  Estimated runtime: ~{est_min:.0f} min  "
          f"(use --latency-scale 0.1 to run in ~{est_min/10:.0f} min with identical speedup ratios)")

    results = asyncio.run(sweep(
        n_receipts    =args.receipts,
        worker_counts =args.workers,
        n_runs        =args.runs,
        provider      =args.provider,
    ))

    print("\n=== Summary (averaged over runs) ===")
    _print_table(results)
    _save_results(results, args.provider)


if __name__ == "__main__":
    main()
