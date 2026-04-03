#!/usr/bin/env bash
# =============================================================================
# GatherYourDeals ETL — Baseline Experiment (Provider Egress Comparison)
# =============================================================================
# Measures egress characteristics of all pipeline providers:
#   1. Azure Document Intelligence (OCR)           — runs with every batch
#   2. OpenRouter / claude-haiku-4.5               — primary LLM provider
#   3. CLOD / Qwen2.5-7B-Instruct-Turbo            — alternative LLM provider
#   4. CLOD / gemma-3n-E4B-it                      — alternative LLM provider
#   5. OpenRouter / qwen-2.5-7b-instruct           — alternative LLM provider
#
# Each provider is run 3× for statistically meaningful P50/P95 latency
# and cost/receipt averages.
#
# Findings produced:
#   - Baseline P50/P95 latency per provider (ADI + LLM)
#   - Cost per receipt for each provider
#   - Context length (OCR chars in, LLM tokens in/out) per receipt
#   - Correctness (items extracted vs ground truth)
#   - Which provider is viable as primary vs fallback
#
# Output:
#   logs/etl_<date>.jsonl     — raw event log (one line per provider call)
#   reports/baseline_<ts>.md  — structured provider comparison report
#
# Usage (run from the project root):
#   bash scripts/run_baseline.sh
#
# Prerequisites:
#   - .env configured with OPENROUTER_API_KEY, CLOD_API_KEY, AZURE_DI_KEY
#   - OpenRouter account topped up (claude-haiku-4.5 is a paid model ~$0.0025/receipt)
#   - Receipts/ folder populated with receipt images
#   - ground_truth/ folder populated (for correctness scoring)
#   - pip install -r requirements.txt
# =============================================================================

set -e

# Always run from the project root so relative paths (Receipts/, logs/, etc.) resolve correctly.
cd "$(dirname "$0")/.."

# Helper: run a provider block and continue even if it fails (so one flaky
# provider doesn't abort the whole experiment).
run_or_warn() {
    "$@" || echo "[baseline] WARNING: command failed (exit $?), continuing…" >&2
}

USER="lkim"
RECEIPTS="Receipts/"

# Record experiment start time — baseline_report uses this to scope logs to this run only
date -u +"%Y-%m-%dT%H:%M:%SZ" > .baseline_start

echo ""
echo "============================================================"
echo " GatherYourDeals ETL — Baseline Experiment"
echo " $(date '+%Y-%m-%d %H:%M')"
echo "============================================================"
echo ""
echo "Receipts folder : $RECEIPTS"
echo "Receipt count   : $(ls $RECEIPTS*.jpg 2>/dev/null | wc -l | tr -d ' ')"
echo ""

# -----------------------------------------------------------------------------
# Runs 1–3 — OpenRouter (Primary LLM provider)  [COMMENTED OUT — preserve credits]
# -----------------------------------------------------------------------------
# for i in 1; do
#     echo "------------------------------------------------------------"
#     echo " OpenRouter run $i/3 — claude-haiku-4.5"
#     echo " Measures: ADI egress latency + OpenRouter egress latency/cost"
#     echo "------------------------------------------------------------"
#     run_or_warn venv/bin/python etl.py "$RECEIPTS" \
#         --user "$USER" \
#         --provider openrouter \
#         --model anthropic/claude-haiku-4.5 \
#         --no-upload
#     echo ""
# done

# -----------------------------------------------------------------------------
# Runs 4–6 — CLOD (Alternative LLM provider)
# 3 runs × 10 receipts = 30 data points for statistically meaningful P50/P95.
# -----------------------------------------------------------------------------
for i in 1; do
    echo "------------------------------------------------------------"
    echo " CLOD run $i/3 — Qwen2.5-7B-Instruct-Turbo"
    echo " Measures: ADI egress latency + CLOD egress latency/cost"
    echo "------------------------------------------------------------"
    run_or_warn venv/bin/python etl.py "$RECEIPTS" \
        --user "$USER" \
        --provider clod \
        --model Qwen/Qwen2.5-7B-Instruct-Turbo \
        --no-upload
    echo ""
done

echo ""

# -----------------------------------------------------------------------------
# Runs 7–9 — CLOD / gemma-3n-E4B-it (second alternative LLM provider)
# -----------------------------------------------------------------------------
for i in 1; do
    echo "------------------------------------------------------------"
    echo " CLOD run $i/3 — gemma-3n-E4B-it"
    echo " Measures: ADI egress latency + CLOD egress latency/cost"
    echo "------------------------------------------------------------"
    run_or_warn venv/bin/python etl.py "$RECEIPTS" \
        --user "$USER" \
        --provider clod \
        --model google/gemma-3n-E4B-it \
        --no-upload
    echo ""
done

# -----------------------------------------------------------------------------
# Runs 10–12 — OpenRouter / qwen-2.5-7b-instruct  [COMMENTED OUT — preserve credits]
# -----------------------------------------------------------------------------
# for i in 1; do
#     echo "------------------------------------------------------------"
#     echo " OpenRouter run $i/3 — qwen/qwen-2.5-7b-instruct"
#     echo " Measures: ADI egress latency + OpenRouter egress latency/cost"
#     echo "------------------------------------------------------------"
#     run_or_warn venv/bin/python etl.py "$RECEIPTS" \
#         --user "$USER" \
#         --provider openrouter \
#         --model qwen/qwen-2.5-7b-instruct \
#         --no-upload
#     echo ""
# done

# -----------------------------------------------------------------------------
# Generate baseline experiment report
# -----------------------------------------------------------------------------
echo "------------------------------------------------------------"
echo " Generating baseline experiment report"
echo "------------------------------------------------------------"
venv/bin/python etl.py --baseline-report

echo ""
echo "============================================================"
echo " Baseline experiment complete."
echo " Results in reports/"
echo "============================================================"
echo ""
echo "Next steps:"
echo "  1. Paste the compare table into Notion weekly log"
echo "  2. Note which provider had lower p95 latency"
echo "  3. Note cost per receipt for each provider"
echo "  4. Identify primary vs fallback provider based on latency + correctness"
echo ""
