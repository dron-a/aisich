"""Dev-only stand-in for Evolution's send endpoint.
Prints outbound bot messages to the console so the curl test loop
becomes fully observable without a real instance or phone.
Run:  uvicorn scripts.mock_evolution:app --port 8080
"""
from fastapi import FastAPI, Request

app = FastAPI()


@app.post("/message/sendText/{instance}")
async def send_text(instance: str, request: Request):
    body = await request.json()
    print(f"\n=== BOT REPLY (instance={instance}, to={body.get('number')}) ===")
    print(body.get("text"))
    print("=" * 50)
    return {"status": "ok"}