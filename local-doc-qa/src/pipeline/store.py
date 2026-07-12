"""Tiny on-disk vector store (numpy). No external DB needed.

Persists to store/vectors.npy + store/meta.json. Cosine similarity over
L2-normalized embeddings (so search is a single matrix-vector product).
"""
import os, json, hashlib
import numpy as np
from config import STORE_DIR, EMB_DIM

VEC_PATH  = os.path.join(STORE_DIR, "vectors.npy")
META_PATH = os.path.join(STORE_DIR, "meta.json")


class VectorStore:
    def __init__(self):
        os.makedirs(STORE_DIR, exist_ok=True)
        if os.path.exists(VEC_PATH) and os.path.exists(META_PATH):
            self.vectors = np.load(VEC_PATH)
            with open(META_PATH) as f:
                self.meta = json.load(f)
        else:
            self.vectors = np.zeros((0, EMB_DIM), dtype=np.float32)
            self.meta = []

    def files(self):
        return sorted({m["source"] for m in self.meta})

    def has_file(self, source):
        return any(m["source"] == source for m in self.meta)

    def remove_file(self, source):
        keep = [i for i, m in enumerate(self.meta) if m["source"] != source]
        self.vectors = self.vectors[keep] if keep else np.zeros((0, EMB_DIM), np.float32)
        self.meta = [self.meta[i] for i in keep]

    def add(self, chunks, embeddings):
        """chunks: list of {source, locator, text}; embeddings: (N, dim)."""
        for c in chunks:
            cid = hashlib.sha1((c["source"] + c["locator"] + c["text"][:64]).encode()).hexdigest()[:12]
            self.meta.append({"id": cid, **c})
        self.vectors = np.vstack([self.vectors, embeddings.astype(np.float32)])

    def search(self, query_vec, k=6):
        if len(self.meta) == 0:
            return []
        sims = self.vectors @ query_vec.reshape(-1)      # cosine (normalized)
        idx = np.argsort(-sims)[:k]
        return [{**self.meta[i], "score": float(sims[i])} for i in idx]

    def save(self):
        np.save(VEC_PATH, self.vectors)
        with open(META_PATH, "w") as f:
            json.dump(self.meta, f)

    def stats(self):
        return {"files": len(self.files()), "chunks": len(self.meta)}
