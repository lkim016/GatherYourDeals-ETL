#!/usr/bin/env python3
"""
GatherYourDeals ETL
====================
Three-step pipeline per receipt, orchestrated as a Railtracks Flow:
  Step 1 — Azure Document Intelligence (prebuilt-read)   [ocr_node]
            Sends the receipt image to ADI, returns raw OCR text.
  Step 2 — LLM structuring  (OpenRouter  OR  CLOD)       [structure_node]
            Structures OCR text into the GYD JSON format.
  Step 3 — Azure Maps Geocoding                          [geocode_node]
            Resolves store address → latitude / longitude.

Railtracks broadcasts granular events at each step so you can inspect
runs in the local visualizer: run `railtracks viz` after processing.

Usage
-----
  # Single receipt — OpenRouter (default)
  python etl.py Receipts/2025-10-01Vons.jpg --user lkim016 --no-upload

  # Single receipt — CLOD
  python etl.py Receipts/2025-10-01Vons.jpg --user lkim016 --provider clod --no-upload

  # Whole directory
  python etl.py Receipts/ --user lkim016 --no-upload

  # With SDK upload
  python etl.py Receipts/2025-10-01Vons.jpg --user lkim016

  # Eval output/ against ground_truth/
  python etl.py --eval

  # View Railtracks run visualizer
  railtracks viz

Requirements
------------
  pip install openai python-dotenv azure-ai-documentintelligence
  pip install "railtracks[cli]"
  pip install git+https://github.com/yuewang199511/GatherYourDeals-SDK.git
  pip install matplotlib              # optional — charts in --report
  pip install pillow pillow-heif      # optional — only for HEIC (iPhone) photos

Environment (.env)
------------------
  # Step 1 — Azure Document Intelligence
  AZURE_DI_ENDPOINT=https://<your-resource>.cognitiveservices.azure.com/
  AZURE_DI_KEY=<your-key>

  # Step 2 — LLM provider: "openrouter" (default) or "clod"
  OPENROUTER_API_KEY=sk-or-v1-...
  LLM_PROVIDER=openrouter

  # Step 3 — Azure Maps geocoding (optional)
  AZURE_MAPS_KEY=<your-key>

  # GYD data server (leave blank to run extract-only)
  GYD_SERVER_URL=http://localhost:8080/api/v1
  GYD_ACCESS_TOKEN=<jwt-access-token>   # from: gatherYourDeals show-token
"""

import argparse
import asyncio
import json
import re
import sys
import time
import uuid
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import os
import time
import base64


from src.logs import etl_logger as el
from src.logs import reporting as rpt

# OR, if you want to use them directly without the 'prompts.' prefix:
from src.core import config, prompts, llm_config
from src.services import ocr as ocr_srvc
from src.services import llm, geo

# Then reference it like this:
if not config.AZURE_DI_KEY:
    print("Error: Azure Key missing!")


# 1. Define the folder you want to scan (e.g., your Receipts folder)
# You can use the Path from your config if you set one up
folder = Path("Receipts")

image_list = [f for f in folder.glob("*") if f.suffix.lower() in config.IMAGE_EXTS]


# --- Concurrency Control ---
_OCR_SEMAPHORE = None

def get_ocr_sem():
    global _OCR_SEMAPHORE
    if _OCR_SEMAPHORE is None:
        _OCR_SEMAPHORE = asyncio.Semaphore(14)
    return _OCR_SEMAPHORE

# Define the global limit for LLM concurrency
# Start with 2 or 3 to be safe for your current API tier
_LLM_SEMAPHORE = None

def get_llm_sem():
    global _LLM_SEMAPHORE
    if _LLM_SEMAPHORE is None:
        _LLM_SEMAPHORE = asyncio.Semaphore(6)
    return _LLM_SEMAPHORE

# Keep geocoding tight - most APIs hate more than 1 or 2 at the exact same time
_GEO_SEMAPHORE = None

def get_geo_sem():
    global _GEO_SEMAPHORE
    if _GEO_SEMAPHORE is None:
        _GEO_SEMAPHORE = asyncio.Semaphore(1)
    return _GEO_SEMAPHORE

async def throttled_ocr(image_bytes: bytes, display_name: str, run_id: str, user_id: str, use_cache: bool):
    """
    Directly passes bytes to the service. No disk I/O needed.
    """    
    async with get_ocr_sem():
        def run_sync():
            # Pass the bytes directly to the service
            # Your service handles the internal conversion/ADI call
            return ocr_srvc.AzureOCRService(
                image_bytes, 
                display_name, 
                run_id, 
                user_id=user_id, 
                use_cache=use_cache
            )

        # Still use to_thread because the SDK call inside the service is synchronous
        return await asyncio.to_thread(run_sync)

# ---------------------------------------------------------------------------
# Railtracks — flow orchestration + observability
# ---------------------------------------------------------------------------
try:
    import railtracks as rt
    _RT_AVAILABLE = True
