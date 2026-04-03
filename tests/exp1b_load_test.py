#!/usr/bin/env python3
"""
Experiment 1B — Live Load Test: Worker Parallelism vs. Throughput
=================================================================
Sends real POST /process requests to the deployed ETL service and measures
end-to-end HTTP latency and throughput as concurrent request count scales.

This is the live counterpart to exp1_worker_scaling.py (Phase A simulation).
Real API calls are made — Azure DI, LLM, and geocoding costs apply.

Default cost estimate (CLOD, 9 receipts × 4 concurrency levels × 2 runs):
  ~72 receipts × $0.0003/receipt ≈ $0.02 total

Usage
-----
  # Against local service (start with: uvicorn app:app --port 8080)
  python experiments/exp1b_load_test.py --url http://localhost:8080

  # Against deployed Azure Container Apps service
  python experiments/exp1b_load_test.py --url https://gyd-etl.<region>.azurecontainerapps.io

  # Use OpenRouter instead of CLOD (~$0.27 for same run)
  python experiments/exp1b_load_test.py --url http://localhost:8080 --provider openrouter

  # Sweep specific concurrency levels
  python experiments/exp1b_load_test.py --url http://localhost:8080 --concurrency 1 2 4

  # More runs per concurrency level for tighter confidence intervals
  python experiments/exp1b_load_test.py --url http://localhost:8080 --runs 3

Environment
-----------
  ETL_SERVICE_URL — default service URL (overridden by --url)
"""

import argparse
import asyncio
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RECEIPTS_DIR = Path(__file__).parent.parent / "Receipts"


def _collect_receipts(n: int) -> list[Path]:
    """
    Return up to n receipt images from Receipts/ (excluding Unused/).
    Cycles through available files if n exceeds the number on disk.
    """
    available = sorted(
        p for p in RECEIPTS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".heic"}
    )
    if not available:
        raise FileNotFoundError(f"No receipt images found in {RECEIPTS_DIR}")
    # Cycle to fill requested count
    return [available[i % len(available)] for i in range(n)]


def _percentile(values: list[float], p: int) -> float:
    s = sorted(values)
    idx = max(0, min(int(len(s) * p / 100), len(s) - 1))
    return round(s[idx], 1)


# ---------------------------------------------------------------------------
# Single request
# ---------------------------------------------------------------------------

async def _post_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    url: str,
    image_path: Path,
    provider: str,
    user: str,
) -> dict:
    """
    POST one receipt image to /process. Returns timing and outcome.
    The semaphore bounds how many requests are in-flight simultaneously.
    """
    async with sem:
        t0 = time.monotonic()
        status = None
        error = None
        items = 0
        try:
            with open(image_path, "rb") as f:
                resp = await client.post(
                    f"{url}/process",
                    files={"image": (image_path.name, f, "image/jpeg")},
                    params={"user": user, "provider": provider},
                    timeout=120,
                )
            status = resp.status_code
            if resp.status_code == 200:
                data = resp.json()
                items = len(data) if isinstance(data, list) else 0
            else:
                error = f"HTTP {resp.status_code}"
        except Exception as exc:
            error = str(exc)

        latency_ms = (time.monotonic() - t0) * 1000
        return {
            "image":      image_path.name,
            "latency_ms": round(latency_ms, 1),
            "status":     status,
            "items":      items,
            "success":    error is None,
            "error":      error,
        }


# ---------------------------------------------------------------------------
# One concurrency-level run
# ---------------------------------------------------------------------------

