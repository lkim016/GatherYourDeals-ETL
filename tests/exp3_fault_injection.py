#!/usr/bin/env python3
"""
Experiment 3 — Fault Injection: Resilience Strategies Under Provider Failures
==============================================================================
Injects configurable failure rates into the LLM provider and compares four
resilience strategies:

  no-retry         Fail immediately — no retries
  fixed-retry      3 retries, constant 1 s delay between attempts
  exp-backoff      3 retries, exponential delays: 1 s / 2 s / 4 s
  circuit-breaker  Trip after 3 consecutive failures; 30 s cooldown;
                   route to the secondary provider while tripped

Each (failure_rate × strategy) combination is run on the same 9-receipt batch
at N=4 workers.  Results show success rate, P50/P99 latency, retry overhead
cost, and circuit breaker trip frequency.

Failure rates tested: 0% (baseline), 20% (moderate), 40% (severe)

Usage
-----
  # All strategies × all failure rates (default)
  python experiments/exp3_fault_injection.py

  # Single combination
  python experiments/exp3_fault_injection.py \
      --failure-rates 0.4 --strategies circuit-breaker

  # Skip real geocoding to reduce latency during testing (mock=true)
  python experiments/exp3_fault_injection.py --mock-geo

Cost estimate
-------------
  9 receipts × 3 failure rates × 4 strategies × 2 runs
  = 216 receipts max (most fail fast at high failure rates)
  Worst-case CLOD: ~$0.065; OpenRouter fallback: ~$0.05 extra
  Total: ~$0.12

Notes
-----
  - OCR (Azure DI) is always real — failure injection targets the LLM stage only.
  - The circuit breaker routes to the secondary provider (OpenRouter) when tripped,
    so the secondary must have OPENROUTER_API_KEY set.
  - Set --primary clod --secondary openrouter to match the cost-aware baseline.
"""

import argparse
import asyncio
import json
import math
import os
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import etl as _etl

RECEIPTS_DIR = _REPO_ROOT / "Receipts"
REPORTS_DIR  = _REPO_ROOT / "reports"


# ---------------------------------------------------------------------------
# Mock provider wrapper with configurable failure injection
# ---------------------------------------------------------------------------

class MockProviderWrapper:
    """
    Wraps an LLM provider call and injects random failures at `failure_rate`.
    Records every call attempt for cost-overhead analysis.
    """

    def __init__(self, provider: str, failure_rate: float):
        if not 0.0 <= failure_rate <= 1.0:
            raise ValueError(f"failure_rate must be in [0, 1], got {failure_rate}")
        self.provider = provider
        self.failure_rate = failure_rate
        self._model = (
            _etl.CLOD_DEFAULT_MODEL if provider == "clod" else _etl.OR_DEFAULT_MODEL
        )
        self.total_calls = 0
        self.injected_failures = 0

    def call(self, ocr_text: str) -> dict:
        """
        Attempt one provider call.  Raises RuntimeError at `failure_rate` probability.
        """
        self.total_calls += 1
        if random.random() < self.failure_rate:
            self.injected_failures += 1
            raise RuntimeError(
                f"[injected] {self.provider} provider failure "
                f"(rate={self.failure_rate:.0%})"
            )
        if self.provider == "clod":
            result, _pt, _ct, _cost = _etl._structure_clod(ocr_text, self._model)
        else:
            result, _pt, _ct, _gen_id = _etl._structure_openrouter(ocr_text, self._model)
        return result


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """
    Simple circuit breaker for one provider.

    States:
      closed    — normal operation; failures counted
      open      — tripped; route to secondary; auto-reset after cooldown_s
      half-open — trial period after cooldown; one success closes it

    Parameters
    ----------
    threshold   : consecutive failures before tripping
    cooldown_s  : seconds to stay open before entering half-open
    """

    _CLOSED    = "closed"
    _OPEN      = "open"
    _HALF_OPEN = "half-open"

    def __init__(self, threshold: int = 3, cooldown_s: float = 30.0):
        self.threshold    = threshold
        self.cooldown_s   = cooldown_s
        self._state       = self._CLOSED
        self._failures    = 0          # consecutive failures in closed state
        self._opened_at   = 0.0        # monotonic timestamp when tripped
        self.trip_count   = 0          # total times the breaker has tripped

    @property
    def is_open(self) -> bool:
        if self._state == self._OPEN:
            if time.monotonic() - self._opened_at >= self.cooldown_s:
                self._state = self._HALF_OPEN
                return False
            return True
        return False

    def record_success(self):
        self._failures = 0
        self._state    = self._CLOSED

    def record_failure(self):
        self._failures += 1
        if self._state in (self._CLOSED, self._HALF_OPEN):
            if self._failures >= self.threshold:
                self._state    = self._OPEN
                self._opened_at = time.monotonic()
                self.trip_count += 1
                self._failures  = 0


