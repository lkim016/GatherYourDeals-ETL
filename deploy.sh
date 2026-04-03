#!/usr/bin/env bash
# deploy.sh — Deploy GatherYourDeals ETL to Azure Container Apps
#
# Prerequisites:
#   az login
#   az extension add --name containerapp --upgrade
#   Docker Desktop running
#
# Usage:
#   bash deploy.sh            # first deploy
#   bash deploy.sh --update   # redeploy after code changes

set -euo pipefail

# ---------------------------------------------------------------------------
# Config — edit these once
# ---------------------------------------------------------------------------
APP_NAME="gyd-etl"
RESOURCE_GROUP="ETL-ADI"          # reuse your existing Azure DI resource group
LOCATION="eastus"                 # match your Azure DI resource region
ENVIRONMENT="gyd-env"

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
if [ ! -f .env ]; then
  echo "ERROR: .env not found. Copy .env.example and fill in your keys."
  exit 1
fi
source .env

# ---------------------------------------------------------------------------
# Build env-vars string for Container Apps (key=value pairs)
# ---------------------------------------------------------------------------
ENV_VARS=(
  "AZURE_DI_ENDPOINT=${AZURE_DI_ENDPOINT}"
  "AZURE_DI_KEY=${AZURE_DI_KEY}"
  "LLM_PROVIDER=${LLM_PROVIDER:-openrouter}"
  "OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}"
  "OR_DEFAULT_MODEL=${OR_DEFAULT_MODEL:-anthropic/claude-haiku-4.5}"
  "CLOD_API_KEY=${CLOD_API_KEY:-}"
  "CLOD_DEFAULT_MODEL=${CLOD_DEFAULT_MODEL:-Qwen/Qwen2.5-7B-Instruct-Turbo}"
  "AZURE_MAPS_KEY=${AZURE_MAPS_KEY:-}"
)

# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------
if [ "${1:-}" == "--update" ]; then
  echo "Redeploying $APP_NAME..."
  az containerapp update \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --source .
else
  echo "Creating resource group..."
  az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

  echo "Installing containerapp extension..."
  az extension add --name containerapp --upgrade --only-show-errors

  echo "Deploying $APP_NAME to Azure Container Apps..."
  az containerapp up \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --environment "$ENVIRONMENT" \
    --source . \
    --target-port 8080 \
    --ingress external \
    --env-vars "${ENV_VARS[@]}"
fi

echo ""
echo "Service URL:"
az containerapp show \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query "properties.configuration.ingress.fqdn" \
  --output tsv | sed 's/^/https:\/\//'
