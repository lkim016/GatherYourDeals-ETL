#!/usr/bin/env python3
"""
delete_receipts.py — personal upload-testing utility
=====================================================
Deletes GYD receipt records that were uploaded during ETL testing.

Receipt IDs are looked up from the upload registry written by etl.py
(output/.upload_registry.json).  Pass the image stem (or full filename)
used when the upload was run.

Usage (run from the project root):
    python scripts/delete_receipts.py 2026-01-03Costco
    python scripts/delete_receipts.py 2026-01-03Costco.jpg   # same result
    python scripts/delete_receipts.py --list                  # show registry

Requires:
    GYD_ACCESS_TOKEN set in .env (same as etl.py)
    GYD_SERVER_URL   set in .env (default: http://localhost:8080/api/v1)
"""

import os
import sys
import time
import uuid
from pathlib import Path

# Allow imports from the project root regardless of where the script is invoked.
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import upload_registry as registry
from etl_logger import log_delete

GYD_SERVER_URL   = os.getenv("GYD_SERVER_URL", "http://localhost:8080/api/v1")
GYD_ACCESS_TOKEN = os.getenv("GYD_ACCESS_TOKEN", "")


def delete_receipts(image_name: str) -> list[str]:
    """Delete all GYD records uploaded for *image_name*.

    Looks up receipt UUIDs from the local registry, calls
    ``client.receipts.delete(id)`` for each, and cleans up the registry entry.

    :param image_name: Image filename or stem (e.g. ``"2026-01-03Costco"``).
    :returns: List of receipt IDs that were successfully deleted.
    :raises SystemExit: If no registry entry exists for the image.
    """
    from gather_your_deals import GYDClient
    from gather_your_deals.exceptions import NotFoundError

    image_stem = Path(image_name).stem or image_name
    reg = registry.load()

    if image_stem not in reg:
        print(f"ERROR: No upload record found for '{image_stem}'.")
        print("       Run `python scripts/delete_receipts.py --list` to see all tracked receipts.")
        sys.exit(1)

    ids = reg[image_stem]
    print(f"Found {len(ids)} receipt(s) for '{image_stem}'.")

    client = GYDClient(GYD_SERVER_URL, auto_persist_tokens=False)
    if GYD_ACCESS_TOKEN:
        client._transport.set_tokens(GYD_ACCESS_TOKEN, "")

    run_id = str(uuid.uuid4())
    deleted, failed_ids = [], []
    start = time.monotonic()

    for receipt_id in ids:
        try:
            client.receipts.delete(receipt_id)
            print(f"  deleted  {receipt_id}")
            deleted.append(receipt_id)
        except NotFoundError:
            # Already removed directly from the DB — treat as gone.
            print(f"  already gone  {receipt_id}  (not found in DB)")
            deleted.append(receipt_id)
        except Exception as e:
            print(f"  FAILED  {receipt_id}  — {e}")
            failed_ids.append(receipt_id)

    latency_ms = (time.monotonic() - start) * 1000
    success = len(failed_ids) == 0
    log_delete(run_id, image_stem, "", len(ids), len(deleted), len(failed_ids),
               latency_ms, success,
               f"{len(failed_ids)} items failed" if failed_ids else None)

    if success:
        registry.remove(image_stem)
        print(f"\nDone — deleted {len(deleted)} receipt(s).  Registry entry removed.")
    else:
        # Leave only the IDs that still need to be retried.
        registry.save(image_stem, failed_ids)
        print(f"\nPartial — deleted {len(deleted)}, failed {len(failed_ids)}.  "
              f"Registry updated with remaining IDs.")

    return deleted


def list_registry() -> None:
    reg = registry.load()
    if not reg:
        print("Registry is empty — no uploads tracked yet.")
        return
    print(f"{'Image':<40}  {'Items':>5}")
    print("-" * 48)
    for stem, ids in sorted(reg.items()):
        print(f"{stem:<40}  {len(ids):>5}")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        description="Delete GYD receipt records uploaded during ETL testing."
    )
    p.add_argument("image", nargs="?",
                   help="Image filename or stem to delete (e.g. 2026-01-03Costco)")
    p.add_argument("--list", action="store_true",
                   help="List all tracked uploads in the registry")
    args = p.parse_args()

    if args.list:
        list_registry()
    elif args.image:
        delete_receipts(args.image)
    else:
        p.print_help()
        sys.exit(1)