async def _run_once(
    url: str,
    receipts: list[Path],
    concurrency: int,
    provider: str,
    user: str,
) -> dict:
    sem = asyncio.Semaphore(concurrency)
    wall_start = time.monotonic()

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[_post_one(client, sem, url, img, provider, user) for img in receipts]
        )

    wall_ms = (time.monotonic() - wall_start) * 1000
    latencies = [r["latency_ms"] for r in results if r["success"]]
    n_ok = sum(1 for r in results if r["success"])
    n_fail = len(results) - n_ok

    return {
        "concurrency":    concurrency,
        "n_receipts":     len(results),
        "n_ok":           n_ok,
        "n_fail":         n_fail,
        "wall_time_s":    round(wall_ms / 1000, 2),
        "throughput_rpm": round(n_ok / (wall_ms / 1000) * 60, 1) if wall_ms > 0 else 0,
        "e2e_p50_ms":     _percentile(latencies, 50) if latencies else None,
        "e2e_p95_ms":     _percentile(latencies, 95) if latencies else None,
        "e2e_avg_ms":     round(statistics.mean(latencies), 1) if latencies else None,
        "per_request":    list(results),
    }


# ---------------------------------------------------------------------------
# Sweep across concurrency levels
# ---------------------------------------------------------------------------

async def sweep(
    url: str,
    receipts: list[Path],
    concurrency_levels: list[int],
    n_runs: int,
    provider: str,
    user: str,
) -> list[dict]:
    all_results = []

    for c in concurrency_levels:
        run_stats = []
        print(f"\n  [concurrency={c}]")
        for run_idx in range(n_runs):
            print(f"    run {run_idx + 1}/{n_runs} … ", end="", flush=True)
            stats = await _run_once(url, receipts, c, provider, user)
            run_stats.append(stats)
            ok_str = f"{stats['n_ok']}/{stats['n_receipts']} ok"
            print(
                f"{ok_str}  "
                f"{stats['throughput_rpm']:.1f} rpm  "
                f"p50={stats['e2e_p50_ms']}ms  "
                f"wall={stats['wall_time_s']:.1f}s"
            )
            # Brief pause between runs to avoid back-to-back hammering
            if run_idx < n_runs - 1:
                await asyncio.sleep(2)

        def avg(key):
            vals = [r[key] for r in run_stats if r[key] is not None]
            return round(statistics.mean(vals), 1) if vals else None

        all_results.append({
            "concurrency":    c,
            "n_receipts":     receipts.__len__(),
            "n_runs":         n_runs,
            "provider":       provider,
            "n_ok":           round(statistics.mean(r["n_ok"]   for r in run_stats), 1),
            "n_fail":         round(statistics.mean(r["n_fail"] for r in run_stats), 1),
            "wall_time_s":    avg("wall_time_s"),
            "throughput_rpm": avg("throughput_rpm"),
            "e2e_p50_ms":     avg("e2e_p50_ms"),
            "e2e_p95_ms":     avg("e2e_p95_ms"),
            "e2e_avg_ms":     avg("e2e_avg_ms"),
            "speedup":        None,
        })

    # Speedup relative to concurrency=1 baseline
    baseline_rpm = all_results[0]["throughput_rpm"]
    for r in all_results:
        r["speedup"] = round(r["throughput_rpm"] / baseline_rpm, 2) if baseline_rpm else None

    return all_results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _print_table(results: list[dict]):
    print()
    header = (
        f"{'Concurrency':>12}  {'Throughput':>14}  {'Speedup':>8}  "
        f"{'E2E P50':>10}  {'E2E P95':>10}  {'Failures':>9}  {'Wall (s)':>10}"
    )
    print(header)
    print("─" * len(header))
    for r in results:
        fails = f"{r['n_fail']:.1f}" if r["n_fail"] else "0"
        print(
            f"{r['concurrency']:>12}  "
            f"{r['throughput_rpm']:>12.1f}rpm  "
            f"{r['speedup']:>7.2f}x  "
            f"{r['e2e_p50_ms']:>9}ms  "
            f"{r['e2e_p95_ms']:>9}ms  "
            f"{fails:>9}  "
            f"{r['wall_time_s']:>9.1f}s"
        )
    print()


