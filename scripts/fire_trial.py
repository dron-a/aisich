"""Fake volume: phase 1 breadth (many numbers), phase 2 contention (one number, burst)."""
import asyncio, json, random, httpx

URL, SECRET = "http://localhost:8000/webhook", "dev-secret"
GROUP = "428011003957@g.us"

def payload(phone, text):
    return {"event": "messages.upsert", "data": {
        "key": {"remoteJid": GROUP, "fromMe": False,
                "participant": f"{phone}99@lid", "participantAlt": f"{phone}@s.whatsapp.net"},
        "message": {"conversation": f"@919999999999 {text}"},
        "contextInfo": {"mentionedJid": ["919999999999@s.whatsapp.net"]}}}

async def fire(client, phone, text):
    r = await client.post(URL, json=payload(phone, text), headers={"x-webhook-secret": SECRET})
    assert r.status_code == 200

async def main():
    async with httpx.AsyncClient(timeout=30) as c:
        # phase 1: 30 numbers x 3 queries
        await asyncio.gather(*(fire(c, f"9198000{i:05d}", f"q{n}")
                               for i in range(30) for n in range(3)))
        # phase 2: one number, 30 concurrent (must stop at quota=20 exactly)
        await asyncio.gather(*(fire(c, "919711122233", f"burst{n}") for n in range(30)))
    print("fired; check trial_usage counts")

asyncio.run(main())