except ImportError:
    _RT_AVAILABLE = False

# When set to a Path, debug mode writes intermediate pipeline files there.
# Set via --debug CLI flag; stays None in normal operation.
DEBUG_DIR: Path | None = None

def _dbg(stem: str, stage: str, text: str) -> None:
    """Write one debug file if DEBUG_DIR is set. No-op otherwise."""
    if DEBUG_DIR is None:
        return
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    (DEBUG_DIR / f"{stem}.{stage}").write_text(text, encoding="utf-8")
    print(f"  [DBG]  saved debug/{stem}.{stage}")

# Enable Railtracks logging (writes to logs/rt.log + stdout at INFO level)
if _RT_AVAILABLE:
    config.LOGS_DIR.mkdir(exist_ok=True)
    rt.enable_logging(level="INFO", log_file=str(config.LOGS_DIR / "rt.log"))


def _build_system_prompt(ocr_text: str, use_direct: bool = False) -> str:
    # Use the imported variables
    base = prompts.SYSTEM_PROMPT_DIRECT if use_direct else prompts.SYSTEM_PROMPT
    
    ocr_upper = ocr_text.upper()[:1000] 
    addenda = []
    
    # Costco Specifics
    if "COSTCO" in ocr_upper:
        addenda.append(prompts.COSTCO_PROMPT_ADDENDUM)
    
    # You can easily add more store rules here later
    # if "WALMART" in ocr_upper:
    #     addenda.append(prompts.WALMART_ADDENDUM)

    return base + "\n".join(addenda)

# ---------------------------------------------------------------------------
# ETL Pipeline Nodes
# ---------------------------------------------------------------------------
async def ocr_node(image_path, run_id, user_id):
    ocr_service = ocr_srvc.AzureOCRService()
    
    # This replaces the massive block of code previously in etl.py
    ocr_text = await ocr_service.perform_ocr(
        image_data=image_path,
        display_name=image_path.name,
        run_id=run_id,
        user_id=user_id
    )
    
    return ocr_text


# ---------------------------------------------------------------------------
# Discord LOGS
# ---------------------------------------------------------------------------
def run_batch_evaluation(results: list[dict]):
    """
    Evaluation bridge using a persistent Railway Volume.
    """
    # 1. Point to the real directory in your Volume
    # No more hydrate_ground_truth_from_env()!
    gt_dir = config.GROUND_TRUTH_DIR 

    # 2. Safety check: Does the Volume actually have files?
    if not gt_dir.exists() or not any(gt_dir.glob("*.json")):
        print(f"EVAL: Ground truth directory {gt_dir} is empty or missing.")
        return

    # 3. Find the output to score
    # We look for the model folder created during the ETL process
    if not config.OUTPUT_DIR.exists():
        print("EVAL: No outputs found to evaluate.")
        return
        
    # Get the most recently updated subdirectory (e.g., 'openrouter-gpt-4o')
    all_dirs = [d for d in config.OUTPUT_DIR.iterdir() if d.is_dir()]
    target_dir = max(all_dirs, key=os.path.getmtime) if all_dirs else config.OUTPUT_DIR

    print(f"EVAL: Comparing {target_dir.name} vs Ground Truth")

    # 4. Run the scoring engine
    # (Using the same _compute_eval function you already wrote)
    header, rows, scores = rpt.compute_eval(target_dir, gt_dir=gt_dir)

    # 5. Discord Summary
    if scores:
        avg = sum(scores) / len(scores)
        rpt.send_discord_summary(avg, len(scores), rows)

# ---------------------------------------------------------------------------
# Output flattening — denormalize receipt metadata into per-item records
# ---------------------------------------------------------------------------

_VALID_PRICE_RE = re.compile(r"^\d+\.\d{2}[A-Z]{3}$")
_FLAT_NON_PRODUCT = re.compile(
    r"\b(donation|charity|bag\s+fee|bottle\s+dep|deposit|recycling|crv|redemption|"
    r"tax|subtotal|total|savings?|discount|coupon|reward|point|loyalty|"
    r"balance\s+due|change\s+due|cash\s+back|gift\s+card|gst|hst|pst|visa|"
    r"how did we do|survey|feedback|visit us|thank you|mastercard|cash|"
    r"change|balancedue|points earned|net \d+|lb|kg|@)\b",
    re.IGNORECASE
)

