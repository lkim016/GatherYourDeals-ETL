import os
import asyncio
from src import etl
from pathlib import Path

# --- 1. MOCK THE WORKERS (THE AIRPLANES) ---

async def _mock_ocr(image_path, *args, **kwargs):
    # This sits behind 'throttled_ocr'
    await asyncio.sleep(5) # Simulate OCR work
    return "MOCK OCR TEXT"

def _mock_structure(*args, **kwargs):
    # Note: 'structure' is called via asyncio.to_thread, so it's a regular def
    import time
    time.sleep(3) # Simulate LLM thinking
    return {"storeName": "Mock Store", "items": [{"productName": "Milk", "price": "4.00"}]}, 100, 50, 0.002

def _mock_geocode(*args, **kwargs):
    # Note: 'geo.geocode' is called via asyncio.to_thread
    return 34.05, -118.24

# --- 2. APPLY THE PATCHES (SURGERY) ---

etl.throttled_ocr = _mock_ocr # Replaces the logic inside gate 1
etl.structure = _mock_structure # Replaces the logic inside gate 2
etl.geo.geocode = _mock_geocode # Replaces the logic inside gate 3

# --- Start the App ---
from app import app
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    # We use 1 worker for the cleanest "Event Loop" validation
    print(f"[Phase 1C] Starting Async ETL Service on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")