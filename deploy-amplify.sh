#!/usr/bin/env bash
# Deploy Vis Arena frontend to AWS Amplify (manual hosting / no Git)
# Usage: ./deploy-amplify.sh [--app-id <existing-app-id>]
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
APP_NAME="vis-arena"
BACKEND_URL="${VITE_API_URL:-http://44.248.40.235:8000}"
DIST_DIR="apps/web/dist"
BRANCH="main"

echo "==> Building frontend with backend URL: $BACKEND_URL"
cd "$(dirname "$0")"
VITE_API_URL="$BACKEND_URL" pnpm --filter vis-arena-web build

echo "==> Zipping build artifacts..."
cd "$DIST_DIR"
zip -qr /tmp/vis-arena-frontend.zip .
cd -

# Check for existing app
APP_ID="${1:-}"
if [ -z "$APP_ID" ] && [ -f ".amplify-app-id" ]; then
  APP_ID=$(cat .amplify-app-id)
fi

if [ -z "$APP_ID" ]; then
  echo "==> Creating new Amplify app..."
  APP_ID=$(aws amplify create-app \
    --name "$APP_NAME" \
    --region "$REGION" \
    --platform WEB \
    --environment-variables "VITE_API_URL=$BACKEND_URL" \
    --query 'app.appId' \
    --output text)
  echo "$APP_ID" > .amplify-app-id
  echo "    App ID: $APP_ID"

  echo "==> Creating branch '$BRANCH'..."
  aws amplify create-branch \
    --app-id "$APP_ID" \
    --branch-name "$BRANCH" \
    --region "$REGION" \
    --environment-variables "VITE_API_URL=$BACKEND_URL" \
    --no-cli-pager > /dev/null
else
  echo "==> Using existing Amplify app: $APP_ID"
fi

echo "==> Creating deployment..."
DEPLOY=$(aws amplify create-deployment \
  --app-id "$APP_ID" \
  --branch-name "$BRANCH" \
  --region "$REGION" \
  --output json)

JOB_ID=$(echo "$DEPLOY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['jobId'])")
UPLOAD_URL=$(echo "$DEPLOY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['zipUploadUrl'])")

echo "==> Uploading build (job $JOB_ID)..."
curl -s -T /tmp/vis-arena-frontend.zip "$UPLOAD_URL"

echo "==> Starting deployment..."
aws amplify start-deployment \
  --app-id "$APP_ID" \
  --branch-name "$BRANCH" \
  --job-id "$JOB_ID" \
  --region "$REGION" \
  --no-cli-pager > /dev/null

echo ""
echo "✓ Deployment started!"
echo "  App ID  : $APP_ID"
echo "  Region  : $REGION"
echo "  Branch  : $BRANCH"
echo "  Status  : https://$REGION.console.aws.amazon.com/amplify/home?region=$REGION#/$APP_ID/$BRANCH"
echo ""
echo "  Your frontend will be live at:"
echo "  https://$BRANCH.$APP_ID.amplifyapp.com"
echo ""
echo "  Backend (this EC2): $BACKEND_URL"
echo "  Make sure port 8000 is open in your EC2 security group."