def flatten_receipt(receipt: dict) -> list[dict]:
    """
    Convert a structured receipt dict into flat per-item records.

    Each record contains only the 7 target output fields:
      productName, purchaseDate, price, amount, storeName, latitude, longitude

    Items are dropped if:
      - productName or price is null/empty
      - price does not match the X.XXCURRENCY format (e.g. "3.69USD")
      - productName matches a non-product keyword pattern
      - productName is entirely lowercase (garbled OCR — real receipt names are CAPS/Title)
      - productName contains the store name (header line leaked in as a product)

    After filtering, substring deduplication removes fragment names where a longer
    name at the same price already exists (e.g. "GRD A LRG" when "GRD A LRG BRWN MRJ"
    is also present at the same price).
    """
    store_name    = receipt.get("storeName") or None
    purchase_date = receipt.get("purchaseDate") or None
    lat           = receipt.get("latitude")
    lon           = receipt.get("longitude")
    store_lower   = (store_name or "").lower()

    flat_items: list[dict] = []
    for item in receipt.get("items", []):
        name  = str(item.get("productName") or "").strip()
        raw_price = str(item.get("price") or "").strip()

        # 1. Normalize price
        price = raw_price.replace("$", "").replace(" ", "").upper()
        if price.replace(".", "").replace(",", "").isdigit() and "USD" not in price:
             price += "USD"

        # 2. LOG EVERYTHING IMMEDIATELY
        print(f"DEBUG: [Flatten] Processing: '{name}' | Price: '{price}'")
        
        if not name or not price:
            print(f"DEBUG: [Flatten] REJECTED: Missing name or price")
            continue

        # 3. Noise reduction (Tuned for Precision)
        if any(noise in name.upper() for noise in ["NET", "@", "LB", "PCS"]) or "?" in name:
            print(f"DEBUG: [Flatten] REJECTED: Unit/Weight/Survey noise")
            continue
            
        if sum(c.isalpha() for c in name) < 2:
            print(f"DEBUG: [Flatten] REJECTED: Not a product name (too few letters)")
            continue
            
        if not _VALID_PRICE_RE.match(price):
            print(f"DEBUG: [Flatten] REJECTED: Price '{price}' failed regex")
            continue

        # 4. Content Filters
        if name == name.lower() and any(c.isalpha() for c in name):
            print(f"DEBUG: [Flatten] REJECTED: Lowercase/OCR noise")
            continue
        
        if _FLAT_NON_PRODUCT.search(name):
            print(f"DEBUG: [Flatten] REJECTED: Non-product keyword")
            continue

        if store_lower and store_lower in name.lower():
            print(f"DEBUG: [Flatten] REJECTED: Store name leak")
            continue

        # 5. Success!
        flat_items.append({
            "productName":  name,
            "purchaseDate": purchase_date,
            "price":         price,
            "amount":        item.get("amount") or "1",
            "storeName":     store_name,
            "latitude":      lat,
            "longitude":     lon,
        })

    # substring dedup at same price.
    # If name A is a substring of name B and they share a price, drop A (keep the longer one).
    price_groups: dict[str, list[dict]] = {}
    for item in flat_items:
        price_groups.setdefault(item["price"], []).append(item)

    deduped: list[dict] = []
    for items_at_price in price_groups.values():
        names = [i["productName"].lower() for i in items_at_price]
        keep = []
        for i, item in enumerate(items_at_price):
            n = names[i]
            # Drop if any other name at this price fully contains this one (and is longer)
            if any(n != names[j] and n in names[j] for j in range(len(names))):
                continue
            keep.append(item)
        deduped.extend(keep)

    return deduped


