"""
GatherYourDeals ETL Service
============================
HTTP wrapper around the ETL pipeline.  Implements the contract defined in
openapi.yaml:

    POST /etl   { "source": "<image URL, local path, or Google Drive folder URL>" }
                → { "success": true/false, "message": "..." }
                  or, for Drive folder sources:
                → { "success": true/false, "message": "N/M succeeded",
                    "results": [{"file": "...", "success": ..., "message": "..."}] }

Single image: the full pipeline (ADI OCR → LLM structuring → geocode → GYD upload)
runs synchronously and blocks until complete.

Google Drive folder: all image files directly inside the folder are processed in
sequence.  Requires GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and
GOOGLE_REFRESH_TOKEN in .env (OAuth 2.0 — run scripts/google_oauth_setup.py
once to obtain the refresh token).

Run:
    uvicorn app:app --host 0.0.0.0 --port 8000

Environment (.env):
    Same variables as etl.py — AZURE_DI_*, OPENROUTER_API_KEY / CLOD_API_KEY,
    GYD_SERVER_URL, GYD_ACCESS_TOKEN, etc.
    Additional:
        ETL_DEFAULT_USER=lkim           # username written into receipt JSON metadata
        GOOGLE_CLIENT_ID=<id>           # OAuth 2.0 client ID  (Drive folder ingestion)
        GOOGLE_CLIENT_SECRET=<secret>   # OAuth 2.0 client secret
        GOOGLE_REFRESH_TOKEN=<token>    # long-lived refresh token
"""

import asyncio
import json
import math
import os
import random
import time
import re
import urllib.parse
import uuid
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src import etl as _etl
from src.core import config
from src.logs import etl_logger as el
from src.logs import reporting as rpt

import gdown
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
logger = logging.getLogger("uvicorn.error")

app = FastAPI(
    title="ETL Service API",
    description=(
        "Internal ETL service that accepts a remote address and processes it "
        "synchronously. No authentication required."
    ),
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_USER = os.getenv("ETL_DEFAULT_USER", "unknown")
_GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
_GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
_GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "")

_ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".tiff", ".tif", ".bmp"}

_DRIVE_IMAGE_MIME_TYPES = {
    "image/jpeg", "image/png", "image/webp",
    "image/heic", "image/tiff", "image/bmp",
}

# Matches: https://drive.google.com/drive/folders/<id>[?...]
_GDRIVE_FOLDER_RE = re.compile(r"drive\.google\.com/drive/folders/([^/?#]+)")

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class EtlRequest(BaseModel):
    source: str
    refresh_token: str | None = None


class EtlResponse(BaseModel):
    success: bool
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_source(source: str) -> tuple[bytes, str]:
    """Download a URL or read a local path into memory.

    Returns:
        (image_bytes, display_name)
        image_bytes:  Raw image bytes.
        display_name: Original filename for use in logs and output/ directory.

    Raises ValueError / FileNotFoundError on bad input.
    """
    if source.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(source)
        url_filename = Path(parsed.path).name or "receipt.jpg"
        
        # Keep your existing extension/display_name logic here for non-GDrive URLs...
        display_name = url_filename 
        
        try:
            import httpx
            with httpx.Client(follow_redirects=True, timeout=60) as client:
                resp = client.get(source)
                resp.raise_for_status()
                return resp.content, display_name
        except Exception as exc:
            raise ValueError(f"Failed to download: {exc}")
    
    # Handle local file paths
    p = Path(source)
    if p.exists():
        return p.read_bytes(), p.name
    raise FileNotFoundError(f"Source not found: {source}")


# ---------------------------------------------------------------------------
# Google Drive folder helpers
# ---------------------------------------------------------------------------


def _build_drive_service():
    """Build an authenticated Drive v3 service from stored OAuth credentials."""
    if not (_GOOGLE_CLIENT_ID and _GOOGLE_CLIENT_SECRET and _GOOGLE_REFRESH_TOKEN):
        raise RuntimeError(
            "Google Drive OAuth not configured. "
            "Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_REFRESH_TOKEN in .env "
            "(run scripts/google_oauth_setup.py once to obtain the refresh token)."
        )
    creds = Credentials(
        token=None,
        refresh_token=_GOOGLE_REFRESH_TOKEN,
        client_id=_GOOGLE_CLIENT_ID,
        client_secret=_GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    creds.refresh(GoogleAuthRequest())
    return build("drive", "v3", credentials=creds)


def _list_drive_images(service, folder_id: str) -> list[dict]:
    """Return all image files directly inside a Google Drive folder."""
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name, mimeType)",
        pageSize=1000,
    ).execute()

    files = results.get("files", [])
    images = [f for f in files if f.get("mimeType") in _DRIVE_IMAGE_MIME_TYPES]

    print(
        f"[drive/oauth] folder={folder_id}: {len(images)} image(s) found "
        f"({len(files)} total files)"
    )
    return images


