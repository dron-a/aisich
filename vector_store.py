import io
import logging
import pickle
import tempfile

import boto3
import faiss
from sentence_transformers import SentenceTransformer

from config import settings

logger = logging.getLogger(__name__)


class VectorStore:
    """In-memory FAISS index, loaded once from S3. load() is blocking —
    main.py runs it in a thread after port binding (cold-start requirement)."""

    def __init__(self) -> None:
        self.index: faiss.Index | None = None
        self.chunks: list[str] = []
        self.model: SentenceTransformer | None = None

    def load(self) -> None:
        # vector_store.py — at the top of load():
        if settings.local_vector_dir:
            self.index = faiss.read_index(f"{settings.local_vector_dir}/index.faiss")
            with open(f"{settings.local_vector_dir}/chunks.pkl", "rb") as f:
                self.chunks = pickle.load(f)
            self.model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
            logger.info("Vector store loaded from disk: %d chunks", len(self.chunks))
            return
        # ...existing S3 path unchanged

        s3 = boto3.client(
            "s3",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
        with tempfile.NamedTemporaryFile(suffix=".faiss") as tmp:
            s3.download_fileobj(settings.s3_bucket, settings.s3_index_key, tmp)
            tmp.flush()
            self.index = faiss.read_index(tmp.name)

        buf = io.BytesIO()
        s3.download_fileobj(settings.s3_bucket, settings.s3_chunks_key, buf)
        buf.seek(0)
        # chunks.pkl comes from our own offline pipeline. Never point this at
        # an untrusted bucket — pickle deserialization executes code.
        self.chunks = pickle.load(buf)

        self.model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        logger.info("Vector store loaded: %d chunks", len(self.chunks))

    def search(self, query: str, top_k: int | None = None) -> list[str]:
        assert self.index is not None and self.model is not None, "Store not loaded"
        k = top_k or settings.rag_top_k
        emb = self.model.encode([query], normalize_embeddings=True)
        _, ids = self.index.search(emb, k)
        return [self.chunks[i] for i in ids[0] if 0 <= i < len(self.chunks)]


store = VectorStore()