def structure(ocr_text: str, display_name: str, user_name: str,
              model: str, run_id: str, provider: str | None = None) -> dict:
    """
    Send OCR text to the configured LLM provider and return the structured receipt dict.

    Long receipts (> _CHUNK_THRESHOLD_CHARS) are split into overlapping vertical
    sections before extraction to prevent LLM attention degradation on noisy OCR.
    Chunk results are merged before geocoding.

    :param provider: ``"openrouter"`` or ``"clod"``.  Defaults to the
        ``LLM_PROVIDER`` environment variable (fallback: ``"openrouter"``).
    """
    resolved_provider = (provider or config.LLM_PROVIDER).lower()

    # Tier 0 — global OCR normalisation before any other processing.
    # Step 1: fix spaced decimals ("1. 160" → "1.160", "1. 72" → "1.72").
    # Step 2: join dangling price lines onto the preceding item line so the
    #         LLM sees "BANANAS  2.00" rather than two unrelated lines.
    # Applied once here so every downstream tier — noise filter, chunker,
    # weight-price parser, and LLM prompt — all see clean, aligned text.
    ocr_text = llm_config.NORM_SPACED_NUM.sub(r'\1.\2', ocr_text)
    ocr_text = llm._join_split_price_lines(ocr_text)

    # Tier 1 — strip noise lines before the text reaches the LLM.
    # Reduces token count on large receipts and prevents total/tax rows
    # from being misidentified as product items.
    ocr_text = llm._filter_noise_lines(ocr_text)

    # Always run the chunker so it strips the raw OCR body when a SPATIAL
    # LAYOUT section is present (saves ~40% tokens on short receipts too).
    # For long receipts it also splits into overlapping sections as before.
    _SPATIAL_MARKER = "\n---\n## SPATIAL LAYOUT\n"
    if _SPATIAL_MARKER in ocr_text or len(ocr_text) > llm._CHUNK_THRESHOLD_CHARS:
        chunks = llm._split_ocr_into_chunks(ocr_text)
    else:
        chunks = [ocr_text]
    is_chunked = len(chunks) > 1

    # Use the leaner direct-output prompt for simple receipts (no CoT scaffolding).
    # Simple = single chunk with spatial layout (column-aligned, unambiguous).
    # Complex = chunked or no spatial layout → keep full CoT prompt for accuracy.
    _use_direct = not is_chunked and _SPATIAL_MARKER in ocr_text
    _is_costco  = "COSTCO" in ocr_text.upper()[:500]
    _prompt = _build_system_prompt(ocr_text, _use_direct)

    # Prompt-path label recorded in the log so per-receipt token counts can be
    # interpreted alongside system-prompt size differences.
    _prompt_path = ("direct" if _use_direct else "cot") + ("+costco" if _is_costco else "")

    # Total OCR content chars sent to the LLM (sum across all chunks, system
    # prompt excluded).  This is the metric we track for token reduction work.
    _input_chars = sum(len(c) for c in chunks)

    start = time.monotonic()
    total_pt, total_ct, total_cost = 0, 0, 0.0
    cost_source    = "estimate"
    latency_source = "local"
    chunk_results: list[dict] = []

    try:
        # --- LLM EXTRACTION ---
        for chunk_text in chunks:
            # Use the imported utility! 
            # It handles provider branching, retries, and JSON parsing internally.
            llm_res = llm.structure_llm(
                provider=resolved_provider,
                ocr_text=chunk_text,
                model=model,
                system_prompt=_prompt
            )
            
            # Accumulate metrics from the LLMResult object
            total_pt += llm_res.input_tokens
            total_ct += llm_res.output_tokens
            total_cost += (llm_res.cost_usd or 0.0)
            
            chunk_results.append(llm_res.data)

        # Merge chunks (no-op when only one chunk)
        result = llm._merge_chunk_results(chunk_results)

        # --- HALLUCINATION GUARD ---
        # Hallucination guard: if none of the extracted item names appear in the
        # OCR text, the model fabricated the receipt.  Retry once with the same
        # model; if it hallucinates again, fall back to the other CLOD model.
        if llm._is_hallucinated(result, ocr_text):
            _FALLBACK_MODEL = "openai/gpt-4o-mini" # Or your preferred fallback
            retry_model = _FALLBACK_MODEL if model != _FALLBACK_MODEL else model
            retry_chunks: list[dict] = []
            
            for chunk_text in chunks:
                try:
                    # Use the same utility for the retry!
                    retry_res = llm.structure_llm(resolved_provider, chunk_text, retry_model, _prompt)
                    total_pt += retry_res.input_tokens
                    total_ct += retry_res.output_tokens
                    total_cost += (retry_res.cost_usd or 0.0)
                    retry_chunks.append(retry_res.data)
                except Exception:
                    pass
            if retry_chunks:
                result = llm._merge_chunk_results(retry_chunks)

        # --- DETERMINISTIC POST-PROCESSING ---
        # Deterministic post-processing: drop bad rows, fix column swaps
        # Tier 2c — normalise store name first so Canadian-store CAD inference works.
        if result.get("storeName"):
            result["storeName"] = llm._normalize_store_name(result["storeName"])
            result["storeName"] = llm._correct_store_name_from_ocr(result["storeName"], ocr_text)

        # Tier 2d — override LLM date with deterministic OCR scan when model picks
        # a promotional date (e.g. contest end date) instead of the transaction date.
        ocr_date = llm._extract_transaction_date(ocr_text)
        if ocr_date:
            result["purchaseDate"] = ocr_date

        # Override LLM-extracted currency with deterministic OCR scan so
        # small models that default to "USD" are corrected for CA/GB/EU receipts.
        # Fall back to Canadian-store inference when OCR has no explicit marker.
        ocr_currency = ocr_srvc._detect_currency_from_ocr(ocr_text)
        if ocr_currency is None:
            ocr_currency = llm._infer_currency_from_store(result.get("storeName") or "")
        currency = ocr_currency or result.get("currency") or "USD"
        result["currency"] = currency

        # Tier 2+3 — deterministic post-processing
        result["items"] = llm._validate_and_fix_items(result.get("items", []), currency)

        # Tier 2b — recover prices for weight-priced items (e.g. "1.160 kg @ $1.72/kg 2.00")
        # Do this before the null-price repair so weight items don't consume repair budget.
        result["items"] = llm._inject_weight_prices(result["items"], ocr_text, currency)

        # Tier 3b+4 — targeted repair for items with null price, then escalate
        null_price_count = sum(1 for i in result.get("items", []) if not str(i.get("price") or "").strip() or str(i.get("price") or "").lower() == "null")
        if null_price_count:
            result["items"] = llm._repair_failed_items(
                result["items"], ocr_text, model, resolved_provider, currency
            )

        latency_ms = (time.monotonic() - start) * 1000

        # Inject caller-controlled fields
        result["totalItems"] = len(result["items"])
        result["imageName"] = display_name
        result["userName"]  = user_name
        

        # Aggressive Junk Filter logic
        valid_items = []
        for item in flatten_receipt(result):
            name = str(item.get("productName") or "").strip()
            is_barcode = any([name.isdigit() and len(name) > 8, len(name) > 10 and any(char.isdigit() for char in name[:8])])
            if is_barcode or "%" in name or "TAX" in name.upper() or len(name) <= 1:
                continue
            if any(stop in name.upper() for stop in ["SUBTOTAL", "TOTAL", "CASH", "CHANGE"]):
                continue
            item["purchaseDate"] = result.get("purchaseDate")
            item["storeName"] = result.get("storeName")
            valid_items.append(item)
        
        result["items"] = valid_items
        
        # --- LOGGING & DISCORD FORWARDING ---
        # 1. Create the log entry
        log_entry = el.log_llm(
            run_id, display_name, user_name, resolved_provider, model,
            total_pt, total_ct, total_cost, latency_ms,
            len(result["items"]), True,
            cost_source=cost_source, latency_source=latency_source,
                input_chars=_input_chars, prompt_path=_prompt_path
        )

        return result, total_pt, total_ct, total_cost

        # log_llm(run_id, display_name, user_name, resolved_provider, model,
        #         total_pt, total_ct, total_cost, latency_ms,
        #         len(result.get("items", [])), True,
        #         cost_source=cost_source, latency_source=latency_source,
        #         input_chars=_input_chars, prompt_path=_prompt_path)
        # return result, total_pt, total_ct, total_cost

    except Exception as e:
        latency_ms = (time.monotonic() - start) * 1000
        error_log = el.log_llm(
            run_id, display_name, user_name, resolved_provider, model,
            0, 0, 0.0, latency_ms, 0, False, str(e), latency_source="local",
            input_chars=_input_chars, prompt_path=_prompt_path
        )
        raise e
    
