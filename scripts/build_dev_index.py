import pickle, faiss
from sentence_transformers import SentenceTransformer

chunks = [
    "Our support hours are 9am to 6pm IST, Monday through Friday.",
    "Refunds are processed within 5 business days of approval.",
    "The premium plan includes priority support and API access.",
]
model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
emb = model.encode(chunks, normalize_embeddings=True)
index = faiss.IndexFlatIP(emb.shape[1])
index.add(emb)
faiss.write_index(index, "dev_vectors/index.faiss")
with open("dev_vectors/chunks.pkl", "wb") as f:
    pickle.dump(chunks, f)
print(f"Built dev index: {len(chunks)} chunks")