"""Shared config for the local document pipeline.

Everything here runs on-box. The model servers expose an OpenAI-compatible API
(by default on localhost) — with a local backend, no document content leaves the
machine. Endpoints and model names are read from the environment so you can bring
your own model server (llama.cpp, vLLM, Ollama, or any OpenAI-compatible endpoint).
"""
import os

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # skill root
MODELS_DIR = os.path.join(BASE_DIR, "models")
STORE_DIR  = os.path.join(BASE_DIR, "store")
LOGS_DIR   = os.path.join(BASE_DIR, "logs")
# Where structured queries and PDF->MD conversions read/write Markdown docs.
KB_DIR     = os.environ.get("DOCPIPE_KB_DIR", os.path.join(BASE_DIR, "knowledge-base"))

# Model servers (OpenAI-compatible). Point these at any backend you like.
# Defaults assume two local servers: one chat/instruct model, one embedding model.
CHAT_URL   = os.environ.get("DOCPIPE_CHAT_URL", "http://127.0.0.1:18182/v1")
EMB_URL    = os.environ.get("DOCPIPE_EMB_URL",  "http://127.0.0.1:18183/v1")
CHAT_MODEL = os.environ.get("DOCPIPE_CHAT_MODEL", "local-chat")   # e.g. a 7B instruct model
EMB_MODEL  = os.environ.get("DOCPIPE_EMB_MODEL",  "local-embed")  # e.g. nomic-embed-text
# Embedding dimension MUST match your embedding model (e.g. 768 for nomic-embed-text-v1.5).
EMB_DIM    = int(os.environ.get("DOCPIPE_EMB_DIM", "768"))
# Some embedding models (nomic) want a task prefix; set to "0" to disable.
EMB_TASK_PREFIX = os.environ.get("DOCPIPE_EMB_TASK_PREFIX", "1") != "0"

# Retrieval / chunking defaults
CHUNK_CHARS   = 1200
CHUNK_OVERLAP = 150
TOP_K         = 10    # point lookups
AGG_TOP_K     = 48    # list/aggregation/numeric-filter questions need whole-table context

# Only genuine "enumerate the whole table" / numeric-filter phrasings — NOT generic
# "which"/"how many" (those are usually point lookups and over-trigger the slow path).
_AGG_HINTS = ("list every", "list all", "every ", "more than $", "less than $",
              "greater than $", "at least $", "over $", "under $", "cheapest",
              "most expensive", "average price", "all of the", "each of the")


def effective_k(question):
    """Aggregation/numeric-filter questions need many more chunks than point lookups."""
    ql = question.lower()
    return AGG_TOP_K if any(h in ql for h in _AGG_HINTS) else TOP_K
