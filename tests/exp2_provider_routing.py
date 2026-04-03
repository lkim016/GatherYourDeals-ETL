#!/usr/bin/env python3
"""
Experiment 2 — Provider Routing Strategy Under Concurrent Load
==============================================================
Compares three LLM load-balancing strategies on the same 27-receipt batch
(9 receipts × 3 runs) at N=4 concurrent workers.  Measures cost/receipt,
field-level accuracy, throughput, and provider utilisation split.

Strategies
----------
  single       All traffic to OpenRouter (current single-provider baseline)
  round-robin  Alternate between OpenRouter and CLOD per receipt
  cost-aware   Route to CLOD first; fall back to OpenRouter if CLOD fails
               OR if the extracted item count falls below a confidence
               threshold (< 2 items extracted → low confidence)

The ProviderRouter wraps the existing _structure_openrouter() and
_structure_clod() calls in etl.py, so all logging and cost tracking are
unchanged.

Usage
-----
  # All three strategies, 9 receipts, 3 runs each
  python experiments/exp2_provider_routing.py

  # Specific strategy only
  python experiments/exp2_provider_routing.py --strategy cost-aware

  # More receipts (cycles through Receipts/ as needed)
  python experiments/exp2_provider_routing.py --receipts 27 --workers 4

Cost estimate (default run)
---------------------------
  single:      27 × $0.0038 = $0.10
  round-robin: ~14 CLOD + ~13 OR = ~$0.054
  cost-aware:  ~27 × $0.0003 = $0.008  (most fall through on CLOD)
  Total worst-case: ~$0.16
"""

import argparse
import asyncio
import itertools
import json
import math
import os
import random
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Path setup — allow running from repo root or experiments/
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
import sys
sys.path.insert(0, str(_REPO_ROOT))

import etl as _etl

RECEIPTS_DIR = _REPO_ROOT / "Receipts"
REPORTS_DIR  = _REPO_ROOT / "reports"

# ---------------------------------------------------------------------------
# Provider router
# ---------------------------------------------------------------------------

class ProviderRouter:
    """
    Pluggable router that wraps _structure_openrouter() and _structure_clod().

    Each call to route() selects a provider per the configured strategy and
    returns (result_dict, provider_used, model_used).
    """

    _LOW_CONFIDENCE_ITEMS = 2  # fewer than this → cost-aware falls back to OR

    def __init__(self, strategy: str):
        if strategy not in ("single", "round-robin", "cost-aware"):
            raise ValueError(f"Unknown strategy: {strategy!r}")
        self.strategy = strategy
        self._rr_counter = itertools.count()   # round-robin sequence

    def route(self, ocr_text: str) -> tuple[dict, str, str]:
        """
        Run LLM structuring for one receipt.

        Returns:
            (result, provider_used, model_used)
        """
        if self.strategy == "single":
            return self._call_openrouter(ocr_text)

        if self.strategy == "round-robin":
            idx = next(self._rr_counter)
            if idx % 2 == 0:
                return self._call_openrouter(ocr_text)
            else:
                return self._call_clod(ocr_text)

        if self.strategy == "cost-aware":
            # Try CLOD first; fall back to OpenRouter on failure or low confidence
            try:
                result, provider, model = self._call_clod(ocr_text)
                n_items = len(result.get("items") or [])
                if n_items >= self._LOW_CONFIDENCE_ITEMS:
                    return result, provider, model
                # Low confidence — retry with OpenRouter
            except Exception:
                pass
            return self._call_openrouter(ocr_text)

        raise RuntimeError("unreachable")

    # --- Provider wrappers --------------------------------------------------

    @staticmethod
    def _call_openrouter(ocr_text: str) -> tuple[dict, str, str]:
        model = _etl.OR_DEFAULT_MODEL
        result, _pt, _ct, _gen_id = _etl._structure_openrouter(ocr_text, model)
        return result, "openrouter", model

    @staticmethod
    def _call_clod(ocr_text: str) -> tuple[dict, str, str]:
        model = _etl.CLOD_DEFAULT_MODEL
        result, _pt, _ct, _cost = _etl._structure_clod(ocr_text, model)
        return result, "clod", model


# ---------------------------------------------------------------------------
# Single receipt processing
# ---------------------------------------------------------------------------

