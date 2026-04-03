# GatherYourDeals-ETL

Receipt digitization pipeline exposed as a REST service. Accepts a receipt image and returns structured line-item JSON via OCR + LLM, orchestrated as a [Railtracks](https://railtracks.org/) Flow.

```
POST /etl  { "source": "<image URL or local path>" }
    │
    ▼  Step 1 — Azure Document Intelligence  (prebuilt-read)
    │           Extracts all text from the image via OCR
    │
    ▼  Step 2 — LLM  (OpenRouter  /  CLOD)
    │           Structures the OCR text into the GYD JSON format
    │
    ▼  Step 3 — Azure Maps Geocoding  (optional)
    │           Resolves store address → latitude / longitude
    │
    ▼  Step 4 — GYD Upload
                Uploads structured items to the GYD data service via SDK
```

**Baseline (9 receipts, 2026-03-30):** 9/9 OpenRouter · 9/9 CLOD · $0.1921 total (last run) · OpenRouter $0.0038/receipt · CLOD $0.0003/receipt

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
pip install "railtracks[cli]"      # optional — flow observability
pip install pillow pillow-heif     # optional — only for HEIC (iPhone) photos
pip install matplotlib             # optional — for --report charts
```

> Azure Maps geocoding uses Python's built-in `urllib` — no extra package needed.

### 2. Azure Document Intelligence (Step 1 — OCR)

1. Go to [portal.azure.com](https://portal.azure.com)
2. **Create a resource** → search **Document Intelligence** → Create
3. Choose **Free tier (F0)**: 500 pages/month, no charge
4. After deployment: **Keys and Endpoint** → copy **Endpoint** and **Key 1**

See `docs/setup-azure-di.md` for detailed instructions.

### 3. LLM provider (Step 2 — structuring)

**Option A — OpenRouter** (default)

1. Sign up at [openrouter.ai](https://openrouter.ai)
2. **Keys** → Create key
3. Default model: `anthropic/claude-haiku-4.5`

**Option B — CLOD**

1. Sign up at [app.clod.io](https://app.clod.io)
2. **API Keys** → Create key
3. Set `LLM_PROVIDER=clod` in `.env`
4. Default model: `Qwen/Qwen2.5-7B-Instruct-Turbo`

See `docs/llm-provider-setup.md` for model options, pricing, and troubleshooting.

### 4. Azure Maps Geocoding (Step 3 — optional)

Resolves the store address into `latitude` / `longitude`.
If `AZURE_MAPS_KEY` is not set, lat/lon will be `null` in the output.

1. In the Azure Portal, add **Azure Maps** to your existing resource group
2. Choose **Gen2** pricing tier (free: 5,000 geocode requests/month)
3. After deployment: **Authentication** → copy **Primary Key**

### 5. GYD data service token

The ETL uploads structured receipts to the GYD data service using a JWT access token — no username/password needed.

```bash
# One-time login via the GYD CLI
gatherYourDeals login

# Print your current access token and copy it into .env
gatherYourDeals show-token
```

```env
GYD_SERVER_URL=http://localhost:8080/api/v1
GYD_ACCESS_TOKEN=<paste token here>
```

> The token is initialized **per request** (no shared client). If `GYD_ACCESS_TOKEN` is not set, the SDK falls back to tokens auto-loaded from `~/.GYD_SDK/env.yaml` stored by the CLI login.

### 6. Configure `.env`

```bash
cp .env.example .env
```

```env
# Step 1 — Azure Document Intelligence
AZURE_DI_ENDPOINT=https://<your-resource>.cognitiveservices.azure.com/
AZURE_DI_KEY=<your-key>

# Step 2 — LLM provider: openrouter (default) or clod
LLM_PROVIDER=openrouter

# Option A — OpenRouter
OPENROUTER_API_KEY=sk-or-v1-...
OR_DEFAULT_MODEL=anthropic/claude-haiku-4.5

# Option B — CLOD (set LLM_PROVIDER=clod to use)
# CLOD_API_KEY=<your-key>
CLOD_DEFAULT_MODEL=Qwen/Qwen2.5-7B-Instruct-Turbo

# Step 3 — Azure Maps geocoding (optional — leave blank to skip)
AZURE_MAPS_KEY=<your-key>

# GYD data service — leave blank to run in extract-only mode
GYD_SERVER_URL=http://localhost:8080/api/v1
# JWT access token: run `gatherYourDeals login` then `gatherYourDeals show-token`
GYD_ACCESS_TOKEN=

# ETL service username written into receipt JSON metadata
ETL_DEFAULT_USER=lkim
```

---

## Running as a Service

Start the ETL service with uvicorn:

```bash
uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/etl` | Run the full ETL pipeline from a remote image address |
| `GET` | `/health` | Liveness check |

Interactive API docs available at `http://localhost:8000/docs` once running.

### Example request

```bash
curl -X POST http://localhost:8000/etl \
  -H "Content-Type: application/json" \
  -d '{"source": "https://example.com/receipts/2026-01-03Costco.jpg"}'
```

The `source` field accepts any image address — HTTP/HTTPS URL or local file path.

### Response

```json
{ "success": true, "message": "ETL completed successfully" }
```

| Status | Meaning |
|--------|---------|
| `200` | Pipeline + upload completed successfully |
| `400` | Empty source, unreachable URL, or unsupported file type |
| `422` | Source reachable but OCR / LLM processing failed |
| `500` | Upload to GYD service failed |

### Deploy to Railway

Push to GitHub — Railway auto-detects the `Procfile` and deploys:

```
web: uvicorn app:app --host 0.0.0.0 --port $PORT
```

Set your environment variables (`AZURE_DI_ENDPOINT`, `AZURE_DI_KEY`, `OPENROUTER_API_KEY`, etc.) in the Railway dashboard under **Variables**.

---

## Infrastructure

### Docker (local)

```bash
# Build and start
docker compose up --build

# Run in background
docker compose up -d

# Stop
docker compose down
```

The service starts on `http://localhost:8080`. Logs and output are mounted as volumes so they persist outside the container.

### Docker (manual)

```bash
docker build -t gyd-etl .
docker run -p 8080:8080 --env-file .env gyd-etl
```

### Deploy to Azure Container Apps

Requires [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) and Docker Desktop running locally.

```bash
# One-time login
az login

# First deploy (~3 min — builds image, creates Container App, returns public URL)
bash deploy.sh

# Redeploy after code changes
bash deploy.sh --update
```

`deploy.sh` reads your `.env` and passes all keys as environment variables to the Container App. No Terraform needed.

> **Cost**: Azure Container Apps free grant covers 180,000 vCPU-seconds/month — enough to run the service continuously. An Azure Container Registry (Basic) is created automatically at ~$0.17/day; covered by student credits.

### After deploying

**1. Verify it's live:**
```bash
curl https://<your-app>.<region>.azurecontainerapps.io/health
# → {"status": "ok"}
```

**2. Test with a real receipt:**
```bash
curl -X POST https://<your-app>.<region>.azurecontainerapps.io/etl \
  -H "Content-Type: application/json" \
  -d '{"source": "https://<your-storage>/receipts/2026-01-03Costco.jpg"}'
```

**3. Run Experiment 1 against the live service** — concurrent HTTP requests to `/etl`, no mock needed:
```bash
python experiments/exp1_worker_scaling.py --live https://<your-url>/etl
```

---

### Scale workers locally

```bash
# Run 4 container instances behind a single port (requires a load balancer)
docker compose up --scale etl=4
```

Or use uvicorn's built-in worker pool (single container, multiple processes):

```bash
docker run -p 8080:8080 --env-file .env gyd-etl \
  uvicorn app:app --host 0.0.0.0 --port 8080 --workers 4
```

---

## Usage (CLI)

```bash
# Single receipt
python etl.py Receipts/2026-01-03Costco.jpg --user $GYD_USERNAME --no-upload

# Whole directory
python etl.py Receipts/ --user $GYD_USERNAME --no-upload

# Use CLOD instead of OpenRouter
python etl.py Receipts/ --user $GYD_USERNAME --provider clod --no-upload

# With upload to GYD data service (set GYD_* in .env first)
python etl.py Receipts/ --user $GYD_USERNAME

# Full baseline experiment (both providers, 3× each)
bash scripts/run_baseline.sh

# Generate baseline experiment report (last run only)
python etl.py --baseline-report

# Generate cumulative usage report + charts from logs
python etl.py --report

# Generate per-model comparison table
python etl.py --compare

# Evaluate output/ against ground_truth/
python etl.py --eval

# View Railtracks run visualizer
railtracks viz
```

### CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--user USER` | `unknown` | Username written into the output JSON |
| `--provider {openrouter,clod}` | `LLM_PROVIDER` env var | LLM backend to use |
| `--model MODEL` | `OR_DEFAULT_MODEL` env var | Model ID — overrides provider default |
| `--no-upload` | off | Skip upload to GYD data service |
| `--baseline-report` | off | Structured provider egress report scoped to last run |
| `--report` | off | Cumulative usage report + chart from all logs |
| `--compare` | off | Per-model comparison table from all logs |
| `--eval` | off | Score output/ against ground_truth/ |

---

## Output format

Each receipt produces `output/<provider>/<image-name>.json` as a **list of flat per-item dicts**.
Each element has exactly 7 fields:

```json
[
  {
    "productName": "OPN NAT GRANOLA",
    "purchaseDate": "2026.01.03",
    "price": "4.79USD",
    "amount": "1",
    "storeName": "COSTCO",
    "latitude": 33.8673096,
    "longitude": -117.7515869
  }
]
```

Output is separated by provider (`output/openrouter/`, `output/clod/`) so each can be evaluated independently against ground truth.

---

## `ground_truth/`

Contains reference JSON files — one per receipt — used to evaluate pipeline accuracy. Files are named after the source image (e.g. `2026-01-03Costco.json`) and follow the full receipt schema.

Ground truth files in this repo were **written manually** — each receipt image was read by hand and transcribed into JSON, keeping the reference fully independent from the pipeline.

```bash
python etl.py --eval
```

Scores every file in `output/<provider>/` against `ground_truth/` field by field:

- **Store name, date, lat/lon** — receipt-level scalar fields
- **Item count** — correct number of line items?
- **Item name / price / amount** — per-item matching

Overall 0–100 score (50% scalar fields, 50% item accuracy) saved to `reports/`.

---

## Observability — Railtracks

The pipeline runs as a Railtracks Flow (`receipt_etl`). Railtracks provides a local browser UI to inspect per-receipt run timelines and pass/fail history.

### Setup

```bash
# Inside a virtual environment (recommended)
pip install "railtracks[cli]"

# System Python
pip install "railtracks[cli]" --break-system-packages
```

> If Railtracks is not installed the pipeline falls back to a plain sequential run automatically.

### Usage

```bash
# 1. Run the ETL as normal
python etl.py Receipts/ --user $GYD_USERNAME --no-upload

# 2. Open the viz dashboard
railtracks viz
# → http://localhost:3030
```

### What Railtracks shows vs JSONL logs

| Metric | Railtracks | JSONL logs |
|---|---|---|
| E2E duration per receipt | ✓ | ✓ (`total_latency_ms`) |
| Pass / fail per run | ✓ | ✓ (`llm_success`) |
| Cost per receipt | ✓ (`cost.total_usd`) | ✓ (`llm_cost_usd`) |
| Token counts | ✓ (`usage.*_tokens`) | ✓ (`llm_input_tokens`, `llm_output_tokens`) |
| OCR vs LLM latency split | — | ✓ (`ocr_latency_ms`, `llm_latency_ms`) |

---

## Pricing

| Service | Rate | Notes |
|---------|------|-------|
| Azure Document Intelligence | $0.0015/page (S0) | F0: 500 pages/month free |
| Azure Maps Geocoding | ~$4.50/1,000 req | Gen2: 5,000 req/month free |
| OpenRouter `anthropic/claude-haiku-4.5` | $1.00/$5.00 per M tokens | ~$0.0038/receipt |
| CLOD `Qwen/Qwen2.5-7B-Instruct-Turbo` | $0.30/$0.12 per M tokens | ~$0.0003/receipt (sponsored) |

> ADI cost is always logged at the S0 rate for accurate production cost tracking. Free tier quotas are not subtracted.

---

## Project structure

```
# Service
app.py                  # FastAPI service — POST /etl, GET /health
openapi.yaml            # OpenAPI 3.0.3 schema for the ETL service
etl.py                  # Core pipeline (ADI OCR → LLM → geocode → GYD upload)
etl_logger.py           # Structured JSONL logging
upload_registry.py      # Upload ID registry shared by etl.py and scripts/
reporting.py            # Reporting (--baseline-report, --report, --compare, --eval)

# Infrastructure
Dockerfile              # Container image definition
docker-compose.yml      # Local multi-container setup with volume mounts
.dockerignore           # Excludes venv, receipts, logs from image
deploy.sh               # Azure Container Apps deploy script
Procfile                # Railway deploy config

# Config
.env.example            # Environment variable template
requirements.txt        # Python dependencies

# Data (git-ignored)
Receipts/               # Input receipt images
ground_truth/           # Manually-transcribed reference JSON for each receipt
output/
  openrouter-claude-haiku-4.5/     # Output JSON from OpenRouter runs
  clod-qwen2.5-7b-instruct-turbo/  # Output JSON from CLOD/Qwen runs
  clod-gemma-3n-e4b-it/            # Output JSON from CLOD/Gemma runs
  .upload_registry.json            # Maps image stem → uploaded GYD receipt UUIDs
logs/                   # JSONL structured logs (etl_YYYY-MM-DD.jsonl) + Railtracks rt.log
reports/                # Experiment results (Drafts/ and provider-charts/ are git-ignored)

# Scripts
scripts/
  run_baseline.sh           # Baseline experiment (3 providers × 3 runs)
  delete_receipts.py        # Personal utility — delete test uploads from GYD database

# Experiments
experiments/
  exp1_worker_scaling.py    # Experiment 1 — worker parallelism vs. throughput

# Docs
docs/
  CHANGELOG.md                    # Change log
  distributed_system_proposal.md  # CS6650 final project proposal
  setup-azure-di.md               # Azure DI setup guide
  llm-provider-setup.md           # LLM provider config + model change log
```

---

## Remote storage

Receipt images and large output files are stored on SharePoint:

https://northeastern-my.sharepoint.com/my?id=%2Fpersonal%2Fkim%5Flor%5Fnortheastern%5Fedu%2FDocuments%2FCS6650%20Project&viewid=a5263e9e%2D3e9c%2D4313%2Db199%2De7d2b9d70fc6&startedResponseCatch=true
