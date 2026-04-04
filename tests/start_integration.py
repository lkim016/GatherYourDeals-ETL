"""
Integration Load Test — ETL Service Startup
============================================
Starts the FastAPI service with external API calls patched out but the full
ETL code path intact. The real pipeline runs — source validation, extract()
orchestration, flatten_receipt(), etl_logger, and file output — only the
three external network calls are replaced with stubs:

  ocr()       → static OCR text  + CPU work + sleep (~5s)
  structure() → static receipt   + CPU work + sleep (~3.7s)
  upload()    → no-op (returns empty list)

This is an integration load test, not a unit test. It answers:

  "Does the service fall apart under concurrent requests?"
  "Where does latency build up under real pipeline execution?"
  "Does the async scheduling serialize unexpectedly?"

Usage (two terminals):

  # Terminal 1 — start the service (run from project root)
  python tests/start_integration.py

  # Terminal 2 — run the load test (no ?mock=true — hits the real code path)
  locust -f tests/locustfile_integration.py --headless -u 100 -r 10 \\
      --run-time 60s --host http://localhost:8080

  locust -f tests/locustfile_integration.py --headless -u 500 -r 50 \\
      --run-time 60s --host http://localhost:8080

Why stubs still produce meaningful results:

  - CPU work (loop-based) in each stub creates real scheduling pressure and
    thread contention — requests compete for resources, not just timers.
  - Calibrated sleep preserves relative stage cost (OCR ≈ 5s, LLM ≈ 3.7s),
    so latency proportions match production.
  - The ADI concurrency cap (15 concurrent OCR calls) is enforced via a
    semaphore so rate-limiting behaviour and tail latency spikes are visible.
  - The full etl.py code path runs — any bottleneck in extract() orchestration,
    flatten_receipt(), or logging shows up in the results.
"""

import sys
import threading
import time
from pathlib import Path

# Add project root so etl, app, etl_logger are importable.
# Add tests/ dir so uvicorn workers can import "start_integration:app" directly.
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import etl

# ---------------------------------------------------------------------------
# Static stub responses
# ---------------------------------------------------------------------------

_STATIC_OCR_TEXT = """
Store: Test Market
123 Main St, Vancouver BC V6B 1A1
Date: 2026-04-02

Whole Milk 2L          3.99
Sourdough Bread        4.49
Free Range Eggs 12pk   5.99
Sparkling Water 1L     1.99

Subtotal              16.46
Tax (5%)               0.82
Total                 17.28
"""

_STATIC_RECEIPT = {
    "storeName": "Test Market",
    "purchaseDate": "2026-04-02",
    "latitude": 49.2827,
    "longitude": -123.1207,
    "userName": "loadtest",
    "imageName": "receipt.jpg",
    "items": [
        {"productName": "Whole Milk 2L",        "price": "3.99USD", "amount": "1"},
        {"productName": "Sourdough Bread",       "price": "4.49USD", "amount": "1"},
        {"productName": "Free Range Eggs 12pk",  "price": "5.99USD", "amount": "1"},
        {"productName": "Sparkling Water 1L",    "price": "1.99USD", "amount": "1"},
    ],
}

# ---------------------------------------------------------------------------
# CPU work — creates real scheduling pressure so requests compete for
# resources rather than just waiting on timers.
# ---------------------------------------------------------------------------

def _cpu_work(n: int = 50_000) -> None:
    x = 0
    for i in range(n):
        x += i * i


# ---------------------------------------------------------------------------
# ADI concurrency cap — Azure DI S0 tier allows ~15 concurrent requests.
# Enforced via a semaphore so callers beyond the cap see throttling latency.
# ---------------------------------------------------------------------------

_ADI_CAP = 15
_adi_semaphore = threading.Semaphore(_ADI_CAP)
_active_ocr_lock = threading.Lock()
_active_ocr_calls = 0


# ---------------------------------------------------------------------------
# Patched OCR — simulates Azure Document Intelligence
# ---------------------------------------------------------------------------

def _mock_ocr(image_path, run_id, user_id="", **kwargs):
    global _active_ocr_calls

    with _active_ocr_lock:
        _active_ocr_calls += 1
        current = _active_ocr_calls

    try:
        acquired = _adi_semaphore.acquire(timeout=30)
        if not acquired:
            # Semaphore timeout — simulate a provider-side timeout
            raise TimeoutError("ADI concurrency cap exceeded — request timed out")

        try:
            # Simulate parsing effort — CPU contention under load
            _cpu_work(30_000)

            if current > _ADI_CAP:
                # Throttling tail — extra latency for over-limit calls
                time.sleep(6.5)
            else:
                time.sleep(4.7)  # OCR P50 ≈ 5s total with cpu_work

            return _STATIC_OCR_TEXT
        finally:
            _adi_semaphore.release()
    finally:
        with _active_ocr_lock:
            _active_ocr_calls -= 1


# ---------------------------------------------------------------------------
# Patched structure — simulates LLM provider call
# ---------------------------------------------------------------------------

def _mock_structure(ocr_text, image_path, user_name, model, run_id,
                    provider=None, **kwargs):
    # Simulate LLM token processing effort
    _cpu_work(20_000)
    time.sleep(3.5)  # LLM P50 ≈ 3.7s total with cpu_work
    return _STATIC_RECEIPT, 900, 400, 0.0038


# ---------------------------------------------------------------------------
# Patched upload — no-op, GYD service not called
# ---------------------------------------------------------------------------

def _mock_upload(receipt, run_id):
    return []


# ---------------------------------------------------------------------------
# Apply patches
# ---------------------------------------------------------------------------

etl.ocr       = _mock_ocr
etl.structure = _mock_structure
etl.upload    = _mock_upload

print("[integration] Patches applied:")
print("  etl.ocr       → _mock_ocr       (CPU work + sleep ~5s, ADI cap=15)")
print("  etl.structure → _mock_structure  (CPU work + sleep ~3.7s)")
print("  etl.upload    → _mock_upload     (no-op)")
print()

# ---------------------------------------------------------------------------
# Import app AFTER patches — workers import this module, so patches run in
# every worker process before any request is handled.
# ---------------------------------------------------------------------------

from app import app  # noqa: E402

# ---------------------------------------------------------------------------
# Start uvicorn
# ---------------------------------------------------------------------------

import uvicorn

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Start ETL service with mocked providers for integration load testing"
    )
    parser.add_argument("--workers", type=int, default=1,
                        help="Number of uvicorn workers (default: 1)")
    parser.add_argument("--port", type=int, default=8080,
                        help="Port to listen on (default: 8080)")
    args = parser.parse_args()

    print(f"[integration] Starting ETL service on port {args.port} "
          f"with {args.workers} worker(s)")
    print(f"[integration] Send requests to POST http://localhost:{args.port}/etl")
    print()

    uvicorn.run("start_integration:app", host="0.0.0.0", port=args.port, workers=args.workers)