# ---------------------------------------------------------------------------
# Railtracks — Pydantic context models + function nodes
# ---------------------------------------------------------------------------
if _RT_AVAILABLE:
    from pydantic import BaseModel

    class OcrInput(BaseModel):
        image_path: str   # serialised as string; converted back to Path in node
        display_name: str
        run_id:     str
        user_name:  str
        model:      str
        provider:   str

    class OcrOutput(OcrInput):
        ocr_text: str

    class StructureOutput(OcrOutput):
        # Fields from ctx.model_dump() and ocr_text are likely in OcrOutput
        # but these are the specific ones you just added/confirmed:
        store_name: str
        items_count: int      # Matches the length of your cleaned list
        is_valid: bool        # The quality flag from validate_extraction
        receipt_json: str     # The json.dumps of your scrubbed dict
        usage: dict           # {input_tokens, output_tokens, total_tokens}
        cost: dict            # {total_usd}
        
    
    @rt.function_node
    async def receipt_pipeline(ctx: OcrInput) -> StructureOutput:
        """Single-node pipeline: OCR → LLM/geocode in one Railtracks step."""
        image_path = Path(ctx.image_path)
        
        # 1. OCR Step
        ocr_text = await throttled_ocr(image_path, ctx.display_name, ctx.run_id, ctx.user_name, True)
        if ocr_text is None:
            print(f"ERROR: throttled_ocr returned None for {image_path.name}")
            return 
            
        # 2. LLM Step
        async with get_llm_sem(): # <--- The "Bouncer" gate
            await rt.broadcast(
                f"[LLM] Starting — provider={ctx.provider}  model={ctx.model}; Waiting for slot/Executing — {image_path.name}"
                f"input={len(ocr_text)} chars"
            )
            result, total_pt, total_ct, total_cost = await asyncio.to_thread(
                structure, ocr_text, ctx.display_name, ctx.user_name,
                ctx.model, ctx.run_id, ctx.provider
            )

        # 3. Handle Failed Extraction
        if result is None:
            # Use today's date or a placeholder so the DB upload doesn't reject it
            result = {
                "items": [], 
                "storeName": "Unknown", 
                "purchaseDate": datetime.now().strftime("%Y-%m-%d") 
            }
            total_pt, total_ct, total_cost = 0, 0, 0.0

        # --- PRE-PROCESS STRINGS (Move this up!) ---
        raw_store = str(result.get("storeName") or "Unknown")
        raw_date = result.get("purchaseDate")
        raw_items = result.get("items") or []

        # --- GEOCODING STAGE ---
        store_address = result.get("storeAddress")
        short_name = (result.get("storeName") or "").split(" ")[0]
        lat, lon = None, None
        
        if store_address:
            # Use the lazy getter to avoid "different event loop" error
            async with get_geo_sem(): 
                lat, lon = await asyncio.to_thread(geo.geocode, store_address, short_name)

            
        # --- 4. VALIDATION & CLEANING ---
        clean_items, extraction_is_valid = validate_extraction(raw_items, raw_store)

        # --- 5. STRICT FILTERING (No 0s, No Unknowns) ---
        final_compliant_items = []
        for item in clean_items:
            name = item.get("productName")
            
            # 1. CLEAN PRICE: Force to string, remove non-numeric chars
            raw_price = str(item.get("price") or "0")
            clean_price_str = "".join(c for c in raw_price if c.isdigit() or c == '.')
            try:
                price = float(clean_price_str) if clean_price_str else 0.0
            except ValueError:
                price = 0.0
            
            # 2. CLEAN AMOUNT: Handle strings like "1b" or "2pk"
            raw_amount = str(item.get("amount") or "1")
            # Extract only the leading digits (so "1b" becomes "1")
            clean_amount_str = "".join(c for c in raw_amount if c.isdigit())
            try:
                amount = int(clean_amount_str) if clean_amount_str else 1
            except ValueError:
                amount = 1
            
            # 3. QUALITY GATE
            if name and name != "Unknown Item" and price > 0:
                compliant_item = {
                    "productName": name,
                    "purchaseDate": item.get("purchaseDate") or raw_date,
                    "price": price,
                    "amount": amount,
                    "storeName": item.get("storeName") or raw_store,
                    "latitude": lat,
                    "longitude": lon
                }
                final_compliant_items.append(compliant_item)

        # --- 6. CONDITIONAL SUCCESS ---
        items_count = len(final_compliant_items)
        
        # If we have NO valid items, we treat this as a failure so it doesn't 
        # count toward your "Succeeded" tally in the logs.
        if items_count == 0:
            raise ValueError(f"ETL Failed for {image_path.name}: No valid items extracted.")

        result["items"] = final_compliant_items
        await rt.broadcast(
            f"[LLM] Done — {items_count} valid items  store={raw_store}  valid={extraction_is_valid}"
        )

        return StructureOutput(
            **ctx.model_dump(),
            ocr_text=ocr_text,
            store_name=raw_store,
            items_count=items_count,
            # Save the cleaned JSON, not the raw LLM noise
            receipt_json=json.dumps(result), 
            # Pass the validation flag to your Pydantic model
            is_valid=extraction_is_valid, 
            usage={"input_tokens": total_pt, "output_tokens": total_ct, "total_tokens": total_pt + total_ct},
            cost={"total_usd": round(total_cost, 8)},
        )



