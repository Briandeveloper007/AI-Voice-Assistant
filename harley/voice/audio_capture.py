"""
Harley Voice Assistant - Audio Capture Worker (Process 1)
Handles real-time microphone streaming, wake-word detection, and VAD segmentation.
"""

import time
import queue
import logging
import collections
import numpy as np
import multiprocessing as mp

# Required Audio & ML Dependencies
import sounddevice as sd
import torch  # type: ignore
from openwakeword.model import Model
import sys
from pathlib import Path

logger = logging.getLogger("HarleyAudioProcess")

# --- Audio Configuration ---
SAMPLE_RATE = 16000
CHUNK_SIZE = 1280  # 80ms chunks for openWakeWord processing
RING_BUFFER_SECONDS = 1.5
SILENCE_THRESHOLD_MS = 700
SILENCE_CHUNKS_LIMIT = int((SILENCE_THRESHOLD_MS / 1000) * SAMPLE_RATE / CHUNK_SIZE)

def audio_capture_worker(
    shutdown_event: mp.synchronize.Event,  # type: ignore
    interrupt_playback_event: mp.synchronize.Event,  # type: ignore
    tts_active_event: mp.synchronize.Event,  # type: ignore
    event_queue: mp.Queue,
):
    """
    PROCESS 1: High-Priority Real-Time Audio Capture & Wake-Word Detection.
    Runs entirely outside the Python GIL using multiprocessing.
    """
    logger.info("[AudioWorker] Initializing Audio Pipeline...")

    # 1. Initialize openWakeWord Models
    base_path = Path(getattr(sys, "_MEIPASS", Path.cwd()))
    custom_model_path = str(base_path / "models" / "hey_harley.onnx")
    
    try:
        oww_model = Model(wakeword_models=[custom_model_path, "timer"], inference_framework="onnx")
    except Exception as e:
        logger.error(f"[AudioWorker] Failed to load openWakeWord models: {e}")
        return
    
    # 2. Initialize Silero VAD via PyTorch Hub
    logger.info("[AudioWorker] Loading Silero VAD...")
    try:
        vad_model, utils = torch.hub.load(
            repo_or_dir='snakers4/silero-vad', 
            model='silero_vad', 
            force_reload=False,
            trust_repo=True
        )
    except Exception as e:
        logger.error(f"[AudioWorker] Failed to load Silero VAD: {e}")
        return
        
    # 3. State Management
    max_ring_chunks = int((RING_BUFFER_SECONDS * SAMPLE_RATE) / CHUNK_SIZE)
    ring_buffer = collections.deque(maxlen=max_ring_chunks)
    
    audio_queue = queue.Queue()
    state = "WAKE_WORD"
    utterance_buffer = bytearray()
    silence_counter = 0

    def audio_callback(indata, frames, time_info, status):
        """Called by sounddevice for each hardware audio block."""
        if status:
            logger.warning(f"[AudioWorker] Audio stream status: {status}")
        
        # sounddevice returns a 2D array; we slice the first channel for mono
        audio_data = indata[:, 0].copy()
        audio_queue.put(audio_data)

    logger.info("[AudioWorker] Starting microphone stream...")
    
    try:
        # Open the microphone at strictly 16 kHz, 16-bit mono
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=CHUNK_SIZE,
            callback=audio_callback
        ):
            logger.info("[AudioWorker] Listening...")
            
            while not shutdown_event.is_set():
                try:
                    chunk = audio_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                # 1. Feed continuous ring buffer
                ring_buffer.append(chunk)

                # 2. State Polling: Check if TTS is talking to toggle Barge-in mode
                if tts_active_event.is_set():
                    state = "BARGE_IN_CHECK"
                elif state == "BARGE_IN_CHECK" and not tts_active_event.is_set():
                    # TTS finished naturally, resume normal listening
                    state = "WAKE_WORD"
                    utterance_buffer.clear()

                # 3. Execution based on State Machine
                if state == "WAKE_WORD":
                    # Evaluate the chunk for the main wake word
                    predictions = oww_model.predict(chunk)
                    main_wake_score = predictions.get("hey_harley", 0.0)  # type: ignore
                    
                    if main_wake_score > 0.5:
                        logger.info("[AudioWorker] 🟢 Wake Word Detected!")
                        event_queue.put({"type": "WAKE_WORD_DETECTED"})
                        
                        # Pre-fill the utterance with the 1.5-second pre-wake ring buffer
                        utterance_buffer = bytearray()
                        for b in ring_buffer:
                            utterance_buffer.extend(b.tobytes())
                            
                        state = "RECORDING"
                        silence_counter = 0
                        vad_model.reset_states()

                elif state == "RECORDING":
                    utterance_buffer.extend(chunk.tobytes())
                    
                    # Silero VAD requires float32 tensors normalized between -1.0 and 1.0
                    tensor_chunk = torch.from_numpy(chunk.astype(np.float32) / 32768.0)
                    speech_prob = vad_model(tensor_chunk, SAMPLE_RATE).item()
                    
                    if speech_prob < 0.3:
                        silence_counter += 1
                    else:
                        silence_counter = 0

                    if silence_counter >= SILENCE_CHUNKS_LIMIT:
                        logger.info("[AudioWorker] 🔴 700ms Silence boundary detected. Dispatching audio packet.")
                        
                        # Send the fully segmented raw audio bytes back to the Orchestrator
                        event_queue.put({
                            "type": "AUDIO_COMMAND_CAPTURED",
                            "data": bytes(utterance_buffer)
                        })
                        
                        utterance_buffer.clear()
                        state = "WAKE_WORD"

                elif state == "BARGE_IN_CHECK":
                    # While the AI is speaking, we ONLY listen for the cancellation wake word
                    predictions = oww_model.predict(chunk)
                    stop_score = predictions.get("timer", 0.0)  # type: ignore  # Use "timer" or "stop" model
                    
                    if stop_score > 0.6:
                        logger.warning("[AudioWorker] 🛑 Barge-in Detected! Setting hardware interrupt flag.")
                        interrupt_playback_event.set()
                        event_queue.put({"type": "BARGE_IN_TRIGGERED"})
                        
                        # Debounce to prevent multiple fires
                        time.sleep(1.0)
                        
    except Exception as e:
        logger.error(f"[AudioWorker] Fatal error in audio pipeline: {e}", exc_info=True)
    finally:
        logger.info("[AudioWorker] Terminated cleanly.")
