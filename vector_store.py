import io
import json
import logging
import os
import tempfile
from typing import Protocol

import boto3
import numpy as np

from config import settings

# -------- code for async vector search to mode all searches to a dedicated thread ----------------
# import asyncio
# from concurrent.futures import ThreadPoolExecutor
# _search_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embed-search")
# -------------------------------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# Pin BLAS/OMP threads before any numeric lib spins up a pool. On a 1-2 vCPU
# dyno onnxruntime and OpenBLAS otherwise contend for the same cores.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")


# --------------------------------------------------------------------------- #
# Embedders (unchanged contract)
# --------------------------------------------------------------------------- #
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
            self._model = TextEmbedding(
                "sentence-transformers/all-MiniLM-L6-v2",
                cache_dir=settings.model_cache_dir or None,   # bake into image; avoid HF pull at boot
                threads=1,
            )
        return self._model

    def encode(self, texts: list[str]) -> np.ndarray:
        vecs = np.array(list(self._ensure().embed(texts)), dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return vecs / np.clip(norms, 1e-12, None)   # normalize defensively; fastembed
                                                     # already normalizes this model, this
                                                     # makes the contract explicit


class RemoteEmbedder:
    """Stub for jina/gemini — implement encode() when the switch is triggered.
    See runbook: rebuild + re-upload index with the same backend before flipping.
    V2 stub. NOTE: dims below were wrong; corrected to real model sizes."""
    def __init__(self, provider: str) -> None:
        self.name = f"remote:{provider}"
        self.dim = {"jina-v2-small": 512, "jina-v3": 1024, "gemini": 768}.get(provider, 0)
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


# --------------------------------------------------------------------------- #
# Index backends
# --------------------------------------------------------------------------- #
class Index(Protocol):
    """Exact cosine search over L2-normalized vectors.
    Artifact names are fixed per backend so the S3 layout is predictable."""
    name: str
    artifacts: dict[str, str]          # {"index": filename, "chunks": filename}

    @property
    def size(self) -> int: ...
    def load(self, blobs: dict[str, bytes]) -> None: ...
    def save(self, vectors: np.ndarray, chunks: list[str], out_dir: str) -> None: ...
    def search(self, query_vec: np.ndarray, k: int) -> list[int]: ...
    def chunk(self, i: int) -> str: ...


class NumpyIndex:
    """Vectors as float32 .npz, chunks as JSON. No extra deps beyond numpy."""
    name = "numpy"
    artifacts = {"index": "vectors.npz", "chunks": "chunks.json"}

    def __init__(self) -> None:
        self.vectors: np.ndarray | None = None
        self.chunks: list[str] = []

    @property
    def size(self) -> int:
        return len(self.chunks)

    def load(self, blobs: dict[str, bytes]) -> None:
        self.vectors = np.load(io.BytesIO(blobs["index"]))["vectors"].astype(np.float32, copy=False)
        self.chunks = json.loads(blobs["chunks"])
        if self.vectors.shape[0] != len(self.chunks):
            raise RuntimeError(f"vectors ({self.vectors.shape[0]}) != chunks ({len(self.chunks)})")

    def save(self, vectors: np.ndarray, chunks: list[str], out_dir: str) -> None:
        np.savez_compressed(f"{out_dir}/{self.artifacts['index']}", vectors=vectors.astype(np.float32))
        with open(f"{out_dir}/{self.artifacts['chunks']}", "w", encoding="utf-8") as f:
            json.dump(chunks, f, ensure_ascii=False)


    def search(self, query_vec: np.ndarray, k: int) -> list[int]:
        assert self.vectors is not None
        n = self.vectors.shape[0]
        if k <= 0 or n == 0:
            return []
        k = min(k, n)
        scores = self.vectors @ query_vec
        top = np.argpartition(scores, n - k)[n - k:]
        return top[np.argsort(scores[top])][::-1].tolist()

    def chunk(self, i: int) -> str:
        return self.chunks[i]


class FaissIndex:
    """Original backend. faiss is imported lazily so the numpy path never loads it."""
    name = "faiss"
    artifacts = {"index": "index.faiss", "chunks": "chunks.pkl"}

    def __init__(self) -> None:
        self.index = None
        self.chunks: list[str] = []

    @property
    def size(self) -> int:
        return len(self.chunks)

    def load(self, blobs: dict[str, bytes]) -> None:
        import faiss
        import pickle
        self.index = faiss.deserialize_index(np.frombuffer(blobs["index"], dtype=np.uint8))
        # our own offline artifact; never point at untrusted buckets (pickle executes code)
        self.chunks = pickle.loads(blobs["chunks"])
        if self.index.ntotal != len(self.chunks):
            raise RuntimeError(f"index ntotal ({self.index.ntotal}) != chunks ({len(self.chunks)})")

    def save(self, vectors: np.ndarray, chunks: list[str], out_dir: str) -> None:
        import faiss
        import pickle
        idx = faiss.IndexFlatIP(vectors.shape[1])
        idx.add(np.ascontiguousarray(vectors, dtype=np.float32))
        faiss.write_index(idx, f"{out_dir}/{self.artifacts['index']}")
        with open(f"{out_dir}/{self.artifacts['chunks']}", "wb") as f:
            pickle.dump(chunks, f)

    def search(self, query_vec: np.ndarray, k: int) -> list[int]:
        assert self.index is not None
        _, ids = self.index.search(query_vec[None, :].astype(np.float32), k)
        return [int(i) for i in ids[0] if i >= 0]

    def chunk(self, i: int) -> str:
        return self.chunks[i]


INDEX_BACKENDS: dict[str, type[Index]] = {"numpy": NumpyIndex, "faiss": FaissIndex}


def make_index(backend: str | None = None) -> Index:
    backend = backend or settings.index_backend
    try:
        return INDEX_BACKENDS[backend]()
    except KeyError:
        raise ValueError(f"unknown index_backend: {backend!r} (choose from {list(INDEX_BACKENDS)})")


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class VectorStore:
    """Loads manifest + the artifact set for settings.index_backend from S3 (or
    local dir in dev). Boot fails loudly on embedder mismatch or missing format."""

    def __init__(self) -> None:
        self.embedder: Embedder = make_embedder()
        self.index: Index = make_index()

    def _verify_manifest(self, manifest: dict) -> None:
        if manifest.get("embedder") != self.embedder.name or manifest.get("dim") != self.embedder.dim:
            raise RuntimeError(
                f"Index/embedder mismatch: index built with "
                f"{manifest.get('embedder')}(dim={manifest.get('dim')}), "
                f"active embedder is {self.embedder.name}(dim={self.embedder.dim}). "
                "Rebuild and re-upload the index with the active backend (use build_index.py)."
            )
        formats = manifest.get("index_formats", ["faiss"])   # pre-migration manifests are faiss-only
        if self.index.name not in formats:
            raise RuntimeError(
                f"index_backend={self.index.name!r} but manifest only has {formats}. "
                "Rebuild the index (build_index.py writes all formats)."
            )

    def _read_local(self, d: str) -> dict[str, bytes]:
        blobs = {}
        for key, fname in self.index.artifacts.items():
            with open(f"{d}/{fname}", "rb") as f:
                blobs[key] = f.read()
        with open(f"{d}/manifest.json", "rb") as f:
            blobs["manifest"] = f.read()
        return blobs

    def _read_s3(self) -> dict[str, bytes]:
        s3 = boto3.client(
            "s3",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
        def get(fname: str) -> bytes:
            buf = io.BytesIO()
            s3.download_fileobj(settings.s3_bucket, f"{settings.s3_prefix}{fname}", buf)
            return buf.getvalue()
        blobs = {key: get(fname) for key, fname in self.index.artifacts.items()}
        blobs["manifest"] = get("manifest.json")
        return blobs

    def load(self) -> None:
        if settings.local_vector_dir:
            if os.environ.get("DYNO"):
                raise RuntimeError("LOCAL_VECTOR_DIR must not be set in production")
            blobs = self._read_local(settings.local_vector_dir)
            src = settings.local_vector_dir
        else:
            blobs = self._read_s3()
            src = f"s3://{settings.s3_bucket}/{settings.s3_prefix}"

        self._verify_manifest(json.loads(blobs["manifest"]))
        self.index.load(blobs)
        self.embedder.encode(["warmup"])
        logger.info("Vector store loaded for [%s] from %s: %d chunks", self.index.name, src, self.index.size)

    def search(self, query: str, top_k: int | None = None) -> list[str]:
        if self.index.size == 0:
            raise RuntimeError("Store not loaded")
        k = top_k or settings.rag_top_k
        q = self.embedder.encode([query])[0]
        return [self.index.chunk(i) for i in self.index.search(q, k)]

    def reload(self) -> None:
        """Build a fresh index and swap it in with one assignment, so a
        concurrent search sees the old index or the new one, never a mix."""
        fresh = make_index()
        blobs = (self._read_local(settings.local_vector_dir)
                 if settings.local_vector_dir else self._read_s3())
        self._verify_manifest(json.loads(blobs["manifest"]))
        fresh.load(blobs)
        del blobs
        self.index = fresh

    # -------- code for async vector search to mode all searches to a dedicated thread--------------------
    # async def search_async(self, query: str, top_k: int | None = None) -> list[str]:
    #     loop = asyncio.get_running_loop()
    #     return await loop.run_in_executor(_search_executor, self.search, query, top_k)
    # ----------------------------------------------------------------------------------------------------


store = VectorStore()