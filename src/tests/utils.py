import asyncio
import random
import math

# ---------------------------------------------------------------------------
# Mock pipeline helper (Experiment 1 Phase B — zero API cost load test)
# ---------------------------------------------------------------------------

# Lognormal params calibrated to 2026-03-31 baseline (OCR P50=5000ms P95=11000ms,
# LLM P50=3718ms P95=10080ms, geocode P50=500ms P95=1200ms)
def _ln_params(p50_ms: float, p95_ms: float) -> tuple[float, float]:
    mu = math.log(p50_ms)
    sigma = (math.log(p95_ms) - mu) / 1.645
    return mu, sigma


_MOCK_OCR_MU,  _MOCK_OCR_SIG  = _ln_params(5_000,  11_000)
_MOCK_LLM_MU,  _MOCK_LLM_SIG  = _ln_params(3_718,  10_080)
_MOCK_GEO_MU,  _MOCK_GEO_SIG  = _ln_params(500,     1_200)


# async def run_mock_pipeline() -> None:
#     """Sleep through a simulated OCR → LLM → geocode pipeline. No API calls."""
#     ocr_ms  = random.lognormvariate(_MOCK_OCR_MU,  _MOCK_OCR_SIG)
#     llm_ms  = random.lognormvariate(_MOCK_LLM_MU,  _MOCK_LLM_SIG)
#     geo_ms  = random.lognormvariate(_MOCK_GEO_MU,  _MOCK_GEO_SIG)
#     await asyncio.sleep((ocr_ms + llm_ms + geo_ms) / 1_000)

# ---------------------------------------------------------------------------
# Mock pipeline helper (Experiment 1 Phase C — zero API cost load test)
# ---------------------------------------------------------------------------
# --- Global Concurrency Control ---
_OCR_SEMAPHORE = None
_LLM_SEMAPHORE = None
_GEO_SEMAPHORE = None

def get_ocr_sem():
    global _OCR_SEMAPHORE
    if _OCR_SEMAPHORE is None:
        _OCR_SEMAPHORE = asyncio.Semaphore(14)
    return _OCR_SEMAPHORE

def get_llm_sem():
    global _LLM_SEMAPHORE
    if _LLM_SEMAPHORE is None:
        # Lower limit for LLM as they are usually more rate-limited
        _LLM_SEMAPHORE = asyncio.Semaphore(6)
    return _LLM_SEMAPHORE

def get_geo_sem():
    global _GEO_SEMAPHORE
    if _GEO_SEMAPHORE is None:
        # Geocoding APIs are often strictly 1-at-a-time for free/low tiers
        _GEO_SEMAPHORE = asyncio.Semaphore(1)
    return _GEO_SEMAPHORE

async def run_mock_pipeline() -> None:
    """
    Simulated OCR → LLM → geocode pipeline.
    Uses Semaphores to simulate real-world API rate limits.
    """
    # 1. Simulate OCR Step (Limited to 14 concurrent)
    async with get_ocr_sem():
        ocr_ms = random.lognormvariate(_MOCK_OCR_MU, _MOCK_OCR_SIG)
        await asyncio.sleep(ocr_ms / 1_000)

    # 2. Simulate LLM Step (Limited to 6 concurrent)
    async with get_llm_sem():
        llm_ms = random.lognormvariate(_MOCK_LLM_MU, _MOCK_LLM_SIG)
        await asyncio.sleep(llm_ms / 1_000)

    # 3. Simulate Geocode Step (Limited to 1 concurrent)
    async with get_geo_sem():
        geo_ms = random.lognormvariate(_MOCK_GEO_MU, _MOCK_GEO_SIG)
        await asyncio.sleep(geo_ms / 1_000)