def _download_folder_gdown(folder_url: str) -> list[tuple[bytes, str]]:
    """Download all image files from a public Drive folder using gdown.

    No API key or OAuth required — folder must be shared as
    'Anyone with the link can view'.

    Returns a list of (image_bytes, filename) tuples for each image found.
    """
    import tempfile
    import shutil

    try:
        import gdown
    except ImportError:
        raise RuntimeError(
            "gdown not installed. Run: pip install gdown"
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="gyd_drive_"))
    try:
        paths = gdown.download_folder(
            url=folder_url,
            output=str(tmp_dir),
            quiet=False,
            use_cookies=False,
        )
        if not paths:
            return []

        results = []
        for p in sorted(paths):
            p = Path(p)
            if p.suffix.lower() in _ALLOWED_EXTS:
                results.append((p.read_bytes(), p.name))
                print(f"  [gdown] {p.name} — {p.stat().st_size:,} bytes")

        return results
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _download_drive_file(service, file_id: str, file_name: str) -> bytes:
    """Download a Drive file via the SDK directly into memory."""
    import io

    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    data = buf.getvalue()
    print(f"  [drive/sdk] {file_name} — {len(data):,} bytes")
    return data


async def _collect_folder_files(source: str, folder_id: str) -> list[tuple]:
    """Uses your existing functions to get images from a folder."""
    file_pairs = []
    
    print(f"\n[etl] batch/gdown — attempting public download for folder {folder_id}\n")
    # 1. Attempt gdown (Public)
    try:
        # Calls your _download_folder_gdown function
        file_pairs = await asyncio.to_thread(_download_folder_gdown, source)
    except Exception as exc:
        print(f"  [gdown] public download failed: {exc}")

    # 2. Fallback to OAuth (Private) if gdown returned nothing
    if not file_pairs:
        oauth_configured = bool(_GOOGLE_CLIENT_ID and _GOOGLE_CLIENT_SECRET and _GOOGLE_REFRESH_TOKEN)
        if oauth_configured:
            print(f"  [gdown] no files — falling back to OAuth SDK...")
            service = await asyncio.to_thread(_build_drive_service)
            images = await asyncio.to_thread(_list_drive_images, service, folder_id)
            
            print(f"  [oauth] {len(images)} image(s) found\n")
            for file in images:
                try:
                    # Calls your _download_drive_file function
                    image_bytes = await asyncio.to_thread(
                        _download_drive_file, service, file["id"], file["name"]
                    )
                    file_pairs.append((image_bytes, file["name"]))
                except Exception as exc:
                    # Handle individual file download failures
                    file_pairs.append((None, file["name"], str(exc)))
                    
    return file_pairs

async def _collect_single_file(source: str) -> tuple[bytes | None, str | None]:
    """
    Resolves a single source (URL, GDrive File, or Local) into bytes and a name.
    """
    _GDRIVE_FILE_RE = re.compile(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)")
    file_match = _GDRIVE_FILE_RE.search(source)
    
    image_bytes = None
    display_name = None

    if file_match:
        # --- GOOGLE DRIVE FILE PATH ---
        file_id = file_match.group(1)
        clean_url = f"https://drive.google.com/uc?id={file_id}"
        display_name = f"gdrive_{file_id[:6]}.jpg"

        # Try gdown first
        try:
            output_path = await asyncio.to_thread(
                gdown.download, clean_url, quiet=True, use_cookies=False
            )
            if output_path and os.path.exists(output_path):
                image_bytes = Path(output_path).read_bytes()
                os.remove(output_path)
        except Exception as e:
            logger.warning(f"gdown failed for single file: {e}")

        # Fallback to OAuth if gdown fails and creds exist
        if not image_bytes and bool(_GOOGLE_CLIENT_ID and _GOOGLE_REFRESH_TOKEN):
            try:
                service = await asyncio.to_thread(_build_drive_service)
                image_bytes = await asyncio.to_thread(_download_drive_file, service, file_id, display_name)
            except Exception as e:
                logger.error(f"OAuth fallback failed: {e}")
    else:
        # --- STANDARD URL / LOCAL PATH ---
        try:
            # Calls your existing _resolve_source function
            image_bytes, display_name = await asyncio.to_thread(_resolve_source, source)
            if hasattr(image_bytes, "read_bytes"):
                image_bytes = image_bytes.read_bytes()
        except Exception as e:
            print(f"  [resolve] failed: {e}")

    return image_bytes, display_name

