"""
src/audio/capture.py — Audio Capture Pipeline (Process 1)
==========================================================
Implements the full audio pipeline described in docs/specs/01_process_ipc.md:

  1. openWakeWord listens for a wake word.
  2. On detection, silero-vad + a 1.5 s ring buffer capture the utterance.
  3. Once silero-vad detects 700 ms of continuous silence, the ring buffer
     contents are pushed as raw PCM bytes to the Orchestrator via event_queue.

Audio parameters
-----------------
  Sample rate : 16 000 Hz  (required by both silero-vad and faster-whisper)
  Channels    : 1 (mono)
  Dtype       : int16 (PCM)  — converted to float32 for VAD inference

CLAUDE.md invariants:
  • Process Isolation — this runs in a dedicated multiprocessing.Process.
  • Never use print() — log to stderr / logging only.
  • multiprocessing.Queue for all IPC.
"""

from __future__ import annotations

import logging
import multiprocessing
import queue
import sys
import time
from enum import Enum, auto
from multiprocessing import Event, Queue
from typing import Final

import numpy as np

log = logging.getLogger("harley.audio.capture")

# ---------------------------------------------------------------------------
# Audio constants
# ---------------------------------------------------------------------------
SAMPLE_RATE: Final[int] = 16_000          # Hz — required by silero & whisper
CHANNELS: Final[int] = 1                   # mono
DTYPE = np.int16                            # PCM format

# Ring buffer: 1.5 seconds of audio at 16 kHz
RING_BUFFER_SECONDS: Final[float] = 1.5
RING_BUFFER_SAMPLES: Final[int] = int(SAMPLE_RATE * RING_BUFFER_SECONDS)  # 24 000

# Silero-VAD operates on 512-sample chunks (32 ms at 16 kHz)
VAD_CHUNK_SAMPLES: Final[int] = 512
VAD_CHUNK_SECONDS: Final[float] = VAD_CHUNK_SAMPLES / SAMPLE_RATE  # 0.032

# OpenWakeWord operates on 1280-sample chunks (80 ms at 16 kHz)
OWW_CHUNK_SAMPLES: Final[int] = 1280

# Silence detection: 700 ms of continuous non-speech
SILENCE_DURATION_SECONDS: Final[float] = 0.7
SILENCE_CHUNKS_THRESHOLD: Final[int] = int(
    SILENCE_DURATION_SECONDS / VAD_CHUNK_SECONDS
)  # ≈ 22 chunks

# VAD speech probability threshold
VAD_SPEECH_THRESHOLD: Final[float] = 0.5

# Wake word detection threshold
WAKEWORD_THRESHOLD: Final[float] = 0.5


# ---------------------------------------------------------------------------
# Ring Buffer
# ---------------------------------------------------------------------------

class RingBuffer:
    """
    Fixed-size circular buffer for int16 PCM audio samples.

    Maintains a 1.5 s window of the most recent audio.  When the buffer
    is full, new samples overwrite the oldest data.

    Thread-safety: NOT thread-safe.  Designed to be called only from the
    audio pipeline's main loop (single thread within Process 1).
    """

    __slots__ = ("_buf", "_capacity", "_write_pos", "_filled")

    def __init__(self, capacity: int = RING_BUFFER_SAMPLES) -> None:
        self._buf = np.zeros(capacity, dtype=DTYPE)
        self._capacity = capacity
        self._write_pos = 0
        self._filled = False

    @property
    def is_full(self) -> bool:
        """True once the buffer has been completely written at least once."""
        return self._filled

    def append(self, chunk: np.ndarray) -> None:
        """
        Write *chunk* (1-D int16 array) into the ring buffer.

        If chunk is larger than remaining space, it wraps around.
        """
        n = len(chunk)
        if n == 0:
            return

        end = self._write_pos + n
        if end <= self._capacity:
            self._buf[self._write_pos:end] = chunk
        else:
            # Wrap-around write
            first = self._capacity - self._write_pos
            self._buf[self._write_pos:] = chunk[:first]
            remainder = n - first
            self._buf[:remainder] = chunk[first:]

        self._write_pos = end % self._capacity
        if end >= self._capacity:
            self._filled = True

    def get_contents(self) -> np.ndarray:
        """
        Return the full buffer contents in chronological order.

        If the buffer hasn't wrapped yet, returns only the data written so far.
        """
        if not self._filled:
            return self._buf[:self._write_pos].copy()
        # Wrapped — oldest data starts at _write_pos
        return np.concatenate([
            self._buf[self._write_pos:],
            self._buf[:self._write_pos],
        ])

    def clear(self) -> None:
        """Zero the buffer and reset position."""
        self._buf[:] = 0
        self._write_pos = 0
        self._filled = False


