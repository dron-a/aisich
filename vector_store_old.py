import io
import json
import logging
import pickle
import tempfile
from typing import Protocol

import boto3
import numpy as np

import faiss

from config import settings

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    """The seam: index build and query encoding must use the same implementation.
    encode() returns L2-normalized float32 vectors, shape (n, dim)."""
    dim: int
    name: str

    def encode(self, texts: list[str]) -> np.ndarray: ...


class OnnxEmbedder:
    """all-MiniLM-L6-v2 via fastembed/onnxruntime — same model as before,
    torch-free runtime. Lazy init: model files pull from HF Hub on first use."""
    dim = 384
    name = "onnx:all-MiniLM-L6-v2"

    def __init__(self) -> None:
        self._model = None

    def _ensure(self):
        if self._model is None:
            from fastembed import TextEmbedding
            self._model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
        return self._model

    def encode(self, texts: list[str]) -> np.ndarray:
        vecs = np.array(list(self._ensure().embed(texts)), dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return vecs / np.clip(norms, 1e-12, None)   # normalize defensively; fastembed
                                                     # already normalizes this model, this
                                                     # makes the contract explicit


class RemoteEmbedder:
    """Stub for jina/gemini — implement encode() when the switch is triggered.
    See runbook: rebuild + re-upload index with the same backend before flipping."""
    def __init__(self, provider: str) -> None:
        self.name = f"remote:{provider}"
        self.dim = {"jina": 384, "gemini": 768}.get(provider, 0)
        self._provider = provider

    def encode(self, texts: list[str]) -> np.ndarray:
        raise NotImplementedError(
            f"RemoteEmbedder({self._provider}): wire the client here; "
            "requires settings.embedding_api_key"
        )


def make_embedder() -> Embedder:
    if settings.embedding_backend == "onnx":
        return OnnxEmbedder()
    if settings.embedding_backend == "remote":
        return RemoteEmbedder(settings.embedding_provider)
    raise ValueError(f"unknown embedding_backend: {settings.embedding_backend}")


class VectorStore:
    """In-memory FAISS index + chunks, loaded from S3 (or local dir in dev).
    Boot-time manifest check enforces embedder/index coherence — a config flip
    against a stale index fails LOUDLY here instead of retrieving garbage."""

    def __init__(self) -> None:
        self.index: faiss.Index | None = None
        self.chunks: list[str] = []
        self.embedder: Embedder = make_embedder()

    def _verify_manifest(self, manifest: dict) -> None:
        if manifest.get("embedder") != self.embedder.name or manifest.get("dim") != self.embedder.dim:
            raise RuntimeError(
                f"Index/embedder mismatch: index built with "
                f"{manifest.get('embedder')}(dim={manifest.get('dim')}), "
                f"active embedder is {self.embedder.name}(dim={self.embedder.dim}). "
                "Rebuild and re-upload the index with the active backend."
            )

    def load(self) -> None:
        if settings.local_vector_dir:
            import os
            if os.environ.get("DYNO"):
                raise RuntimeError("LOCAL_VECTOR_DIR must not be set in production")
            d = settings.local_vector_dir
            with open(f"{d}/manifest.json") as f:
                self._verify_manifest(json.load(f))
            self.index = faiss.read_index(f"{d}/index.faiss")
            with open(f"{d}/chunks.pkl", "rb") as f:
                self.chunks = pickle.load(f)
            self.embedder.encode(["warmup"])          # pull model files now, not on msg 1
            logger.info("Vector store loaded from disk: %d chunks", len(self.chunks))
            return

        s3 = boto3.client(
            "s3",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
        mbuf = io.BytesIO()
        s3.download_fileobj(settings.s3_bucket, settings.s3_manifest_key, mbuf)
        self._verify_manifest(json.loads(mbuf.getvalue()))

        with tempfile.NamedTemporaryFile(suffix=".faiss") as tmp:
            s3.download_fileobj(settings.s3_bucket, settings.s3_index_key, tmp)
            tmp.flush()
            self.index = faiss.read_index(tmp.name)

        buf = io.BytesIO()
        s3.download_fileobj(settings.s3_bucket, settings.s3_chunks_key, buf)
        buf.seek(0)
        # our own offline artifact; never point at untrusted buckets (pickle executes code)
        self.chunks = pickle.load(buf)

        self.embedder.encode(["warmup"])
        logger.info("Vector store loaded: %d chunks", len(self.chunks))

    def search(self, query: str, top_k: int | None = None) -> list[str]:
        assert self.index is not None, "Store not loaded"
        k = top_k or settings.rag_top_k
        emb = self.embedder.encode([query])
        _, ids = self.index.search(emb, k)
        return [self.chunks[i] for i in ids[0] if 0 <= i < len(self.chunks)]


store = VectorStore()