def validate_extraction(raw_items: list[dict], store_name: str, currency: str = "USD") -> tuple[list[dict], bool]:
    """
    Cleans extracted items and determines if the overall result is high-quality.
    Returns: (list of cleaned items, is_valid_boolean)
    """
    # --- PRE-FLIGHT TYPE FIX ---
    # Ensure every price/amount is a string so .strip() doesn't crash LLM internal logic
    for item in raw_items:
        if "price" in item:
            item["price"] = str(item["price"] if item["price"] is not None else "0")
        if "amount" in item:
            item["amount"] = str(item["amount"] if item["amount"] is not None else "1")

    # 1. Now it's safe to run your thorough Rule 1-9 + Dedup scrubbing
    fixed_items = llm._validate_and_fix_items(raw_items, currency)
    
    original_count = len(raw_items)
    fixed_count = len(fixed_items)

    print(f"DEBUG: [Validation] Original: {original_count} | Survived: {fixed_count}")

    is_valid = True

    # --- HEURISTIC 1: Zero Yield ---
    # LLM found things, but they were all junk/fees/noise.
    if original_count > 0 and fixed_count == 0:
        print("DEBUG: [Validation] Failed Heuristic 1: Zero Yield")
        is_valid = False

    # --- HEURISTIC 2: High Attrition ---
    # If we dropped > 60% of items, the extraction is likely 'dirty' (e.g., footer text).
    if original_count > 3 and (fixed_count / original_count) < 0.4:
        print(f"DEBUG: [Validation] Failed Heuristic 2: Attrition ({fixed_count/original_count:.2%})")
        is_valid = False

    # --- HEURISTIC 3: Store Name Integrity ---
    # If the store name itself looks like an address, the extraction failed.
    if store_name and llm_config.ADDRESS_LEAK.search(store_name):
        is_valid = False

    return fixed_items, is_valid