async def _process_one(
    sem: asyncio.Semaphore,
    image_path: Path,
    router: ProviderRouter,
    user: str,
) -> dict:
    """
    Process one receipt through OCR + routed LLM + geocode.
    Returns a result dict with timing, provider used, item count, and success.
    """
    async with sem:
        t0 = time.monotonic()
        error = None
        provider_used = None
        model_used = None
        n_items = 0

        try:
            # OCR (blocking — run in thread pool to not block event loop)
            loop = asyncio.get_running_loop()
            run_id = f"exp2-{image_path.stem}-{int(t0*1000)}"
            ocr_text = await loop.run_in_executor(None, _etl.ocr, image_path)

            # LLM routing (blocking)
            result, provider_used, model_used = await loop.run_in_executor(
                None, router.route, ocr_text
            )
            n_items = len(result.get("items") or [])

            # Geocode (blocking)
            await loop.run_in_executor(None, _etl.geocode, result)

        except Exception as exc:
            error = str(exc)

        latency_ms = (time.monotonic() - t0) * 1000
        return {
            "image":        image_path.name,
            "latency_ms":   round(latency_ms, 1),
            "provider":     provider_used,
            "model":        model_used,
            "n_items":      n_items,
            "success":      error is None,
            "error":        error,
        }


# ---------------------------------------------------------------------------
# One strategy run
# ---------------------------------------------------------------------------

async def _run_once(
    receipts: list[Path],
    strategy: str,
    workers: int,
    user: str,
) -> dict:
    sem = asyncio.Semaphore(workers)
    router = ProviderRouter(strategy)
    wall_start = time.monotonic()

    results = await asyncio.gather(
        *[_process_one(sem, img, router, user) for img in receipts]
    )

    wall_ms = (time.monotonic() - wall_start) * 1000
    latencies = [r["latency_ms"] for r in results if r["success"]]
    n_ok = sum(1 for r in results if r["success"])

    # Provider utilisation
    provider_counts: dict[str, int] = {}
    for r in results:
        if r["provider"]:
            provider_counts[r["provider"]] = provider_counts.get(r["provider"], 0) + 1

    def pct(key):
        return round(provider_counts.get(key, 0) / len(results) * 100, 1)

    return {
        "strategy":       strategy,
        "workers":        workers,
        "n_receipts":     len(results),
        "n_ok":           n_ok,
        "n_fail":         len(results) - n_ok,
        "wall_time_s":    round(wall_ms / 1000, 2),
        "throughput_rpm": round(n_ok / (wall_ms / 1000) * 60, 1) if wall_ms > 0 else 0,
        "e2e_p50_ms":     _percentile(latencies, 50) if latencies else None,
        "e2e_p95_ms":     _percentile(latencies, 95) if latencies else None,
        "or_pct":         pct("openrouter"),
        "clod_pct":       pct("clod"),
        "avg_items":      round(statistics.mean(r["n_items"] for r in results if r["success"]), 1) if n_ok else None,
        "per_request":    list(results),
    }


# ---------------------------------------------------------------------------
# Sweep across strategies
# ---------------------------------------------------------------------------

async def sweep(
    receipts: list[Path],
    strategies: list[str],
    workers: int,
    n_runs: int,
    user: str,
) -> list[dict]:
    all_results = []

    for strategy in strategies:
        run_stats = []
        print(f"\n  [strategy={strategy}]")
        for run_idx in range(n_runs):
            print(f"    run {run_idx + 1}/{n_runs} … ", end="", flush=True)
            stats = await _run_once(receipts, strategy, workers, user)
            run_stats.append(stats)
            print(
                f"{stats['n_ok']}/{stats['n_receipts']} ok  "
                f"{stats['throughput_rpm']:.1f} rpm  "
                f"p50={stats['e2e_p50_ms']}ms  "
                f"OR={stats['or_pct']}% CLOD={stats['clod_pct']}%"
            )
            if run_idx < n_runs - 1:
                await asyncio.sleep(2)

        def avg(key):
            vals = [r[key] for r in run_stats if r.get(key) is not None]
            return round(statistics.mean(vals), 1) if vals else None

        all_results.append({
            "strategy":       strategy,
            "workers":        workers,
            "n_receipts":     len(receipts),
            "n_runs":         n_runs,
            "n_ok":           avg("n_ok"),
            "n_fail":         avg("n_fail"),
            "wall_time_s":    avg("wall_time_s"),
            "throughput_rpm": avg("throughput_rpm"),
            "e2e_p50_ms":     avg("e2e_p50_ms"),
            "e2e_p95_ms":     avg("e2e_p95_ms"),
            "or_pct":         avg("or_pct"),
            "clod_pct":       avg("clod_pct"),
            "avg_items":      avg("avg_items"),
        })

    return all_results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _percentile(values: list[float], p: int) -> float:
    s = sorted(values)
    idx = max(0, min(int(len(s) * p / 100), len(s) - 1))
    return round(s[idx], 1)


