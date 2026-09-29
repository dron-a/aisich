"""Builds ALL index formats + manifest with the ACTIVE embedding backend.
Every run writes vectors.npz + chunks.json (numpy) and index.faiss + chunks.pkl
(faiss) from the same embedding pass, so INDEX_BACKEND can be flipped in config
without a rebuild. The app refuses to serve a mismatched set.

Usage: uv run python scripts/build_index.py [output_dir] [--only numpy|faiss]
       (default: dev_vectors, all formats)
"""
import json
import os
import sys

import numpy as np

from vector_store import INDEX_BACKENDS, make_embedder, make_index

chunks = [
    "Our support hours are 9am to 6pm IST, Monday through Friday.",
    "Refunds are processed within 5 business days of approval.",
    "The premium plan includes priority support and API access.",
]
# TODO: replace with BB ingestion (PDF/doc chunking) — same stamping applies.=
def indexBuild(out_dir: str = "dev_vectors", only: str | None = None) -> None:
    formats = [only] if only else list(INDEX_BACKENDS)
    os.makedirs(out_dir, exist_ok=True)

    embedder = make_embedder()
    emb = embedder.encode(chunks)
    assert emb.shape == (len(chunks), embedder.dim), emb.shape
    assert np.allclose(np.linalg.norm(emb, axis=1), 1.0, atol=1e-4), "vectors not normalized"

    for name in formats:
        make_index(name).save(emb, chunks, out_dir)

    with open(f"{out_dir}/manifest.json", "w") as f:
        json.dump(
            {
                "embedder": embedder.name,
                "dim": embedder.dim,
                "chunks": len(chunks),
                "index_formats": formats,
            },
            f,
            indent=2,
        )

    print(f"Built {len(chunks)} chunks with {embedder.name} in formats {formats} -> {out_dir}/")




def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = args[0] if args else "dev_vectors"
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    indexBuild(out, only)


if __name__ == "__main__":
    main()