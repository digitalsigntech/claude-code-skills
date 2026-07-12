# grammar-local — on-box grammar & style fixer (never touches the cloud)

## What it does
A private grammar/spelling/punctuation/clarity fixer that runs entirely against a
**local** language model. You give it text (CLI arg, stdin, or a chat prompt) and it
returns the corrected text — preserving meaning, facts, names, and numbers exactly.
It can optionally match a saved writing-style profile. Because the model call goes to
a localhost endpoint, the text you proofread **never leaves your machine**.

It ships three entry points:
- a **CLI** (`grammar "your text"`),
- a **`/grammar` slash command / editor hook** that intercepts grammar requests in
  Claude Code and answers them locally instead of sending your text to the cloud.

## How it works
| File | Role |
|------|------|
| `src/grammar` | Bash wrapper — resolves its own dir and runs `grammar.py` with the right Python. |
| `src/grammar.py` | Builds the editor system prompt (optionally + a style profile) and makes one local chat call. Prints only the corrected text. |
| `src/llm.py` | Thin client for a local, OpenAI-compatible chat server (`/v1/chat/completions`, `/health`). |
| `src/config.py` | Endpoint + model config, overridable by env vars. |
| `src/grammar_hook.py` | `UserPromptSubmit` hook: if a prompt looks like a grammar request, it runs the fix locally and **blocks** the cloud submission (fail-safe: if the local model is down, it blocks anyway and tells you — it never silently falls through to the cloud). |
| `src/writing-styles/default.example.md` | Template style profile (rename to activate). |

Flow: `grammar` → `grammar.py` → `llm.chat()` → local model server → corrected text.

## Prerequisites
- **Python 3** with the `requests` package (`pip install requests`). That's the only
  dependency.
- A **local, OpenAI-compatible chat server** running on localhost. Any of these work
  (bring your own): [`llama.cpp`](https://github.com/ggml-org/llama.cpp) `llama-server`,
  [Ollama](https://ollama.com/), [vLLM](https://github.com/vllm-project/vllm), or
  LM Studio. Any instruction-tuned chat model will do; a small local model (e.g. a
  7B-class instruct model) is plenty for grammar.
- Optional: [Claude Code](https://claude.com/claude-code) if you want the editor hook.

## Install / setup
1. Copy this folder somewhere stable, e.g. `~/tools/grammar-local`.
2. Make the scripts executable:
   ```bash
   chmod +x src/grammar src/grammar.py
   ```
3. Ensure `requests` is available to whatever Python runs the tool. If you use a venv,
   point the wrapper at it:
   ```bash
   export GRAMMAR_PYTHON=/path/to/venv/bin/python
   ```
4. Start your local model server and point the tool at it (see **Config**).
5. Test:
   ```bash
   ./src/grammar "this sentance have an mistake"
   echo "peice of text to fix" | ./src/grammar
   ```
6. (Optional) Activate a writing style: copy `src/writing-styles/default.example.md`
   to `src/writing-styles/default.md` and edit it, or add `myvoice.md` and run
   `grammar --style myvoice "..."`.
7. (Optional) Wire the editor hook into Claude Code `settings.json`:
   ```json
   {
     "hooks": {
       "UserPromptSubmit": [
         {"hooks": [{"type": "command",
                     "command": "python3 /abs/path/grammar-local/src/grammar_hook.py"}]}
       ]
     }
   }
   ```
   Now typing "check my grammar: ..." or "/grammar ..." in Claude Code runs locally
   and your text is never sent to the cloud.

## Config
| Knob | Where | Default | Meaning |
|------|-------|---------|---------|
| `GRAMMAR_LLM_URL` | env var | `http://127.0.0.1:8080/v1` | Base URL of your local OpenAI-compatible server (include `/v1`). |
| `GRAMMAR_LLM_MODEL` | env var | `local-model` | Model id to send in requests (match what your server expects). |
| `GRAMMAR_PYTHON` | env var | `python3` | Python interpreter the `grammar` wrapper uses (set to a venv with `requests`). |
| `GRAMMAR_HOOK_SKIP_MARKER` | env var | *(sentinel, never matches)* | If your setup injects relayed content into the chat, set this to a prefix that marks it, so the hook never intercepts those turns. |
| `--style <name>` | CLI flag | `default` | Load `writing-styles/<name>.md` (+ `learned-<name>.md` if present); `''` disables. |
| `--meta` | CLI flag | off | Treat the input as possibly starting with an instruction to ignore; fix only the message body. Used by the hook. |

## Caveats
- **Bring your own model backend.** The tool talks to any localhost server that
  exposes `/v1/chat/completions`. The default URL/model are placeholders — set
  `GRAMMAR_LLM_URL` / `GRAMMAR_LLM_MODEL` for your setup. `llm.health()` pings
  `/health`; if your server lacks that route, adapt it to hit `/v1/models`.
- **No credentials, no cloud.** There are no API keys or tokens; nothing is sent to a
  cloud provider. Keeping the endpoint on `127.0.0.1` is what makes it private.
- **The hook fails safe.** If the local model is unreachable, the hook still blocks the
  cloud submission and tells you to start your server — it will not leak your text.
- **What was stripped for release.** This is a genericized copy of an internal tool.
  Removed/omitted: the original was part of a larger local document-Q&A pipeline; only
  the grammar path is included here (embeddings/RAG/PDF-ingest code and the numpy
  dependency were dropped since grammar doesn't need them). Hardcoded local paths,
  a specific model/server port, and an organization-specific default writing-style
  profile were replaced with env-var config and a neutral example profile.
