import asyncio
import logging
from contextlib import asynccontextmanager
import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
# from litellm import exceptions as litellm_exc

import db
import evolution
import llm
import registration_commands
from config import settings
from vector_store import store
from cooldown import CooldownGate
# Define logging
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)


async def _load_store_background():
    try:
        # Simulate loading your vector store
        logger.info("Loading store...")
        await asyncio.sleep(0.5)
        logger.info("Store ready")
    except Exception:
        logger.exception("Store load failed — dyno cannot serve RAG")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: spawn background task
    asyncio.create_task(_load_store_background())
    yield
    # Shutdown (optional)
    logger.info("Shutting down.")


app = FastAPI(lifespan=lifespan)


@app.get("/")
def root():
    return {"status": "ok"}