# ---------------------------------------------------------------------------
# Resilience strategy wrappers
# ---------------------------------------------------------------------------

_RETRY_DELAYS = [1.0, 2.0, 4.0]  # exp-backoff delays


def _call_with_strategy(
    strategy: str,
    primary: MockProviderWrapper,
    secondary: MockProviderWrapper | None,
    ocr_text: str,
    breaker: CircuitBreaker | None,
) -> tuple[dict, int]:
    """
    Run LLM structuring with the given resilience strategy.

    Returns:
        (result_dict, attempt_count)
    Raises:
        Exception if all attempts fail.
    """
    if strategy == "no-retry":
        result = primary.call(ocr_text)
        return result, 1

    if strategy == "fixed-retry":
        for attempt in range(1, 4):
            try:
                result = primary.call(ocr_text)
                return result, attempt
            except Exception:
                if attempt < 3:
                    time.sleep(1.0)
        raise RuntimeError("fixed-retry: all 3 attempts failed")

    if strategy == "exp-backoff":
        for attempt in range(1, 4):
            try:
                result = primary.call(ocr_text)
                return result, attempt
            except Exception:
                if attempt < 3:
                    time.sleep(_RETRY_DELAYS[attempt - 1])
        raise RuntimeError("exp-backoff: all 3 attempts failed")

    if strategy == "circuit-breaker":
        assert breaker is not None and secondary is not None
        if breaker.is_open:
            # Breaker open — route to secondary
            result = secondary.call(ocr_text)
            return result, 1

        for attempt in range(1, 4):
            try:
                result = primary.call(ocr_text)
                breaker.record_success()
                return result, attempt
            except Exception:
                breaker.record_failure()
                if breaker.is_open:
                    # Just tripped — immediately fail over to secondary
                    result = secondary.call(ocr_text)
                    return result, attempt + 1
                if attempt < 3:
                    time.sleep(1.0)
        raise RuntimeError("circuit-breaker: all retries failed")

    raise ValueError(f"Unknown strategy: {strategy!r}")


# ---------------------------------------------------------------------------
# Single receipt
# ---------------------------------------------------------------------------

async def _process_one(
    sem: asyncio.Semaphore,
    image_path: Path,
    strategy: str,
    primary: MockProviderWrapper,
    secondary: MockProviderWrapper | None,
    breaker: CircuitBreaker | None,
    mock_geo: bool,
) -> dict:
    async with sem:
        t0 = time.monotonic()
        error = None
        attempts = 0

        try:
            loop = asyncio.get_running_loop()

            # OCR (always real — failure injection is LLM-only)
            ocr_text = await loop.run_in_executor(None, _etl.ocr, image_path)

            # LLM with resilience strategy (blocking)
            result, attempts = await loop.run_in_executor(
                None,
                _call_with_strategy,
                strategy, primary, secondary, ocr_text, breaker,
            )

            # Geocode — optionally skipped (not part of LLM resilience experiment)
            if not mock_geo:
                await loop.run_in_executor(None, _etl.geocode, result)

        except Exception as exc:
            error = str(exc)

        latency_ms = (time.monotonic() - t0) * 1000
        return {
            "image":      image_path.name,
            "latency_ms": round(latency_ms, 1),
            "attempts":   attempts,
            "success":    error is None,
            "error":      error,
        }


# ---------------------------------------------------------------------------
# One (failure_rate × strategy) run
# ---------------------------------------------------------------------------

