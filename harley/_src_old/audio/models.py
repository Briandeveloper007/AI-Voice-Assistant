"""
src/audio/models.py — Model Download & Management
===================================================
Ensures the silero-vad ONNX model and openwakeword models are available
on disk before the audio pipeline starts.

All models are stored under ``data/models/`` relative to the project root.

CLAUDE.md invariants:
  • Never use print() — log to stderr / logging only.
  • Medium Integrity only — no elevation needed for downloads.
"""

from __future__ import annotations

import logging
import shutil
import urllib.request
from pathlib import Path
from typing import Final

log = logging.getLogger("harley.audio.models")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
MODELS_DIR: Final[Path] = _PROJECT_ROOT / "data" / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

SILERO_VAD_ONNX_PATH: Final[Path] = MODELS_DIR / "silero_vad.onnx"

# ---------------------------------------------------------------------------
# Silero-VAD ONNX model
# ---------------------------------------------------------------------------
# The v5 model from the official snakers4/silero-vad repo on HuggingFace.
_SILERO_VAD_URL: Final[str] = (
    "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
)


def ensure_silero_vad_model() -> Path:
    """
    Download ``silero_vad.onnx`` if it's not already present.

    Returns the absolute path to the model file.

    Raises
    ------
    RuntimeError
        If the download fails.
    """
    if SILERO_VAD_ONNX_PATH.exists():
        log.debug("Silero-VAD model already present: %s", SILERO_VAD_ONNX_PATH)
        return SILERO_VAD_ONNX_PATH

    log.info("Downloading Silero-VAD ONNX model to %s …", SILERO_VAD_ONNX_PATH)
    try:
        tmp_path = SILERO_VAD_ONNX_PATH.with_suffix(".onnx.tmp")
        urllib.request.urlretrieve(_SILERO_VAD_URL, str(tmp_path))
        shutil.move(str(tmp_path), str(SILERO_VAD_ONNX_PATH))
        log.info("Silero-VAD model downloaded successfully.")
    except Exception as exc:
        # Clean up partial download
        tmp_path = SILERO_VAD_ONNX_PATH.with_suffix(".onnx.tmp")
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Failed to download Silero-VAD model from {_SILERO_VAD_URL}: {exc}"
        ) from exc

    return SILERO_VAD_ONNX_PATH


# ---------------------------------------------------------------------------
# OpenWakeWord models
# ---------------------------------------------------------------------------

def ensure_wakeword_models() -> list[str]:
    """
    Ensure openwakeword's default models are available.

    openwakeword downloads its bundled models (``hey_jarvis``,
    ``alexa``, etc.) automatically on first ``Model()`` instantiation.
    This function triggers that download eagerly so the audio pipeline
    doesn't stall on first wake-word check.

    Returns a list of available model names.
    """
    try:
        from openwakeword.model import Model

        # Instantiate with defaults — triggers auto-download of bundled models.
        # inference_framework="onnx" avoids any tflite dependency.
        oww = Model(inference_framework="onnx")
        models = list(oww.models.keys())
        log.info("OpenWakeWord models ready: %s", models)
        return models
    except Exception as exc:
        log.error("Failed to initialise OpenWakeWord models: %s", exc)
        raise
