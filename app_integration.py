"""
Integration wrapper for multi-worker uvicorn load testing.

Each uvicorn worker spawns a fresh Python process. Patches applied in the
parent (start_integration.py) don't carry over. This module applies the
same patches at import time so every worker self-patches before handling
requests.

Usage:
    uvicorn app_integration:app --workers 2 --port 8080
    uvicorn app_integration:app --workers 4 --port 8080
"""

import sys
import threading
import time
from pathlib import Path

# Ensure project root is on path
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
# CPU work
# ---------------------------------------------------------------------------

def _cpu_work(n: int = 50_000) -> None:
    x = 0
    for i in range(n):
        x += i * i


# ---------------------------------------------------------------------------
# ADI concurrency cap
# ---------------------------------------------------------------------------

_ADI_CAP = 15
_adi_semaphore = threading.Semaphore(_ADI_CAP)
_active_ocr_lock = threading.Lock()
_active_ocr_calls = 0


# ---------------------------------------------------------------------------
# Patched stubs
# ---------------------------------------------------------------------------

def _mock_ocr(image_path, run_id, user_id="", **kwargs):
    global _active_ocr_calls

    with _active_ocr_lock:
        _active_ocr_calls += 1
        current = _active_ocr_calls

    try:
        acquired = _adi_semaphore.acquire(timeout=30)
        if not acquired:
            raise TimeoutError("ADI concurrency cap exceeded — request timed out")

        try:
            _cpu_work(30_000)
            if current > _ADI_CAP:
                time.sleep(6.5)
            else:
                time.sleep(4.7)
            return _STATIC_OCR_TEXT
        finally:
            _adi_semaphore.release()
    finally:
        with _active_ocr_lock:
            _active_ocr_calls -= 1


def _mock_structure(ocr_text, image_path, user_name, model, run_id,
                    provider=None, **kwargs):
    _cpu_work(20_000)
    time.sleep(3.5)
    return _STATIC_RECEIPT, 900, 400, 0.0038


def _mock_upload(receipt, run_id):
    return []


# ---------------------------------------------------------------------------
# Apply patches
# ---------------------------------------------------------------------------

etl.ocr       = _mock_ocr
etl.structure = _mock_structure
etl.upload    = _mock_upload

# ---------------------------------------------------------------------------
# Import app AFTER patches are applied
# ---------------------------------------------------------------------------

from app import app  # noqa: E402  (must be after patches)
