"""Thin client for a local, OpenAI-compatible chat server.

All calls go to the configured local endpoint only, so text is processed entirely
on-box and never reaches a cloud provider.
"""
import time
import requests
from config import CHAT_URL, CHAT_MODEL


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
    raise RuntimeError(f"local server call failed ({url}): {last}")


def chat(messages, temperature=0.1, max_tokens=1024):
    payload = {"model": CHAT_MODEL, "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens, "stream": False}
    out = _post(CHAT_URL + "/chat/completions", payload)
    return out["choices"][0]["message"]["content"].strip()


def health():
    """Return True if the local chat server responds on /health, else False.

    Most OpenAI-compatible local servers (llama.cpp, Ollama, LM Studio) expose a
    `/health` route. If yours doesn't, adapt this to hit `/v1/models` instead.
    """
    try:
        return requests.get(CHAT_URL.replace("/v1", "") + "/health", timeout=5).status_code == 200
    except Exception:
        return False
