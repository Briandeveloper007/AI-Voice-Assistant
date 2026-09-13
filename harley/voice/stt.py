"""
Harley Voice Assistant - Speech to Text Engine
Transcribes 16kHz PCM audio bytes into text using Whisper ONNX DirectML.
Includes strict pre-inference silence validation and post-inference hallucination filtering.
"""

import zlib
import logging
import numpy as np
from typing import Optional

logger = logging.getLogger("HarleySTT")

class SilenceException(Exception):
    """Raised when the audio buffer contains only ambient noise."""
    pass

class HallucinationException(Exception):
    """Raised when the STT model outputs a known hallucination loop or artifact."""
    pass

class SpeechToTextEngine:
    def __init__(self, model_id: str = "openai/whisper-tiny.en", model_path: Optional[str] = None):
        """
        Initializes the Whisper model using ONNX Runtime.
        Defaults to the English-only tiny model for maximum local performance.
        """
        if model_path is not None:
            model_id = model_path

        self.sample_rate = 16000
        self.rms_threshold = 0.005  # Hardware-dependent. Tune this to your microphone.
        
        # Whisper often hallucinates these specific phrases when fed background hums
        self.hallucination_blacklist = [
            "thank you for watching",
            "subtitles by",
            "amara.org",
            "you",
            "bye"
        ]

        logger.info("[STT] Loading Whisper ONNX model with DirectML...")
        
        try:
            from transformers import AutoProcessor  # type: ignore
            from optimum.onnxruntime import ORTModelForSpeechSeq2Seq  # type: ignore

            self.processor = AutoProcessor.from_pretrained(model_id)
            
            # Enforce Windows GPU Hardware Acceleration via DirectML
            self.model = ORTModelForSpeechSeq2Seq.from_pretrained(
                model_id,
                export=True, # Auto-converts PyTorch to ONNX if not already cached
                provider="DmlExecutionProvider",
                fallback_provider="CPUExecutionProvider" # Fallback if DirectX 12 fails
            )
            
            active_providers = getattr(self.model, "providers", [])
            logger.info(f"[STT] Engine active. Providers: {active_providers}")
            
            if "DmlExecutionProvider" not in active_providers:
                logger.warning("[STT] DirectML failed to load. Falling back to CPU. Audio processing will be slower.")
                
        except Exception as e:
            logger.error(f"[STT] Failed to initialize ONNX engine: {e}")
            raise

    def _check_rms_volume(self, audio_array: np.ndarray) -> bool:
        """Calculates Root Mean Square volume to detect absolute silence."""
        rms = np.sqrt(np.mean(audio_array**2))
        logger.debug(f"[STT] Buffer RMS Volume: {rms:.5f}")
        return rms >= self.rms_threshold

    def _is_hallucination(self, text: str) -> bool:
        """
        Validates the transcript against known Whisper artifacts and 
        infinite-repetition loops using zlib compression ratios.
        """
        clean_text = text.lower().strip()
        
        # 1. Exact Match Blacklist
        for phrase in self.hallucination_blacklist:
            if clean_text == phrase:
                logger.warning(f"[STT] Blacklist match blocked: '{clean_text}'")
                return True
                
        # 2. Compression Ratio Check (catches "hello hello hello hello hello")
        # Real human speech rarely exceeds a 1.5 - 2.0 compression ratio.
        if len(clean_text) > 25:
            compressed = zlib.compress(clean_text.encode('utf-8'))
            compression_ratio = len(clean_text) / len(compressed)
            
            if compression_ratio > 2.4:
                logger.warning(f"[STT] High compression ratio ({compression_ratio:.2f}). Flagged as repetitive hallucination.")
                return True
                
        return False

    def transcribe(self, audio_bytes: bytes) -> Optional[str]:
        """
        Main entry point. Takes raw 16kHz 16-bit PCM bytes, validates energy, 
        runs inference, and filters the output.
        """
        # 1. Byte Conversion
        # Convert raw PCM bytes to float32 numpy array normalized between -1.0 and 1.0
        audio_array = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        # 2. Ambient Silence Gate
        if not self._check_rms_volume(audio_array):
            raise SilenceException("RMS volume below threshold. Discarding audio.")

        # 3. Feature Extraction & Inference
        try:
            input_features = self.processor(
                audio_array, 
                sampling_rate=self.sample_rate, 
                return_tensors="pt"
            ).input_features

            predicted_ids = self.model.generate(input_features)
            
            transcript = self.processor.batch_decode(
                predicted_ids, 
                skip_special_tokens=True
            )[0].strip()
            
        except Exception as e:
            logger.error(f"[STT] ONNX Inference failed: {e}", exc_info=True)
            raise

        # 4. Hallucination Gate
        if not transcript:
            raise SilenceException("Transcript returned empty.")
            
        if self._is_hallucination(transcript):
            raise HallucinationException(f"Filtered hallucinated string: '{transcript}'")

        logger.info(f"[STT] Final Transcript: '{transcript}'")
        return transcript