# ---------------------------------------------------------------------------
# Single-image pipeline (shared by single and batch paths)
# ---------------------------------------------------------------------------

async def _process_one(
    image_bytes: bytes,
    display_name: str,
    jwt_token: str | None,
    refresh_token: str | None = None,
) -> dict:
    run_id = str(uuid.uuid4())
    provider = config.LLM_PROVIDER
    model = config.CLOD_DEFAULT_MODEL if provider == "clod" else config.OR_DEFAULT_MODEL
    
    pipeline_start = time.monotonic()
    
    # Track state for the final log
    success = False
    error_msg = None
    registry = None
    total_cost = 0.0

    try:
        # 1. EXTRACTION
        try:
            data = await asyncio.to_thread(
                _etl.extract, image_bytes, display_name, _DEFAULT_USER, model, run_id, provider
            )
            
            # Capture the cost from the extraction metadata
            total_cost = data.get("llm_cost_usd", 0.0)
        except Exception as exc:
            error_msg = f"extraction: {exc}"
            return {
                "success": False, 
                "message": f"Failed to parse data: {exc}",
                "provider": provider,
                "model": model
            }

        # 2. TRANSFORM
        rows = _etl.flatten_receipt(data)
        data["items"] = rows

        # Optional: Force 'amount' to string if you want to be 100% safe
        for r in rows: r["amount"] = str(r.get("amount", "1"))
        
        # Save local copy for Eval
        model_slug = model.split("/")[-1].lower()
        out_dir = config.OUTPUT_DIR / f"{provider}-{model_slug}"
        out_dir.mkdir(parents=True, exist_ok=True)
        # Use 'data' here, not 'rows'

        # Keep this for your report's cost analysis!
        if isinstance(data, dict):
            print(f"REPORT_METRIC: {display_name} cost was ${data.get('llm_cost_usd', 0)}")

        print(f"DEBUG: Type of data is {type(data)} | Keys: {list(data.keys()) if isinstance(data, dict) else 'N/A'}")

        # Save ONLY the list of items to match the Ground Truth schema
        items_to_save = data.get("items", []) if isinstance(data, dict) else data

        (out_dir / (Path(display_name).stem + ".json")).write_text(
            json.dumps(items_to_save, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # 3. UPLOAD
        try:
            created = _etl.upload(data, run_id, token=jwt_token, refresh_token=refresh_token)
            # This is where your console gets that nice checkmark
            print(f"   ✓  [Upload] Success: {len(created)} records")
        except Exception as exc:
            error_msg = f"upload: {exc}"
            # Even if upload fails, we still return the model/provider info 
            # so the evaluator can try to run on whatever was saved locally!
            return {
                "success": False, 
                "message": error_msg, 
                "provider": provider, 
                "model": model
            }

        # 4. REGISTRY
        image_stem = Path(display_name).stem or display_name
        record_ids = [r.id for r in created]
        registry = {image_stem: record_ids}
        _etl._registry_save(image_stem, record_ids)
        
        # If we reached here, it's a success
        success = True
        return {
            "success": True, 
            "message": "ETL completed successfully", 
            "registry": registry,
            "provider": provider,
            "model": model
        }

    except Exception as exc:
        error_msg = f"unhandled: {exc}"
        return {"success": False, "message": f"Unexpected error: {exc}"}

    finally:
        # --- THE DRY LOGGING ZONE ---
        # This runs NO MATTER WHAT (success, specific error, or unhandled error)
        total_ms = (time.monotonic() - pipeline_start) * 1000
        el.log_pipeline(
            run_id, 
            display_name, 
            _DEFAULT_USER, 
            provider, 
            model, 
            total_ms, 
            success, 
            error=error_msg,
            cost_usd=total_cost
        )

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


async def _run_mock_pipeline() -> None:
    """Sleep through a simulated OCR → LLM → geocode pipeline. No API calls."""
    ocr_ms  = random.lognormvariate(_MOCK_OCR_MU,  _MOCK_OCR_SIG)
    llm_ms  = random.lognormvariate(_MOCK_LLM_MU,  _MOCK_LLM_SIG)
    geo_ms  = random.lognormvariate(_MOCK_GEO_MU,  _MOCK_GEO_SIG)
    await asyncio.sleep((ocr_ms + llm_ms + geo_ms) / 1_000)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post(
    "/etl",
    response_model=EtlResponse,
    responses={
        400: {"model": EtlResponse, "description": "Bad request — missing or invalid source"},
        422: {"model": EtlResponse, "description": "ETL failed — source reachable but processing failed"},
        500: {"model": EtlResponse, "description": "Internal server error"},
    },
)
async def run_etl(
    body: EtlRequest,
    request: Request,
    mock: bool = Query(False, description="If true, skip real pipeline and sleep through mock latencies (Experiment 1 Phase B)"),
):
    """
    Run the full ETL pipeline for a receipt image or a Google Drive folder.

    - Single image: pass any image URL, Google Drive file URL, or local path.
    - Batch: pass a Google Drive folder URL. Files are downloaded via gdown first
      (public folders, no auth required). If gdown retrieves no files, the service
      falls back to the OAuth SDK (requires GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
      and GOOGLE_REFRESH_TOKEN in .env — works for private folders).

    Set `mock=true` to skip real API calls and simulate pipeline latency instead
    (zero cost — for Experiment 1 Phase B load testing).
    """

    # 1. SETUP & AUTH
    auth_header = request.headers.get("Authorization", "")
    jwt_token = auth_header.removeprefix("Bearer ").strip() or None
    refresh_token = body.refresh_token or None
    source = (body.source or "").strip()

    logger.info(f"Checking source: {source}")

    if not source:
        return JSONResponse(status_code=400, content={"success": False, "message": "Source required"})

    # This list will hold (image_bytes, file_name) regardless of source
    file_pairs: list[tuple] = []

    # 2. SOURCE RESOLUTION (The "Gathering" Phase)
    folder_match = _GDRIVE_FOLDER_RE.search(source)
    
    if folder_match:
        # --- BATCH FOLDER LOGIC ---
        folder_id = folder_match.group(1)
        
        print(f"DEBUG: Extracted Folder ID: {folder_id}") # RESTORED
        # Result: file_pairs is populated with multiple images
        file_pairs = await _collect_folder_files(source, folder_id) 
    else:
        # --- SINGLE IMAGE LOGIC ---
        # Result: file_pairs is populated with exactly one image
        img_bytes, name = await _collect_single_file(source)
        if img_bytes:
            file_pairs.append((img_bytes, name))
        else:
            # Optional: if the helper couldn't get the file, 
            # we return an error early.
            return JSONResponse(
                status_code=400, 
                content={"success": False, "message": f"Could not retrieve file: {source}"}
            )

    # 3. VALIDATION
    if not file_pairs:
        return JSONResponse(status_code=400, content={"success": False, "message": "No images found"})

    # 4. UNIFIED EXECUTION (The DRY Phase)
    if mock:
        for _ in range(len(file_pairs)):
            await _run_mock_pipeline()
        return JSONResponse(content={"success": True, "message": f"Mocked {len(file_pairs)} images"})

    # Throttler
    _ADI_SEM = asyncio.Semaphore(5)

    async def _process_wrapper(entry):
        if len(entry) == 3: # Handle download errors
            print(f"  ✗  {entry[1]} — Download failed: {entry[2]}")
            return {"file": entry[1], "success": False, "message": entry[2]}
        
        image_bytes, file_name = entry
        async with _ADI_SEM:
            # This calls your _process_one (the function we fixed with data["items"] = rows)
            return {"file": file_name, **(await _process_one(image_bytes, file_name, jwt_token, refresh_token))}

    # Run everything in the list
    results = await asyncio.gather(*[_process_wrapper(e) for e in file_pairs])

    # --- 5. EVALUATION TRIGGER (PLACE IT HERE) ---
    # Only run this if we aren't in mock mode and there are results to check
    if not mock and results:
        try:
            # Pass the results list to your evaluation utility
            # It will compare these against the Ground Truth and ping Discord once.
            await asyncio.to_thread(rpt.run_batch_evaluation, results)
        except Exception as eval_exc:
            # We use a broad catch here because we don't want an 
            # evaluation failure to crash the actual ETL response.
            logger.error(f"Evaluation reporting failed: {eval_exc}")

    # 5. FINAL RESPONSE
    succeeded = sum(1 for r in results if r.get("success"))
    return JSONResponse(
        status_code=200 if succeeded > 0 else 422,
        content={
            "success": succeeded == len(results),
            "message": f"{succeeded}/{len(results)} succeeded",
            "results": results
        }
    )


@app.get("/health")
def health():
    """Liveness check."""
    return {"status": "ok"}