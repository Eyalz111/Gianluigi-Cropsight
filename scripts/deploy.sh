#!/bin/bash
# Gianluigi Deployment Script for Google Cloud Run
#
# Prerequisites:
# 1. Google Cloud SDK installed (gcloud)
# 2. Docker installed
# 3. Authenticated with gcloud: gcloud auth login
# 4. Project selected: gcloud config set project PROJECT_ID
#
# Usage:
#   ./scripts/deploy.sh [env]
#   ./scripts/deploy.sh production
#   ./scripts/deploy.sh staging

set -e  # Exit on error

# =============================================================================
# Configuration
# =============================================================================

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-gianluigi-cropsight}"
REGION="europe-west1"  # Frankfurt region for EU data residency
SERVICE_NAME="gianluigi"
IMAGE_NAME="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

# Environment (default to staging)
ENVIRONMENT="${1:-staging}"

echo "=========================================="
echo "Deploying Gianluigi to Google Cloud Run"
echo "=========================================="
echo "Project: ${PROJECT_ID}"
echo "Region: ${REGION}"
echo "Environment: ${ENVIRONMENT}"
echo ""

# =============================================================================
# Pre-deployment Checks
# =============================================================================

echo "Running pre-deployment checks..."

# Check if gcloud is installed
if ! command -v gcloud &> /dev/null; then
    echo "Error: gcloud CLI not installed"
    echo "Install from: https://cloud.google.com/sdk/docs/install"
    exit 1
fi

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo "Error: Docker not installed"
    exit 1
fi

# Check if .env file exists
if [ ! -f ".env" ]; then
    echo "Error: .env file not found"
    echo "Copy .env.example to .env and fill in values"
    exit 1
fi

# Check required environment variables
required_vars=("ANTHROPIC_API_KEY" "SUPABASE_URL" "SUPABASE_KEY" "TELEGRAM_BOT_TOKEN")
for var in "${required_vars[@]}"; do
    if ! grep -q "^${var}=" .env; then
        echo "Error: Missing required variable ${var} in .env"
        exit 1
    fi
done

echo "Pre-deployment checks passed!"
echo ""

# =============================================================================
# Build Docker Image
# =============================================================================

echo "Building Docker image..."

# Build with production tag
docker build -t "${IMAGE_NAME}:latest" -t "${IMAGE_NAME}:${ENVIRONMENT}" .

echo "Docker image built successfully!"
echo ""

# =============================================================================
# Push to Google Container Registry
# =============================================================================

echo "Pushing image to Google Container Registry..."

# Configure Docker to use gcloud credentials
gcloud auth configure-docker --quiet

# Push the image
docker push "${IMAGE_NAME}:latest"
docker push "${IMAGE_NAME}:${ENVIRONMENT}"

echo "Image pushed successfully!"
echo ""

# =============================================================================
# Deploy to Cloud Run
# =============================================================================

echo "Deploying to Cloud Run..."

# Load environment variables for secrets
# In production, these should be stored in Secret Manager
gcloud run deploy "${SERVICE_NAME}" \
    --image "${IMAGE_NAME}:${ENVIRONMENT}" \
    --region "${REGION}" \
    --platform managed \
    --allow-unauthenticated \
    --memory 512Mi \
    --cpu 1 \
    --timeout 300 \
    --concurrency 10 \
    --min-instances 0 \
    --max-instances 2 \
    --set-env-vars "ENVIRONMENT=${ENVIRONMENT}" \
    --quiet

echo ""
echo "=========================================="
echo "Deployment Complete!"
echo "=========================================="

# Get the service URL
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
    --region "${REGION}" \
    --format "value(status.url)")

echo "Service URL: ${SERVICE_URL}"
echo ""

# =============================================================================
# Post-deployment
# =============================================================================

echo "Running post-deployment checks..."

# Health check
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${SERVICE_URL}/health" || echo "000")

if [ "$HTTP_STATUS" = "200" ]; then
    echo "Health check passed!"
else
    echo "Warning: Health check returned status ${HTTP_STATUS}"
    echo "Check logs with: gcloud run logs read --service=${SERVICE_NAME} --region=${REGION}"
fi

echo ""
echo "Useful commands:"
echo "  View logs:    gcloud run logs read --service=${SERVICE_NAME} --region=${REGION}"
echo "  View service: gcloud run services describe ${SERVICE_NAME} --region=${REGION}"
echo "  Delete:       gcloud run services delete ${SERVICE_NAME} --region=${REGION}"
echo ""