async def _run_once(
    receipts: list[Path],
    strategy: str,
    failure_rate: float,
    workers: int,
    primary_provider: str,
    secondary_provider: str,
    mock_geo: bool,
) -> dict:
    sem = asyncio.Semaphore(workers)
    primary   = MockProviderWrapper(primary_provider,   failure_rate)
    secondary = MockProviderWrapper(secondary_provider, 0.0)   # secondary never fails
    breaker   = CircuitBreaker() if strategy == "circuit-breaker" else None

    wall_start = time.monotonic()
    results = await asyncio.gather(
        *[_process_one(sem, img, strategy, primary, secondary, breaker, mock_geo)
          for img in receipts]
    )
    wall_ms = (time.monotonic() - wall_start) * 1000

    latencies = [r["latency_ms"] for r in results if r["success"]]
    all_lat   = [r["latency_ms"] for r in results]   # P99 includes failures
    n_ok      = sum(1 for r in results if r["success"])
    total_att = sum(r["attempts"] for r in results)

    return {
        "strategy":       strategy,
        "failure_rate":   failure_rate,
        "workers":        workers,
        "n_receipts":     len(results),
        "n_ok":           n_ok,
        "n_fail":         len(results) - n_ok,
        "success_rate":   round(n_ok / len(results) * 100, 1),
        "wall_time_s":    round(wall_ms / 1000, 2),
        "throughput_rpm": round(n_ok / (wall_ms / 1000) * 60, 1) if wall_ms > 0 else 0,
        "e2e_p50_ms":     _percentile(latencies, 50)  if latencies else None,
        "e2e_p99_ms":     _percentile(all_lat,   99)  if all_lat   else None,
        "avg_attempts":   round(total_att / len(results), 2),
        "breaker_trips":  breaker.trip_count if breaker else 0,
        "injected_fails": primary.injected_failures,
        "per_request":    list(results),
    }


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

