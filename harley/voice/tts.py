"""
voice/tts.py — TTS Output Worker (Process 3)
=============================================
Manages audio synthesis, volume ducking, and chunked playback.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import tempfile
import wave
from multiprocessing.synchronize import Event as EventClass
from typing import Any

import pyaudio
import pyttsx3
from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume

log = logging.getLogger("harley.tts")


def set_system_volume_ducking(duck: bool) -> None:
    """
    Lowers the volume of all non-Python audio sessions by 60% if duck=True,
    or restores them to 1.0 if duck=False.
    """
    try:
        sessions = AudioUtilities.GetAllSessions()
        current_pid = os.getpid()
        for session in sessions:
            if session.Process and session.Process.pid != current_pid:
                volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                if duck:
                    volume.SetMasterVolume(0.4, None)
                else:
                    volume.SetMasterVolume(1.0, None)
    except Exception as exc:
        log.warning("Failed to adjust system ducking: %s", exc)


def synthesize_text_to_pcm(text: str) -> tuple[bytes, int, int]:
    """
    Uses pyttsx3 to synthesize text to a temporary WAV file, reads it,
    and returns the raw PCM bytes, channels, and frame rate.
    """
    engine = pyttsx3.init()
    
    fd, temp_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    
    try:
        engine.save_to_file(text, temp_path)
        engine.runAndWait()
        
        with wave.open(temp_path, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            return frames, wf.getnchannels(), wf.getframerate()
    except Exception as exc:
        log.error("Failed to synthesize text: %s", exc)
        return b"", 1, 16000
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def tts_playback_worker(
    tts_queue: "queue.Queue[Any]",
    interrupt_playback_event: EventClass,
    shutdown_event: EventClass,
    event_queue: "queue.Queue[Any] | None" = None,
    tts_active_event: "EventClass | None" = None
) -> None:
    """
    Process 3 — Text-to-speech playback worker.
    """
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)-8s] %(processName)s/%(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
        ],
    )
    _log = logging.getLogger("harley.tts")
    _log.info("TTS worker started (PID %d).", os.getpid())

    p = pyaudio.PyAudio()
    
    try:
        while not shutdown_event.is_set():
            try:
                job = tts_queue.get(timeout=0.05)
            except queue.Empty:
                continue
                
            if interrupt_playback_event.is_set():
                interrupt_playback_event.clear()
                continue
                
            _log.debug("Received TTS job.")
            
            # 1. Prepare PCM data
            if isinstance(job, str):
                pcm_data, channels, rate = synthesize_text_to_pcm(job)
            elif isinstance(job, bytes):
                pcm_data = job
                channels = 1
                rate = 16000 # default fallback for raw bytes
            elif isinstance(job, tuple) and len(job) == 3:
                pcm_data, channels, rate = job
            else:
                _log.warning("Unknown job type: %s", type(job))
                continue
                
            if not pcm_data:
                continue

            # 2. Start Ducking and set active flag
            if tts_active_event:
                tts_active_event.set()
            set_system_volume_ducking(True)
            
            # 3. Open Stream
            stream = p.open(format=pyaudio.paInt16,
                            channels=channels,
                            rate=rate,
                            output=True)
            
            # 4. Play in chunks (50ms)
            # 50ms chunk size in bytes = rate * 0.05 * channels * 2 (16-bit)
            chunk_size_bytes = int(rate * 0.05) * channels * 2
            
            try:
                for i in range(0, len(pcm_data), chunk_size_bytes):
                    if shutdown_event.is_set():
                        break
                    
                    if interrupt_playback_event.is_set():
                        _log.debug("Barge-in detected mid-playback.")
                        interrupt_playback_event.clear()
                        if event_queue:
                            event_queue.put({"type": "TTS_INTERRUPTED"})
                        break
                    
                    chunk = pcm_data[i:i+chunk_size_bytes]
                    stream.write(chunk)
            finally:
                stream.stop_stream()
                stream.close()
                
                # Restore ducking and flag
                set_system_volume_ducking(False)
                if tts_active_event:
                    tts_active_event.clear()
                    
    finally:
        p.terminate()
        _log.info("TTS worker stopped.")
