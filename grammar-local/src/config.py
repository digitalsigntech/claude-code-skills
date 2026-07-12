"""Config for the local grammar/style fixer.

Points at a LOCAL, OpenAI-compatible chat server so text never leaves your box.
The backend is swappable — llama.cpp `llama-server`, Ollama, vLLM, LM Studio, or any
server that exposes `/v1/chat/completions`. Override the endpoint and model name with
environment variables (bring your own endpoint):

    GRAMMAR_LLM_URL    base URL incl. /v1  (default http://127.0.0.1:8080/v1)
    GRAMMAR_LLM_MODEL  model id to request (default "local-model")
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Local, OpenAI-compatible chat server (localhost by default). Bring your own.
CHAT_URL   = os.environ.get("GRAMMAR_LLM_URL", "http://127.0.0.1:8080/v1")
CHAT_MODEL = os.environ.get("GRAMMAR_LLM_MODEL", "local-model")