# ---------------------------------------------------------------------------
# Silero-VAD (ONNX — no PyTorch dependency)
# ---------------------------------------------------------------------------

class SileroVAD:
    """
    Lightweight wrapper around the Silero-VAD v5 ONNX model.

    Expects 512-sample (32 ms) chunks of float32 audio at 16 kHz.
    Returns a speech probability ∈ [0.0, 1.0].

    The model is *stateful* — it maintains internal hidden-state tensors
    across calls.  Call ``reset_state()`` between separate utterances.
    """

    def __init__(self, model_path: str) -> None:
        import onnxruntime as ort

        self._session = ort.InferenceSession(
            model_path,
            providers=["CPUExecutionProvider"],
        )
        # Identify input names from the model
        self._input_names = [inp.name for inp in self._session.get_inputs()]
        log.debug("Silero-VAD ONNX inputs: %s", self._input_names)

        # Initialise internal state (h, c tensors for the LSTM)
        # Silero-VAD v5 uses 2 layers × 1 batch × 64 hidden units
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)
        self._sr = np.array([SAMPLE_RATE], dtype=np.int64)

    def reset_state(self) -> None:
        """Reset LSTM hidden state between utterances."""
        self._h[:] = 0.0
        self._c[:] = 0.0

    def is_speech(self, chunk_int16: np.ndarray) -> float:
        """
        Run VAD inference on a 512-sample int16 chunk.

        Parameters
        ----------
        chunk_int16 : np.ndarray
            Shape (512,), dtype int16.

        Returns
        -------
        float
            Speech probability ∈ [0.0, 1.0].
        """
        # Convert int16 → float32 normalised to [-1.0, 1.0]
        audio = chunk_int16.astype(np.float32) / 32768.0
        audio = audio.reshape(1, -1)  # (1, 512)

        # Build input dict — silero_vad.onnx expects: input, sr, h, c
        feeds = {
            "input": audio,
            "sr": self._sr,
            "h": self._h,
            "c": self._c,
        }

        # Run inference
        output, h_new, c_new = self._session.run(None, feeds)

        # Update state
        self._h = h_new
        self._c = c_new

        prob = float(output[0][0])
        return prob


# ---------------------------------------------------------------------------
# Pipeline state machine
# ---------------------------------------------------------------------------

class PipelineState(Enum):
    """Audio pipeline states."""
    WAITING_FOR_WAKE = auto()
    LISTENING = auto()


