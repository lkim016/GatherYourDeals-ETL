"""
Google Drive → ETL Service Ingestor
=====================================
Lists all image files directly inside a publicly shared Google Drive folder
and submits each one to the GYD ETL service via POST /etl.

The Drive folder must be shared as "Anyone with the link can view".
Images must be at the top level of the folder (not in subfolders).

Requirements
------------
    pip install google-api-python-client python-dotenv httpx

Setup
-----
1. Google Cloud Console → create project → enable Google Drive API
2. APIs & Services → Credentials → Create API key
3. Add to .env:
       GOOGLE_API_KEY=<your-api-key>
       GYD_ACCESS_TOKEN=<your-gyd-jwt>

Usage
-----
    # Against your deployed Railway service
    python scripts/drive_ingest.py \\
        --folder-id <google-drive-folder-id> \\
        --etl-url https://your-service.up.railway.app

    # Against Yue's deployed Railway service
    python scripts/drive_ingest.py \\
        --folder-id <google-drive-folder-id> \\
        --etl-url https://gatheryourdeals-etl.up.railway.app

The folder ID is the string at the end of the Drive folder URL:
    https://drive.google.com/drive/folders/<folder-id>
"""

import argparse
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from googleapiclient.discovery import build

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GOOGLE_API_KEY  = os.getenv("GOOGLE_API_KEY", "")
GYD_ACCESS_TOKEN = os.getenv("GYD_ACCESS_TOKEN", "")

_IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/tiff",
    "image/bmp",
}

# Direct download URL for a publicly shared Drive file
_DRIVE_DOWNLOAD_URL = "https://drive.google.com/uc?export=download&id={file_id}"


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------

def _list_images(folder_id: str, api_key: str) -> list[dict]:
    """Return all image files directly inside the public Drive folder."""
    service = build("drive", "v3", developerKey=api_key)
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name, mimeType)",
        pageSize=1000,
    ).execute()

    files = results.get("files", [])
    images = [f for f in files if f.get("mimeType") in _IMAGE_MIME_TYPES]

    if not images:
        print(f"[drive] No image files found in folder {folder_id}")
        print(f"        Found {len(files)} total files with MIME types: "
              f"{set(f.get('mimeType') for f in files)}")
    else:
        print(f"[drive] Found {len(images)} image(s) in folder {folder_id}")

    return images


# ---------------------------------------------------------------------------
# ETL submission
# ---------------------------------------------------------------------------

def _submit(file: dict, etl_url: str, jwt: str) -> bool:
    """POST one Drive file to the ETL service. Returns True on success."""
    download_url = _DRIVE_DOWNLOAD_URL.format(file_id=file["id"])
    name = file["name"]

    headers = {"Content-Type": "application/json"}
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"

    try:
        resp = httpx.post(
            f"{etl_url.rstrip('/')}/etl",
            json={"source": download_url},
            headers=headers,
            timeout=300,  # ETL pipeline can take ~30s per receipt
        )
        if resp.status_code == 200:
            print(f"  ✓  {name}")
            return True
        else:
            print(f"  ✗  {name} — HTTP {resp.status_code}: {resp.text[:200]}")
            return False
    except httpx.TimeoutException:
        print(f"  ✗  {name} — request timed out (>300s)")
        return False
    except Exception as exc:
        print(f"  ✗  {name} — {exc}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Ingest images from a public Google Drive folder into the GYD ETL service"
    )
    p.add_argument("--folder-id", required=True,
                   help="Google Drive folder ID (from the folder URL)")
    p.add_argument("--etl-url", required=True,
                   help="ETL service base URL (e.g. https://your-service.up.railway.app)")
    args = p.parse_args()

    if not GOOGLE_API_KEY:
        print("ERROR: GOOGLE_API_KEY not set in .env")
        sys.exit(1)

    if not GYD_ACCESS_TOKEN:
        print("WARN: GYD_ACCESS_TOKEN not set in .env — uploads may fail auth")

    # List images
    try:
        images = _list_images(args.folder_id, GOOGLE_API_KEY)
    except Exception as exc:
        print(f"ERROR: Failed to list Drive folder: {exc}")
        print("       Check that the folder is shared as 'Anyone with the link can view'")
        print("       and that GOOGLE_API_KEY is valid with the Drive API enabled.")
        sys.exit(1)

    if not images:
        sys.exit(0)

    # Submit each image to the ETL service
    print(f"\n[etl] Submitting to {args.etl_url}\n")
    succeeded = failed = 0
    for file in images:
        ok = _submit(file, args.etl_url, GYD_ACCESS_TOKEN)
        if ok:
            succeeded += 1
        else:
            failed += 1
        time.sleep(0.5)  # brief pause between submissions

    print(f"\nDone — {succeeded}/{len(images)} succeeded, {failed} failed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
