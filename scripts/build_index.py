"""Builds the FAISS index + chunks + manifest with the ACTIVE embedding backend.
Output artifacts are stamped; the app refuses to serve a mismatched set.
Usage: uv run python scripts/build_index.py [output_dir]   (default: dev_vectors)
"""
import json
import pickle
import sys

import faiss

from vector_store import make_embedder

chunks = [
    "Our support hours are 9am to 6pm IST, Monday through Friday.",
    "Refunds are processed within 5 business days of approval.",
    "The premium plan includes priority support and API access.",
]
# TODO: replace with BB ingestion (PDF/doc chunking) — same stamping applies.

out = sys.argv[1] if len(sys.argv) > 1 else "dev_vectors"
embedder = make_embedder()

emb = embedder.encode(chunks)
index = faiss.IndexFlatIP(embedder.dim)
index.add(emb)

faiss.write_index(index, f"{out}/index.faiss")
with open(f"{out}/chunks.pkl", "wb") as f:
    pickle.dump(chunks, f)
with open(f"{out}/manifest.json", "w") as f:
    json.dump({"embedder": embedder.name, "dim": embedder.dim, "chunks": len(chunks)}, f)

print(f"Built {len(chunks)} chunks with {embedder.name} -> {out}/")