async def sweep(
    receipts: list[Path],
    strategies: list[str],
    failure_rates: list[float],
    workers: int,
    n_runs: int,
    primary_provider: str,
    secondary_provider: str,
    mock_geo: bool,
) -> list[dict]:
    all_results = []

    for rate in failure_rates:
        for strategy in strategies:
            label = f"failure={rate:.0%}  strategy={strategy}"
            run_stats = []
            print(f"\n  [{label}]")

            for run_idx in range(n_runs):
                print(f"    run {run_idx + 1}/{n_runs} … ", end="", flush=True)
                stats = await _run_once(
                    receipts, strategy, rate, workers,
                    primary_provider, secondary_provider, mock_geo,
                )
                run_stats.append(stats)
                print(
                    f"{stats['success_rate']}% ok  "
                    f"p50={stats['e2e_p50_ms']}ms  "
                    f"p99={stats['e2e_p99_ms']}ms  "
                    f"attempts={stats['avg_attempts']:.2f}  "
                    f"trips={stats['breaker_trips']}"
                )
                if run_idx < n_runs - 1:
                    await asyncio.sleep(2)

            def avg(key):
                vals = [r[key] for r in run_stats if r.get(key) is not None]
                return round(statistics.mean(vals), 2) if vals else None

            all_results.append({
                "strategy":       strategy,
                "failure_rate":   rate,
                "workers":        workers,
                "n_receipts":     len(receipts),
                "n_runs":         n_runs,
                "n_ok":           avg("n_ok"),
                "n_fail":         avg("n_fail"),
                "success_rate":   avg("success_rate"),
                "wall_time_s":    avg("wall_time_s"),
                "throughput_rpm": avg("throughput_rpm"),
                "e2e_p50_ms":     avg("e2e_p50_ms"),
                "e2e_p99_ms":     avg("e2e_p99_ms"),
                "avg_attempts":   avg("avg_attempts"),
                "breaker_trips":  avg("breaker_trips"),
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
        f"{'Failure':>8}  {'Strategy':>16}  {'Success%':>9}  "
        f"{'P50 (ms)':>10}  {'P99 (ms)':>10}  "
        f"{'Avg Att.':>9}  {'CB Trips':>9}"
    )
    print(header)
    print("─" * len(header))

    last_rate = None
    for r in results:
        rate_str = f"{r['failure_rate']:.0%}" if r["failure_rate"] != last_rate else ""
        last_rate = r["failure_rate"]
        print(
            f"{rate_str:>8}  "
            f"{r['strategy']:>16}  "
            f"{str(r['success_rate']) + '%':>9}  "
            f"{str(r['e2e_p50_ms']) + 'ms':>10}  "
            f"{str(r['e2e_p99_ms']) + 'ms':>10}  "
            f"{r['avg_attempts']:>9.2f}  "
            f"{str(r['breaker_trips']):>9}"
        )
    print()


def _save_results(results: list[dict]):
    REPORTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    n = results[0]["n_receipts"]

    json_path = REPORTS_DIR / f"exp3_fault_{n}r_{ts}.json"
    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Results (JSON) → {json_path}")

    md_lines = [
        "# Experiment 3 — Fault Injection: Resilience Strategies Under Provider Failures",
        "",
        f"**Date**: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}  ",
        f"**Receipts per run**: {n}  ",
        f"**Workers**: {results[0]['workers']}  ",
        f"**Runs averaged**: {results[0]['n_runs']}  ",
        "",
        "## Results",
        "",
        "| Failure Rate | Strategy | Success % | P50 (ms) | P99 (ms) | Avg Attempts | CB Trips |",
        "|-------------:|----------:|----------:|---------:|---------:|-------------:|---------:|",
    ]
    last_rate = None
    for r in results:
        rate_str = f"{r['failure_rate']:.0%}" if r["failure_rate"] != last_rate else ""
        last_rate = r["failure_rate"]
        md_lines.append(
            f"| {rate_str} "
            f"| {r['strategy']} "
            f"| {r['success_rate']}% "
            f"| {r['e2e_p50_ms']} "
            f"| {r['e2e_p99_ms']} "
            f"| {r['avg_attempts']:.2f} "
            f"| {r['breaker_trips']} |"
        )

    md_lines += [
        "",
        "## Strategies",
        "",
        "- **no-retry**: fail immediately on first error",
        "- **fixed-retry**: 3 retries, 1 s constant delay",
        "- **exp-backoff**: 3 retries, 1 s / 2 s / 4 s delays",
        "- **circuit-breaker**: trip after 3 consecutive failures, 30 s cooldown, "
          "route to secondary provider while open",
        "",
        "## Methodology",
        "",
        "Failures are injected at the LLM stage only (OCR is always real).",
        "Each configured failure rate independently samples a Bernoulli(p) for every",
        "LLM call attempt.  Results are averaged over multiple runs.",
        "",
        "P99 includes failed requests; P50 is computed over successful requests only.",
        "Average attempts > 1 indicates retry overhead.",
        "CB Trips = number of times the circuit breaker tripped during the run.",
    ]

    md_path = REPORTS_DIR / f"exp3_fault_{n}r_{ts}.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Report (MD)    → {md_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Experiment 3: Fault injection — resilience strategies under provider failures"
    )
    parser.add_argument(
        "--receipts", type=int, default=9,
        help="Receipts per run (default: 9; cycles through Receipts/)",
    )
    parser.add_argument(
        "--workers", type=int, default=4,
        help="Concurrent workers (default: 4)",
    )
    parser.add_argument(
        "--runs", type=int, default=2,
        help="Runs per (failure_rate × strategy) combination to average (default: 2)",
    )
    parser.add_argument(
        "--failure-rates", type=float, nargs="+", default=[0.0, 0.2, 0.4],
        help="Failure rates to sweep (default: 0.0 0.2 0.4)",
    )
    parser.add_argument(
        "--strategies",
        choices=["no-retry", "fixed-retry", "exp-backoff", "circuit-breaker"],
        nargs="+",
        default=["no-retry", "fixed-retry", "exp-backoff", "circuit-breaker"],
        help="Resilience strategies to compare (default: all four)",
    )
    parser.add_argument(
        "--primary", choices=["clod", "openrouter"], default="clod",
        help="Primary LLM provider (default: clod)",
    )
    parser.add_argument(
        "--secondary", choices=["clod", "openrouter"], default="openrouter",
        help="Secondary provider for circuit-breaker fallback (default: openrouter)",
    )
    parser.add_argument(
        "--mock-geo", action="store_true",
        help="Skip geocoding stage to reduce experiment latency",
    )
    args = parser.parse_args()

    receipts = _collect_receipts(args.receipts)
    n_combos = len(args.failure_rates) * len(args.strategies)

    print(f"\nExperiment 3 — Fault Injection")
    print(f"  receipts={len(receipts)}  workers={args.workers}  runs={args.runs}")
    print(f"  failure_rates={args.failure_rates}  strategies={args.strategies}")
    print(f"  primary={args.primary}  secondary={args.secondary}")
    print(f"  mock_geo={args.mock_geo}")
    print(f"  Total combinations: {n_combos} ({n_combos * args.runs} runs)")
    print()

    results = asyncio.run(sweep(
        receipts=receipts,
        strategies=args.strategies,
        failure_rates=args.failure_rates,
        workers=args.workers,
        n_runs=args.runs,
        primary_provider=args.primary,
        secondary_provider=args.secondary,
        mock_geo=args.mock_geo,
    ))

    print("\n=== Summary (averaged over runs) ===")
    _print_table(results)
    _save_results(results)


if __name__ == "__main__":
    main()