def _save_results(results: list[dict], provider: str, url: str):
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    n = results[0]["n_receipts"]

    json_path = reports_dir / f"exp1b_{provider}_{n}r_{ts}.json"
    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Results (JSON) → {json_path}")

    md_lines = [
        "# Experiment 1B — Live Load Test: Worker Parallelism vs. Throughput",
        "",
        f"**Date**: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}  ",
        f"**Service URL**: `{url}`  ",
        f"**Provider**: {provider}  ",
        f"**Receipts per run**: {n}  ",
        f"**Runs averaged**: {results[0]['n_runs']}  ",
        "**Cost**: real API calls (Azure DI + LLM + geocoding)  ",
        "",
        "## Results",
        "",
        "| Concurrency | Throughput (rpm) | Speedup | E2E P50 (ms) | E2E P95 (ms) | Avg Failures | Wall (s) |",
        "|------------:|----------------:|--------:|-------------:|-------------:|-------------:|---------:|",
    ]
    for r in results:
        md_lines.append(
            f"| {r['concurrency']} "
            f"| {r['throughput_rpm']:.1f} "
            f"| {r['speedup']:.2f}x "
            f"| {r['e2e_p50_ms']} "
            f"| {r['e2e_p95_ms']} "
            f"| {r['n_fail']:.1f} "
            f"| {r['wall_time_s']:.1f} |"
        )

    md_lines += [
        "",
        "## Methodology",
        "",
        "Each concurrency level sends all receipts as concurrent `POST /process` requests",
        "bounded by an `asyncio.Semaphore`. Wall time and per-request HTTP response latency",
        "are measured. Results are averaged over multiple runs.",
        "",
        "Speedup is computed relative to concurrency=1 (sequential) baseline.",
        "",
        "Unlike Phase A (exp1_worker_scaling.py), this test makes real API calls —",
        "Azure Document Intelligence OCR, LLM structuring, and Azure Maps geocoding.",
        "All costs are tracked in the structured JSONL logs (`logs/etl_YYYY-MM-DD.jsonl`).",
    ]

    md_path = reports_dir / f"exp1b_{provider}_{n}r_{ts}.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Report (MD)    → {md_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    default_url = os.getenv("ETL_SERVICE_URL", "http://localhost:8080")
    default_user = os.getenv("GYD_USERNAME", "loadtest")

    parser = argparse.ArgumentParser(
        description="Experiment 1B: Live load test — real POST /process requests"
    )
    parser.add_argument(
        "--url", default=default_url,
        help=f"ETL service base URL (default: {default_url})",
    )
    parser.add_argument(
        "--receipts", type=int, default=9,
        help="Number of receipts per run — cycles through Receipts/ if needed (default: 9)",
    )
    parser.add_argument(
        "--concurrency", type=int, nargs="+", default=[1, 2, 4, 8],
        help="Concurrency levels to sweep (default: 1 2 4 8)",
    )
    parser.add_argument(
        "--runs", type=int, default=2,
        help="Runs per concurrency level to average (default: 2)",
    )
    parser.add_argument(
        "--provider", choices=["openrouter", "clod"], default="clod",
        help="LLM provider — passed as query param to the service (default: clod)",
    )
    parser.add_argument(
        "--user", default=default_user,
        help=f"Username written into output metadata (default: {default_user})",
    )
    args = parser.parse_args()

    receipts = _collect_receipts(args.receipts)

    n_total = len(receipts) * len(args.concurrency) * args.runs
    provider_cost = 0.0003 if args.provider == "clod" else 0.0038
    est_cost = n_total * provider_cost

    print(f"\nExperiment 1B — Live Load Test")
    print(f"  url={args.url}")
    print(f"  receipts={len(receipts)}  concurrency={args.concurrency}  "
          f"runs={args.runs}  provider={args.provider}")
    print(f"  Total requests: {n_total}")
    print(f"  Estimated cost: ~${est_cost:.4f} "
          f"(${provider_cost}/receipt × {n_total} receipts)")
    print()

    results = asyncio.run(sweep(
        url=args.url,
        receipts=receipts,
        concurrency_levels=args.concurrency,
        n_runs=args.runs,
        provider=args.provider,
        user=args.user,
    ))

    print("\n=== Summary (averaged over runs) ===")
    _print_table(results)
    _save_results(results, args.provider, args.url)


if __name__ == "__main__":
    main()