# ---------------------------------------------------------------------------
# Extract — runs the pipeline (via Railtracks Flow if available)
# ---------------------------------------------------------------------------
def extract(image_data: "Path | bytes", display_name: str, user_name: str, model: str, run_id: str,
            provider: str | None = None, use_cache: bool = True) -> dict:
    resolved_provider = (provider or config.LLM_PROVIDER).lower()
    
    # 1. Prepare the image path for Railtracks
    image_to_process = None
    is_temp = False

    if isinstance(image_data, bytes):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
            tmp.write(image_data)
            image_to_process = tmp.name
            is_temp = True
    else:
        image_to_process = str(image_data)

    try:
        # 2. Invoke Railtracks
        # Assuming rt and receipt_pipeline are imported/available
        flow = rt.Flow(name="receipt_etl", entry_point=receipt_pipeline)
        
        # Wrap the data in the OcrInput model before passing it to the flow
        input_data = OcrInput(
            image_path=image_to_process,
            display_name=display_name,
            run_id=run_id,
            user_name=user_name,
            model=model,
            provider=resolved_provider
        )

        # Pass the object as the first and only argument
        result = flow.invoke(input_data)

        # 3. Build the response data
        data = json.loads(result.receipt_json)
        data["is_valid"] = result.is_valid
        data["items_count"] = result.items_count
        
        # IMPORTANT: Capture cost for the logger in app.py
        # Railtracks usually stores metadata in the result object
        cost_dict = getattr(result, "cost", {"total_usd": 0.0})
        data["llm_cost_usd"] = cost_dict.get("total_usd", 0.0) # Now it's a float!
        
        return data

    except Exception as exc:
        print(f"Extraction Error for {display_name}: {exc}")
        # Return a structure that _process_one expects for failure
        return {
            "success": False, 
            "message": str(exc),
            "provider": resolved_provider,
            "model": model,
            "llm_cost_usd": 0.0
        }
    finally:
        # 4. Cleanup the temp file
        if is_temp and image_to_process and os.path.exists(image_to_process):
            try:
                os.remove(image_to_process)
            except:
                pass


# ---------------------------------------------------------------------------
# Upload via GYD SDK
# ---------------------------------------------------------------------------
def upload(receipt: dict, run_id: str, token: str | None = None, refresh_token: str | None = None):
    try:
        from gather_your_deals import GYDClient
    except ImportError:
        raise ImportError(
            "GYD SDK not installed.\n"
            "pip install git+https://github.com/yuewang199511/GatherYourDeals-SDK.git"
        )

    # Setup Client
    resolved_token = token or config.GYD_ACCESS_TOKEN
    client = GYDClient(config.GYD_SERVER_URL, auto_persist_tokens=False)
    if resolved_token:
        client._transport.set_tokens(resolved_token, refresh_token or "")

    items = receipt.get("items", [])
    created, failed = [], 0
    start_time = time.monotonic()
    image_name = receipt.get("imageName", "")
    user_name = receipt.get("userName", "Unknown")

    # Shared receipt metadata
    purchase_date = receipt.get("purchaseDate", "0000.00.00")
    store = receipt.get("storeName", "Unknown")

    _UPLOAD_MAX_RETRIES = 3
    _UPLOAD_RETRY_DELAY = 1.0

    for item in items:
        product_name = item.get("productName", "Unknown Item")
        price = item.get("price", "0.00USD")
        amount = str(item.get("amount", "1"))
        
        last_exc = None
        for attempt in range(1, _UPLOAD_MAX_RETRIES + 1):
            try:
                r = client.receipts.create(
                    product_name=product_name, 
                    purchase_date=purchase_date,
                    price=price,
                    amount=amount,
                    store_name=store,
                )
                print(f"[{run_id[:8]}] {purchase_date}  {product_name:<25}  {price:>10}  @ {store}")
                created.append(r)
                last_exc = None
                break 
            except Exception as e:
                last_exc = e
                if attempt < _UPLOAD_MAX_RETRIES:
                    time.sleep(_UPLOAD_RETRY_DELAY)
        
        if last_exc is not None:
            print(f"[ERROR] upload failed for '{product_name}' after {attempt} attempts: {last_exc}")
            failed += 1

    # Final Internal Log for the Upload Step
    latency_ms = (time.monotonic() - start_time) * 1000
    el.log_upload(
        run_id, image_name, user_name,
        len(items), len(created), failed, latency_ms,
        success=(failed == 0), 
        error=f"{failed} items failed" if failed else None
    )

    # If anything failed, we raise this so _process_one knows to mark the whole run as a 'partial success' or 'failure'
    if failed > 0 and len(created) == 0:
        raise RuntimeError(f"Total upload failure: 0/{len(items)} items created.")
    
    return created


# ---------------------------------------------------------------------------
# Upload registry — maps image stem → list of GYD receipt UUIDs
# ---------------------------------------------------------------------------