def _collect_receipts(n: int) -> list[Path]:
    available = sorted(
        p for p in RECEIPTS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".heic"}
    )
    if not available:
        raise FileNotFoundError(f"No receipt images found in {RECEIPTS_DIR}")
    return [available[i % len(available)] for i in range(n)]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _print_table(results: list[dict]):
    print()
    header = (
        f"{'Strategy':>14}  {'Throughput':>14}  {'E2E P50':>10}  "
        f"{'E2E P95':>10}  {'OR%':>6}  {'CLOD%':>6}  "
        f"{'Avg Items':>10}  {'Failures':>9}"
    )
    print(header)
    print("─" * len(header))
    for r in results:
        print(
            f"{r['strategy']:>14}  "
            f"{r['throughput_rpm']:>12.1f}rpm  "
            f"{r['e2e_p50_ms']:>9}ms  "
            f"{r['e2e_p95_ms']:>9}ms  "
            f"{r['or_pct']:>5.1f}%  "
            f"{r['clod_pct']:>5.1f}%  "
            f"{str(r['avg_items']):>10}  "
            f"{str(r['n_fail']):>9}"
        )
    print()


def _save_results(results: list[dict]):
    REPORTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    n = results[0]["n_receipts"]

    json_path = REPORTS_DIR / f"exp2_routing_{n}r_{ts}.json"
    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Results (JSON) → {json_path}")

    md_lines = [
        "# Experiment 2 — Provider Routing Strategy Under Concurrent Load",
        "",
        f"**Date**: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}  ",
        f"**Receipts per run**: {n}  ",
        f"**Workers**: {results[0]['workers']}  ",
        f"**Runs averaged**: {results[0]['n_runs']}  ",
        "",
        "## Results",
        "",
        "| Strategy | Throughput (rpm) | E2E P50 (ms) | E2E P95 (ms) | OR % | CLOD % | Avg Items | Failures |",
        "|----------:|----------------:|-------------:|-------------:|-----:|-------:|----------:|---------:|",
    ]
    for r in results:
        md_lines.append(
            f"| {r['strategy']} "
            f"| {r['throughput_rpm']:.1f} "
            f"| {r['e2e_p50_ms']} "
            f"| {r['e2e_p95_ms']} "
            f"| {r['or_pct']:.1f}% "
            f"| {r['clod_pct']:.1f}% "
            f"| {r['avg_items']} "
            f"| {r['n_fail']} |"
        )

    md_lines += [
        "",
        "## Strategies",
        "",
        "- **single**: all traffic to OpenRouter (baseline)",
        "- **round-robin**: alternate OpenRouter / CLOD per receipt",
        "- **cost-aware**: CLOD first; fall back to OpenRouter if CLOD fails or returns < 2 items",
        "",
        "## Methodology",
        "",
        "Each strategy processes the same receipt batch at N=4 concurrent workers.",
        "Results are averaged over multiple runs.",
        "Accuracy (avg_items) is a proxy — compare to ground truth with `python etl.py --eval`.",
        "Cost is tracked in `logs/etl_YYYY-MM-DD.jsonl` (`llm_extraction` events).",
    ]

    md_path = REPORTS_DIR / f"exp2_routing_{n}r_{ts}.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Report (MD)    → {md_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Experiment 2: Provider routing strategy under concurrent load"
    )
    parser.add_argument(
        "--receipts", type=int, default=9,
        help="Number of receipts per run (default: 9; cycles through Receipts/)",
    )
    parser.add_argument(
        "--workers", type=int, default=4,
        help="Concurrent workers (default: 4)",
    )
    parser.add_argument(
        "--runs", type=int, default=3,
        help="Runs per strategy to average (default: 3)",
    )
    parser.add_argument(
        "--strategy", choices=["single", "round-robin", "cost-aware"],
        nargs="+", default=["single", "round-robin", "cost-aware"],
        help="Strategies to test (default: all three)",
    )
    parser.add_argument(
        "--user", default=os.getenv("GYD_USERNAME", "exp2"),
        help="Username for metadata (default: exp2)",
    )
    args = parser.parse_args()

    receipts = _collect_receipts(args.receipts)

    print(f"\nExperiment 2 — Provider Routing Strategy")
    print(f"  receipts={len(receipts)}  workers={args.workers}  runs={args.runs}")
    print(f"  strategies={args.strategy}")
    print()

    results = asyncio.run(sweep(
        receipts=receipts,
        strategies=args.strategy,
        workers=args.workers,
        n_runs=args.runs,
        user=args.user,
    ))

    print("\n=== Summary (averaged over runs) ===")
    _print_table(results)
    _save_results(results)


if __name__ == "__main__":
    main()
