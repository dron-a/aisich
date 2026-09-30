#!/usr/bin/env bash
set -euo pipefail
# Usage: EVOLUTION_API_KEY=... WEBHOOK_SECRET=... ./scripts/setup_instance.sh devinstance http://host.docker.internal:8000
INSTANCE="${1:?instance name}"
BOT_URL="${2:?bot base url}"
EVO="${EVO:-http://localhost:8080}"

curl -sf -X POST "$EVO/instance/create" \
  -H "apikey: $EVOLUTION_API_KEY" -H "Content-Type: application/json" \
  -d "{\"instanceName\":\"$INSTANCE\",\"integration\":\"WHATSAPP-BAILEYS\"}" \
  || echo "instance may already exist, continuing"

curl -sf -X POST "$EVO/webhook/set/$INSTANCE" \
  -H "apikey: $EVOLUTION_API_KEY" -H "Content-Type: application/json" \
  -d "{\"webhook\":{\"enabled\":true,\"url\":\"$BOT_URL/webhook\",\"headers\":{\"x-webhook-secret\":\"$WEBHOOK_SECRET\"},\"events\":[\"MESSAGES_UPSERT\"]}}"

echo ""
curl -sf "$EVO/webhook/find/$INSTANCE" -H "apikey: $EVOLUTION_API_KEY"

# setup on OCI ########################
EVO=http://localhost:8085

# 1. Create the instance
curl -s -X POST "$EVO/v1/instance/create" \
  -H "apikey: miaulocal123" \
  -H "Content-Type: application/json" \
  -d '{"instanceName":"miautest"}'

# 2. Register the webhook using the internal Docker service name ("chatbot")
curl -s -X POST "$EVO/v1/webhook/set/miautest" \
  -H "apikey: miaulocal123" \
  -H "Content-Type: application/json" \
  -d '{
    "webhook": {
      "enabled": true,
      "url": "http://chatbot:8000/webhook",
      "headers": {
        "x-webhook-secret": "dev-secret-123"
      },
      "events": ["MESSAGES_UPSERT"]
    }
  }'

# 3. Verify
curl -s "$EVO/v1/webhook/find/miautest" -H "apikey: miaulocal123"

#check instannc state
curl -s "http://localhost:8085/v1/instance/connectionState/miautest" -H "apikey: miaulocal123"

# create qr
curl -s "http://localhost:8085/v1/instance/connect/miautest/image" -H "apikey: miaulocal123" -o qr.png && open qr.png
# or
ssh ubuntu@<your url> "curl -s http://127.0.0.1:8080/instance/qrcode_png" > qr.png && open qr.png