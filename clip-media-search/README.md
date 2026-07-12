# CLIP Media Search

Neural, annotation-aware image search over a local media / knowledge-base folder.
Find an image by **what it looks like** or by **what you said about it** — a single
text query is embedded into the joint CLIP space and matched against both the image
embedding and a human-written annotation embedding; the better score wins. You can
also search by example (pass an image path as the query) for reverse/visual lookup.
Everything runs locally on CPU — no image or query ever leaves the machine.

## What it does

- `add` an image, a video, or a whole directory with an optional annotation + tags.
  Each item gets two vectors: its visual embedding and its annotation-text embedding.
  Videos (`.mp4/.mov/.webm/.m4v`) are embedded via their middle frame (requires
  ffmpeg); the index stores the video path, so hits return the clip itself.
- `find "query"` returns the top-k images by cosine similarity, labelling each hit
  as a `visual` or `annotation` match.
- A warm HTTP server keeps the model + index resident so lookups are sub-second.
- Optional: `show_media.py` sends the matching images/videos into a chat backend
  (photos via `sendPhoto`, videos via `sendVideo`). Hits scoring below
  `--ratio` (default 0.8) of the top hit are dropped, so one strong match
  doesn't drag loosely-related media into the chat.

## How it works

| File | Role |
|------|------|
| `src/pipeline/clip_media.py` | Core: embed images/text, the `MediaStore` (numpy vectors + JSON metadata), and the `add` / `find` / `list` CLI. |
| `src/pipeline/clip_server.py` | Warm localhost HTTP server (`/find`, `/health`). Auto-reloads the index when new images are added. |
| `src/media` | Bash CLI wrapper around `clip_media.py`. |
| `src/start_clip_server.sh` | Launches the warm server as a single instance (flock); good for `@reboot` + watchdog cron. |
| `src/show_media.py` | Optional integration: query the warm server and push the result images into a chat. |
| `config.example.env` | Template for the optional env-var configuration. |

The index lives in `CLIP_STORE_DIR` as three files: `media_img_vecs.npy`,
`media_ann_vecs.npy`, and `media_meta.json`. Re-indexing an existing path is
idempotent (old vectors are removed first).

## Prerequisites

- Python 3.9+
- `pip install fastembed numpy` (fastembed pulls in ONNX runtime; first run
  downloads the CLIP model weights, then stays offline).
- No GPU required — CLIP ViT-B/32 runs on CPU.

## Install / setup

```bash
cd clip-media-search/src
python3 -m venv venv && ./venv/bin/pip install fastembed numpy
chmod +x media start_clip_server.sh

# index some images
./media add ~/photos/forklift.jpg --annotation "red forklift on a loading dock" --tags "warehouse,vehicle"
./media add ~/photos/          # index a whole directory (visual-only if no annotation)

# search
./media find "loading dock" -k 5
./media find ~/photos/other.jpg   # reverse image search

# (optional) run the warm server for sub-second lookups
./start_clip_server.sh &
curl "http://127.0.0.1:8477/find?q=loading%20dock&k=5"
```

Copy `config.example.env` to `config.env` and edit if you want non-default paths,
a different model, PDF handling, or the chat integration.

## Config

All optional; defaults derive from the script location.

| Env var | Default | Purpose |
|---------|---------|---------|
| `MEDIA_DIR` | `<src>/media` | Where indexed images are copied. |
| `CLIP_STORE_DIR` | `<src>/store` | Vector index + metadata. |
| `CLIP_SERVER_PORT` | `8477` | Warm-server port. |
| `CLIP_IMG_MODEL` | `Qdrant/clip-ViT-B-32-vision` | Image embedding model. |
| `CLIP_TXT_MODEL` | `Qdrant/clip-ViT-B-32-text` | Text embedding model. |
| `CLIP_DIM` | `512` | Embedding dimension (match your model). |
| `DOCPIPE` | *(unset)* | Optional executable to ingest PDFs; unset = skip PDFs. |
| `CHAT_BACKEND_PATH` | *(unset)* | Dir with a `tg_api` chat module for `show_media.py`. |
| `CHAT_ID` | `123456789` | Default destination chat for `show_media.py`. |

## Caveats

- **Swappable model backend.** The default is CLIP ViT-B/32 via `fastembed`. Any
  fastembed-compatible image/text pair that shares one embedding space works —
  set `CLIP_IMG_MODEL` / `CLIP_TXT_MODEL` (and `CLIP_DIM` to match). Change the
  model and you must re-index (old vectors live in a different space).
- **PDFs are out of scope here.** In the original setup the `add` command handed
  PDFs to a separate text-RAG document pipeline via the `DOCPIPE` hook. That
  pipeline is a different tool and is **not** included. Leave `DOCPIPE` unset and
  PDFs are skipped; images are unaffected.
- **`show_media.py` needs a chat backend.** It imports a `tg_api` module exposing
  `send_message(...)` and `_call("sendPhoto", ...)` — supply your own (e.g. a
  Telegram Bot API client) via `CHAT_BACKEND_PATH`. It's optional; `./media find`
  gives plain text output with no backend.
- **What was stripped for sharing:** the populated index/store, the real media
  folder, and all deployment-specific paths, chat IDs, and product wording from
  the internal version. Bring your own images, annotations, and (optionally)
  credentials.
