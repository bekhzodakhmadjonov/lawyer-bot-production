#!/bin/bash
# Azure Container Instances Deployment Script for Lawyer Bot
# Usage: ./azure-deploy.sh

set -e

# Configuration
RESOURCE_GROUP="lawyer-bot-rg"
CONTAINER_NAME="lawyer-bot"
ACR_NAME="lawyerbotacr"
LOCATION="eastus"
DNS_LABEL="lawyer-bot-$(date +%s)"

# Environment variables (set these or load from .env)
OPENAI_API_KEY="${OPENAI_API_KEY}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN}"
TELEGRAM_WEBHOOK_SECRET="${TELEGRAM_WEBHOOK_SECRET}"
TELEGRAM_WEBHOOK_URL="https://${DNS_LABEL}.${LOCATION}.azurecontainer.io"
TELEGRAM_LEAD_CHAT_ID="${TELEGRAM_LEAD_CHAT_ID}"
REQUIRED_CHANNEL_USERNAME="${REQUIRED_CHANNEL_USERNAME}"
REQUIRED_CHANNEL_ID="${REQUIRED_CHANNEL_ID}"

echo "=== Lawyer Bot Azure Deployment ==="
echo "Resource Group: $RESOURCE_GROUP"
echo "Container Name: $CONTAINER_NAME"
echo "Location: $LOCATION"
echo "DNS Label: $DNS_LABEL"
echo ""

# Check if Azure CLI is installed
if ! command -v az &> /dev/null; then
    echo "Azure CLI not found. Please install it first:"
    echo "  https://docs.microsoft.com/en-us/cli/azure/install-azure-cli"
    exit 1
fi

# Login to Azure (if not already logged in)
echo "Checking Azure login status..."
az account show > /dev/null 2>&1 || {
    echo "Please login to Azure:"
    az login
}

# Create resource group
echo "Creating resource group..."
az group create \
    --name $RESOURCE_GROUP \
    --location $LOCATION \
    --output none

# Create Azure Container Registry
echo "Creating Azure Container Registry..."
az acr create \
    --resource-group $RESOURCE_GROUP \
    --name $ACR_NAME \
    --sku Basic \
    --output none

# Login to ACR
echo "Logging in to Azure Container Registry..."
az acr login --name $ACR_NAME

# Build and push Docker image
echo "Building Docker image..."
docker build -t lawyer-bot:latest .

echo "Tagging image for ACR..."
docker tag lawyer-bot:latest ${ACR_NAME}.azurecr.io/lawyer-bot:latest

echo "Pushing image to ACR..."
docker push ${ACR_NAME}.azurecr.io/lawyer-bot:latest

# Create Azure Container Instance
echo "Creating Azure Container Instance..."
az container create \
    --resource-group $RESOURCE_GROUP \
    --name $CONTAINER_NAME \
    --image ${ACR_NAME}.azurecr.io/lawyer-bot:latest \
    --cpu 1 \
    --memory 1 \
    --ports 8000 \
    --environment-variables \
        OPENAI_API_KEY="$OPENAI_API_KEY" \
        TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" \
        TELEGRAM_WEBHOOK_SECRET="$TELEGRAM_WEBHOOK_SECRET" \
        TELEGRAM_WEBHOOK_URL="$TELEGRAM_WEBHOOK_URL" \
        TELEGRAM_LEAD_CHAT_ID="$TELEGRAM_LEAD_CHAT_ID" \
        REQUIRED_CHANNEL_USERNAME="$REQUIRED_CHANNEL_USERNAME" \
        REQUIRED_CHANNEL_ID="$REQUIRED_CHANNEL_ID" \
    --restart-policy Always \
    --dns-name-label $DNS_LABEL \
    --output none

# Get container instance details
echo "Getting container instance details..."
FQDN=$(az container show \
    --resource-group $RESOURCE_GROUP \
    --name $CONTAINER_NAME \
    --query ipAddress.fqdn \
    --output tsv)

echo ""
echo "=== Deployment Complete ==="
echo "Container FQDN: $FQDN"
echo "Webhook URL: https://$FQDN/webhook"
echo ""

# Set Telegram webhook
echo "Setting Telegram webhook..."
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook?url=https://$FQDN/webhook&secret_token=$TELEGRAM_WEBHOOK_SECRET"

echo ""
echo "=== Webhook Set ==="
echo "Please verify the webhook is set correctly:"
echo "curl https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"
echo ""
echo "=== Testing Health Check ==="
echo "curl https://$FQDN/health"
echo ""
echo "=== Monitoring ==="
echo "View logs: az container logs --resource-group $RESOURCE_GROUP --name $CONTAINER_NAME --follow"
echo "View metrics: az container show --resource-group $RESOURCE_GROUP --name $CONTAINER_NAME"
