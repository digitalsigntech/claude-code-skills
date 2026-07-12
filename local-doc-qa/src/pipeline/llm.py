"""Thin clients for the model servers (chat + embeddings).

Calls go to the endpoints configured in config.py (localhost by default). With a
local backend, document content is processed entirely on-box. Any OpenAI-compatible
server works — bring your own model endpoint.
"""
import time
import requests
import numpy as np
from config import CHAT_URL, EMB_URL, CHAT_MODEL, EMB_MODEL, EMB_TASK_PREFIX


def _post(url, payload, timeout=600, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"model server call failed ({url}): {last}")


def embed(texts, kind="document", batch=4):
    """Embed a list of texts.

    Some embedding models (e.g. nomic-embed) want a task prefix; controlled by
    EMB_TASK_PREFIX in config. Batches small (a CPU embedding server has a limited
    per-request token budget) and falls back to one-at-a-time if a batch errors,
    so ingestion never dies.
    """
    if isinstance(texts, str):
        texts = [texts]
    if EMB_TASK_PREFIX:
        prefix = "search_document: " if kind == "document" else "search_query: "
    else:
        prefix = ""
    inputs = [prefix + (t or " ")[:4000] for t in texts]
    out = []
    for i in range(0, len(inputs), batch):
        grp = inputs[i:i + batch]
        try:
            data = _post(EMB_URL + "/embeddings", {"model": EMB_MODEL, "input": grp})["data"]
            out.extend(d["embedding"] for d in data)
        except Exception:
            for one in grp:  # per-item fallback
                d = _post(EMB_URL + "/embeddings", {"model": EMB_MODEL, "input": [one]})["data"]
                out.append(d[0]["embedding"])
    vecs = np.array(out, dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


def chat(messages, temperature=0.1, max_tokens=1024):
    payload = {"model": CHAT_MODEL, "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens, "stream": False}
    out = _post(CHAT_URL + "/chat/completions", payload)
    return out["choices"][0]["message"]["content"].strip()


def health():
    """Return (chat_ok, emb_ok)."""
    def ok(base):
        try:
            return requests.get(base.replace("/v1", "") + "/health", timeout=5).status_code == 200
        except Exception:
            return False
    return ok(CHAT_URL), ok(EMB_URL)
