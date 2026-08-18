#!/usr/bin/env bash
set -euo pipefail
# Usage: EVOLUTION_API_KEY=... WEBHOOK_SECRET=... ./scripts/setup_instance.sh devinstance http://host.docker.internal:8000
INSTANCE="${1:?instance name}"
BOT_URL="${2:?bot base url}"
EVO="http://localhost:8080"

curl -sf -X POST "$EVO/instance/create" \
  -H "apikey: $EVOLUTION_API_KEY" -H "Content-Type: application/json" \
  -d "{\"instanceName\":\"$INSTANCE\",\"integration\":\"WHATSAPP-BAILEYS\"}" \
  || echo "instance may already exist, continuing"

curl -sf -X POST "$EVO/webhook/set/$INSTANCE" \
  -H "apikey: $EVOLUTION_API_KEY" -H "Content-Type: application/json" \
  -d "{\"webhook\":{\"enabled\":true,\"url\":\"$BOT_URL/webhook\",\"headers\":{\"x-webhook-secret\":\"$WEBHOOK_SECRET\"},\"events\":[\"MESSAGES_UPSERT\"]}}"

echo ""
curl -sf "$EVO/webhook/find/$INSTANCE" -H "apikey: $EVOLUTION_API_KEY"