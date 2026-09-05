#!/usr/bin/env bash
# Kokoro for the local tier: the synthesiser for en es fr it ja pt zh on a
# CPU-only or AMD agent (the owner's decision, 2026-09-04: Kokoro wherever it
# speaks the language, Piper only where it does not). Puts it in ITS OWN venv
# beside the agent — kokoro-onnx pulls onnxruntime and friends, which do not
# belong in a distribution's site-packages on a box that is serving somebody —
# and prints the three environment lines the service needs.
#
#   ./install_kokoro.sh [DEST]          install into DEST (default: ~/kokoro)
#   ./install_kokoro.sh --check [DEST]  verify an existing install by hash
set -euo pipefail
MODEL_URL=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
VOICES_URL=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
MODEL_SHA=7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5
VOICES_SHA=bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d
check=0; [ "${1:-}" = "--check" ] && { check=1; shift; }
DEST="${1:-$HOME/kokoro}"; VENV="${KOKORO_VENV:-$DEST-venv}"
sha() { sha256sum "$1" | cut -d' ' -f1; }
if [ "$check" = 1 ]; then
  ok=1
  for f in "$DEST/kokoro-v1.0.onnx:$MODEL_SHA" "$DEST/voices-v1.0.bin:$VOICES_SHA"; do
    p="${f%%:*}"; want="${f##*:}"
    if [ -f "$p" ] && [ "$(sha "$p")" = "$want" ]; then echo "ok   $p"; else echo "BAD  $p"; ok=0; fi
  done
  "$VENV/bin/python" -c "import kokoro_onnx, onnxruntime, soundfile; print('ok   kokoro-onnx', kokoro_onnx.__version__ if hasattr(kokoro_onnx,'__version__') else '', 'onnxruntime', onnxruntime.__version__)" 2>/dev/null || { echo "BAD  venv $VENV"; ok=0; }
  [ "$ok" = 1 ] && echo "kokoro install verified" || { echo "kokoro install INCOMPLETE" >&2; exit 1; }
  exit 0
fi
mkdir -p "$DEST"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q "kokoro-onnx==0.6.1" soundfile
for f in "kokoro-v1.0.onnx:$MODEL_URL:$MODEL_SHA" "voices-v1.0.bin:$VOICES_URL:$VOICES_SHA"; do
  name="${f%%:*}"; rest="${f#*:}"; url="${rest%:*}"; want="${rest##*:}"
  if [ -f "$DEST/$name" ] && [ "$(sha "$DEST/$name")" = "$want" ]; then echo "have $name"; continue; fi
  echo "fetching $name"
  curl -fsSL -o "$DEST/$name.part" "$url" && mv "$DEST/$name.part" "$DEST/$name"
  [ "$(sha "$DEST/$name")" = "$want" ] || { echo "HASH MISMATCH for $name — refusing to keep it" >&2; rm -f "$DEST/$name"; exit 1; }
done
site="$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
echo
echo "# add to the agent's service environment:"
echo "Environment=LQ_KOKORO_SITE=$site"
echo "Environment=LQ_KOKORO_MODEL=$DEST/kokoro-v1.0.onnx"
echo "Environment=LQ_KOKORO_VOICES=$DEST/voices-v1.0.bin"