class AudioPipeline:
    """
    State machine driving the audio capture pipeline.

    States
    ------
    WAITING_FOR_WAKE
        Feeding 80 ms frames to openWakeWord.  On wake-word detection,
        transitions to LISTENING.

    LISTENING
        Feeding 32 ms chunks to silero-vad while appending all audio to
        the ring buffer.  Tracks consecutive silence frames.  After 700 ms
        of silence, pushes the ring buffer contents to event_queue and
        transitions back to WAITING_FOR_WAKE.
    """

    def __init__(
        self,
        event_queue: "Queue[bytes]",
        vad: SileroVAD,
        oww_model: object,  # openwakeword.model.Model
    ) -> None:
        self._event_queue = event_queue
        self._vad = vad
        self._oww = oww_model
        self._ring = RingBuffer()
        self._state = PipelineState.WAITING_FOR_WAKE
        self._silence_chunks = 0
        self._speech_detected = False  # Have we seen any speech in this utterance?

    @property
    def state(self) -> PipelineState:
        return self._state

    def feed_oww(self, chunk_int16: np.ndarray) -> bool:
        """
        Feed an 80 ms (1280-sample) chunk to openWakeWord.

        Returns True if a wake word was detected above threshold.
        """
        # openwakeword expects int16 numpy array or list
        predictions = self._oww.predict(chunk_int16)

        for model_name, score in predictions.items():
            if score >= WAKEWORD_THRESHOLD:
                log.info(
                    "🎤 Wake word detected: model=%r score=%.3f",
                    model_name, score,
                )
                return True
        return False

    def feed_vad(self, chunk_int16: np.ndarray) -> None:
        """
        Feed a 32 ms (512-sample) chunk through VAD while in LISTENING state.

        Appends audio to the ring buffer and tracks silence duration.
        When 700 ms of silence is reached (after at least some speech),
        pushes the buffer and resets.
        """
        # Always append to ring buffer while listening
        self._ring.append(chunk_int16)

        prob = self._vad.is_speech(chunk_int16)

        if prob >= VAD_SPEECH_THRESHOLD:
            # Speech detected — reset silence counter
            self._silence_chunks = 0
            self._speech_detected = True
            log.debug("VAD: speech (prob=%.2f)", prob)
        else:
            self._silence_chunks += 1
            log.debug(
                "VAD: silence %d/%d (prob=%.2f)",
                self._silence_chunks, SILENCE_CHUNKS_THRESHOLD, prob,
            )

        # Only trigger on silence *after* we've seen speech
        if self._speech_detected and self._silence_chunks >= SILENCE_CHUNKS_THRESHOLD:
            self._push_utterance()

    def _push_utterance(self) -> None:
        """Extract the ring buffer contents and push to the Orchestrator."""
        pcm = self._ring.get_contents()
        pcm_bytes = pcm.tobytes()

        log.info(
            "📤 Utterance captured: %d samples (%.2f s) — pushing to Orchestrator.",
            len(pcm),
            len(pcm) / SAMPLE_RATE,
        )

        try:
            self._event_queue.put_nowait(pcm_bytes)
        except Exception:
            log.warning("event_queue is full — dropping utterance.")

        self._reset_to_waiting()

    def _reset_to_waiting(self) -> None:
        """Reset all state and return to WAITING_FOR_WAKE."""
        self._state = PipelineState.WAITING_FOR_WAKE
        self._silence_chunks = 0
        self._speech_detected = False
        self._ring.clear()
        self._vad.reset_state()
        # Reset openwakeword internal scores
        self._oww.reset()
        log.debug("Pipeline reset → WAITING_FOR_WAKE.")

    def transition_to_listening(self) -> None:
        """Transition from WAITING_FOR_WAKE to LISTENING."""
        self._state = PipelineState.LISTENING
        self._silence_chunks = 0
        self._speech_detected = False
        self._ring.clear()
        self._vad.reset_state()
        log.info("Pipeline → LISTENING (recording utterance).")


# ---------------------------------------------------------------------------
# Entry point for Process 1
# ---------------------------------------------------------------------------