def _registry_load() -> dict:
    if config.UPLOAD_REGISTRY.exists():
        try:
            return json.loads(config.UPLOAD_REGISTRY.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _registry_save(image_stem: str, ids: list[str]) -> None:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    registry = _registry_load()
    registry[image_stem] = ids
    config.UPLOAD_REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False),
                                encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="GatherYourDeals receipt ETL (ADI + LLM)")
    p.add_argument("path",        nargs="?", help="Image file or directory")
    p.add_argument("--user",      default="unknown", help="Username for JSON metadata")
    p.add_argument("--provider",  default=config.LLM_PROVIDER,
                   choices=["openrouter", "clod"],
                   help="LLM provider (default: LLM_PROVIDER env var)")
    p.add_argument("--model",     default=None,
                   help="Model ID — defaults to OR_DEFAULT_MODEL or CLOD_DEFAULT_MODEL env var")
    p.add_argument("--no-upload",         action="store_true", help="Skip SDK upload")
    p.add_argument("--no-ocr-cache",      action="store_true",
                   help="Force fresh ADI call even if ocr_cache/<stem>.txt exists")
    p.add_argument("--eval",              action="store_true",
                   help="Compare output/ against ground_truth/ and print scores")
    p.add_argument("--baseline-report",   action="store_true",
                   help="Generate structured baseline experiment report")
    args = p.parse_args()

    # Resolve model: CLI flag > .env default for provider
    if args.provider == "clod":
        resolved_model = args.model or config.CLOD_DEFAULT_MODEL
    else:
        resolved_model = args.model or config.OR_DEFAULT_MODEL

    if args.eval:
        rpt.eval_receipts(); return

    if args.baseline_report:
        rpt.baseline_report(); return

    if not args.path:
        p.print_help(); sys.exit(1)

    target = Path(args.path)
    run_id = str(uuid.uuid4())

    images = (sorted(f for f in target.iterdir() if f.suffix.lower() in config.IMAGE_EXTS)
              if target.is_dir() else [target] if target.is_file() else [])
    if not images:
        print(f"No images found at {target}"); sys.exit(1)

    do_upload = not args.no_upload and bool(config.GYD_SERVER_URL)
    if not do_upload and not args.no_upload:
        print("[INFO] GYD_SERVER_URL not set — extract-only mode.")


    errors = 0
    for img in images:
        print(f"\n→ {img.name}")
        _start = time.monotonic()
        try:
            data = extract(img, img.name, args.user, resolved_model, run_id, provider=args.provider,
                           use_cache=not args.no_ocr_cache)
            # --- ADD THESE THREE LINES ---
            print(f"DEBUG: Processing {img.name}")
            print(f"DEBUG: Global Store: {data.get('storeName')} | Global Date: {data.get('purchaseDate')}")
            
            if data.get("items"):
                print(f"DEBUG: First Item Keys: {list(data['items'][0].keys())}")
                print(f"DEBUG: First Item Values: {data['items'][0]}")
            else:
                print("DEBUG: No items found in data dict!")
            # -----------------------------
            
            total_ms = (time.monotonic() - _start) * 1000
            el.log_pipeline(run_id, img.name, args.user, args.provider, resolved_model, total_ms, True)
            
            rows = data["items"]

            # --- AUDIT PRINT (Final check before upload) ---
            if rows:
                sample = rows[0]
                # This prints all 5 fields the server is looking for
                print(f"DEBUG [{img.name}] First Item Check: "
                    f"name='{sample.get('productName')}', "
                    f"date='{sample.get('purchaseDate')}', "
                    f"price='{sample.get('price')}', "
                    f"amount='{sample.get('amount')}', "
                    f"store='{sample.get('storeName')}'")
            else:
                print(f"DEBUG [{img.name}] ERROR: No items found after extraction/flattening!")
            # ------------------------------------------------

            model_slug = resolved_model.split("/")[-1].lower()
            provider_out_dir = config.OUTPUT_DIR / f"{args.provider}-{model_slug}"
            provider_out_dir.mkdir(parents=True, exist_ok=True)
            out = provider_out_dir / (img.stem + ".json")
            out.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"  saved  {out}  ({len(rows)} items)")
            if do_upload:
                # --- ADD THIS ---
                # This prints the first item exactly as it's being sent to the server
                if data.get("items"):
                    print(f"DEBUG DATA CHECK: {json.dumps(data['items'][0], indent=2)}")
                # ----------------
                data["imageName"] = img.name
                created = upload(data, run_id)
                print(f"  uploaded {len(created)}/{len(rows)} items")
        except Exception as e:
            total_ms = (time.monotonic() - _start) * 1000
            el.log_pipeline(run_id, img.name, args.user, args.provider, resolved_model, total_ms, False, str(e))
            print(f"  ERROR: {e}", file=sys.stderr)
            errors += 1

    print(f"\nDone — {len(images)-errors}/{len(images)} succeeded.")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()