def run_audio_pipeline(
    event_queue: "Queue[bytes]",
    shutdown_event: "Event",
) -> None:
    """
    Main entry point for the Audio Worker (Process 1).

    Opens a sounddevice InputStream at 16 kHz / mono / int16 and drives
    the AudioPipeline state machine until shutdown_event is set.

    This function is designed to be called from a multiprocessing.Process
    target (see app/main.py audio_worker).
    """
    import sounddevice as sd
    from src.audio.models import ensure_silero_vad_model, ensure_wakeword_models

    log.info("Initialising audio pipeline…")

    # --- 1. Load models ---------------------------------------------------
    vad_path = ensure_silero_vad_model()
    vad = SileroVAD(str(vad_path))
    log.info("Silero-VAD loaded from %s", vad_path)

    # openwakeword — import and initialise
    from openwakeword.model import Model as OWWModel
    oww = OWWModel(inference_framework="onnx")
    available_models = list(oww.models.keys())
    log.info("OpenWakeWord loaded — models: %s", available_models)

    # --- 2. Build pipeline ------------------------------------------------
    pipeline = AudioPipeline(
        event_queue=event_queue,
        vad=vad,
        oww_model=oww,
    )

    # --- 3. Audio callback ------------------------------------------------
    # sounddevice's callback fires on a dedicated PortAudio thread.
    # We push raw frames into a thread-safe stdlib queue; the main loop
    # pulls from it.  This avoids any heavy work in the callback.
    _audio_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)

    def _audio_callback(
        indata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        if status:
            log.warning("sounddevice status: %s", status)
        # indata shape: (frames, 1) — squeeze to 1-D
        _audio_q.put_nowait(indata[:, 0].copy())

    # --- 4. Open stream and run -------------------------------------------
    # blocksize=0 lets PortAudio choose the optimal size; we'll re-chunk
    # in the main loop to match OWW (1280) and VAD (512) expectations.
    log.info(
        "Opening audio stream: %d Hz, %d ch, dtype=%s",
        SAMPLE_RATE, CHANNELS, DTYPE.__name__,
    )

    _leftover = np.array([], dtype=DTYPE)

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=OWW_CHUNK_SAMPLES,  # 80 ms blocks
            callback=_audio_callback,
        ):
            log.info("✅ Audio stream opened — pipeline running.")

            while not shutdown_event.is_set():
                # Pull frames from the callback queue
                try:
                    raw = _audio_q.get(timeout=0.05)
                except queue.Empty:
                    continue

                # Prepend any leftover samples from previous iteration
                if len(_leftover) > 0:
                    raw = np.concatenate([_leftover, raw])
                    _leftover = np.array([], dtype=DTYPE)

                if pipeline.state == PipelineState.WAITING_FOR_WAKE:
                    # Feed in OWW_CHUNK_SAMPLES (1280) blocks
                    offset = 0
                    while offset + OWW_CHUNK_SAMPLES <= len(raw):
                        chunk = raw[offset : offset + OWW_CHUNK_SAMPLES]
                        if pipeline.feed_oww(chunk):
                            pipeline.transition_to_listening()
                            # Process remaining audio as VAD input
                            remaining = raw[offset + OWW_CHUNK_SAMPLES :]
                            if len(remaining) > 0:
                                _leftover = remaining
                            break
                        offset += OWW_CHUNK_SAMPLES
                    else:
                        # Save unused tail for next iteration
                        tail = raw[offset:]
                        if len(tail) > 0:
                            _leftover = tail

                elif pipeline.state == PipelineState.LISTENING:
                    # Feed in VAD_CHUNK_SAMPLES (512) blocks
                    offset = 0
                    while offset + VAD_CHUNK_SAMPLES <= len(raw):
                        chunk = raw[offset : offset + VAD_CHUNK_SAMPLES]
                        pipeline.feed_vad(chunk)

                        # Check if pipeline reset back to waiting
                        if pipeline.state == PipelineState.WAITING_FOR_WAKE:
                            remaining = raw[offset + VAD_CHUNK_SAMPLES :]
                            if len(remaining) > 0:
                                _leftover = remaining
                            break
                        offset += VAD_CHUNK_SAMPLES
                    else:
                        # Save unused tail for next iteration
                        tail = raw[offset:]
                        if len(tail) > 0:
                            _leftover = tail

    except Exception:
        log.exception("Fatal error in audio pipeline.")
        raise
    finally:
        log.info("Audio pipeline shutting down.")
