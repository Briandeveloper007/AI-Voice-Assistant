"""
app/main.py — Harley Voice Assistant Entry Point
=================================================
Architectural invariants enforced here (see CLAUDE.md):
  1. Windows Named Mutex  → single-instance guard
  2. Windows Job Object   → JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE on all children
  3. SQLite WAL mode      → PRAGMA journal_mode=WAL + 5 000 ms busy timeout
  4. 3-Process IPC        → Audio (P1) ↔ event_queue ↔ Orchestrator (P2)
                                         ↔ tts_queue   ↔ TTS (P3)
  5. asyncio              → Orchestrator loop runs inside P2
  6. multiprocessing.freeze_support() → required for PyInstaller frozen builds

NEVER run this process as Administrator (Medium Integrity only).
"""

from __future__ import annotations

import argparse
import asyncio
from dotenv import load_dotenv

load_dotenv()
import json
import logging
import multiprocessing
import multiprocessing.queues
import signal
import sqlite3
import sys
import time
import uuid
import struct
from multiprocessing import Process, Queue
from multiprocessing.synchronize import Event
from pathlib import Path
from typing import Final, Any

import ctypes
import ctypes.wintypes

from voice.tts import tts_playback_worker
from voice.stt import SpeechToTextEngine, SilenceException, HallucinationException
from voice.audio_capture import audio_capture_worker
from intelligence.context import ContextManager
from intelligence.ai_provider import AIProvider, plan_action
from security.permissions import PermissionGatekeeper, GatekeeperVerdict
from commands.schema import StructuredIntent, PermissionLevel
from app.lifecycle import LifecycleWatchdog
from ui.system_tray import SystemTrayManager
from executors.windows import WindowsExecutor
from executors.file_system import FileSystemExecutor
from integrations.spotify import SpotifyAdapter

# ---------------------------------------------------------------------------
# Logging — must be configured before any other import that might emit logs.
# NOTE: Never use print() — safe even in the Native Messaging host context.
# ---------------------------------------------------------------------------
LOG_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(processName)s/%(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),          # stderr only (never stdout)
        logging.FileHandler(LOG_DIR / "harley.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("harley.main")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MUTEX_NAME: Final[str]  = "Local\\HarleyVoiceAssistant_SingleInstance"
DB_PATH:    Final[Path] = Path(__file__).resolve().parent.parent / "data" / "harley.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# IPC queue high-water marks (items, not bytes)
AUDIO_QUEUE_MAXSIZE: Final[int] = 32   # ~32 × audio chunk bursts
TTS_QUEUE_MAXSIZE:   Final[int] = 16   # text strings queued for synthesis

# ---------------------------------------------------------------------------
# Windows API bindings (ctypes)
# ---------------------------------------------------------------------------
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# CreateMutexW
_kernel32.CreateMutexW.restype  = ctypes.wintypes.HANDLE
_kernel32.CreateMutexW.argtypes = [
    ctypes.wintypes.LPVOID,   # lpMutexAttributes
    ctypes.wintypes.BOOL,     # bInitialOwner
    ctypes.wintypes.LPCWSTR,  # lpName
]

# CreateJobObjectW
_kernel32.CreateJobObjectW.restype  = ctypes.wintypes.HANDLE
_kernel32.CreateJobObjectW.argtypes = [
    ctypes.wintypes.LPVOID,   # lpJobAttributes
    ctypes.wintypes.LPCWSTR,  # lpName
]

# AssignProcessToJobObject
_kernel32.AssignProcessToJobObject.restype  = ctypes.wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = [
    ctypes.wintypes.HANDLE,   # hJob
    ctypes.wintypes.HANDLE,   # hProcess
]

# SetInformationJobObject
_kernel32.SetInformationJobObject.restype  = ctypes.wintypes.BOOL
_kernel32.SetInformationJobObject.argtypes = [
    ctypes.wintypes.HANDLE,   # hJob
    ctypes.c_int,             # JobObjectInformationClass
    ctypes.wintypes.LPVOID,   # lpJobObjectInformation
    ctypes.wintypes.DWORD,    # cbJobObjectInformationLength
]

# OpenProcess
_kernel32.OpenProcess.restype  = ctypes.wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [
    ctypes.wintypes.DWORD,    # dwDesiredAccess
    ctypes.wintypes.BOOL,     # bInheritHandle
    ctypes.wintypes.DWORD,    # dwProcessId
]

_kernel32.CloseHandle.restype  = ctypes.wintypes.BOOL
_kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]

# Job Object info class + limit flags
_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
PROCESS_ALL_ACCESS = 0x1F0FFF
ERROR_ALREADY_EXISTS = 183


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit",     ctypes.c_int64),
        ("LimitFlags",              ctypes.wintypes.DWORD),
        ("MinimumWorkingSetSize",   ctypes.c_size_t),
        ("MaximumWorkingSetSize",   ctypes.c_size_t),
        ("ActiveProcessLimit",      ctypes.wintypes.DWORD),
        ("Affinity",                ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass",           ctypes.wintypes.DWORD),
        ("SchedulingClass",         ctypes.wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount",  ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount",   ctypes.c_uint64),
        ("WriteTransferCount",  ctypes.c_uint64),
        ("OtherTransferCount",  ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo",                _IO_COUNTERS),
        ("ProcessMemoryLimit",    ctypes.c_size_t),
        ("JobMemoryLimit",        ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed",     ctypes.c_size_t),
    ]


# ---------------------------------------------------------------------------
# 1. Single-Instance Guard — Windows Named Mutex
# ---------------------------------------------------------------------------
def acquire_single_instance_mutex() -> ctypes.wintypes.HANDLE:
    """
    Create a named mutex scoped to the Local\ namespace (medium integrity).
    If another Harley instance already holds the mutex, exit immediately.

    Returns the HANDLE so the caller can keep it alive for the process lifetime.
    """
    handle = _kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if not handle:
        log.critical("CreateMutexW failed — cannot start Harley.")
        sys.exit(1)

    last_err = ctypes.get_last_error()
    if last_err == ERROR_ALREADY_EXISTS:
        log.warning("Harley is already running (mutex exists). Exiting.")
        _kernel32.CloseHandle(handle)
        sys.exit(0)

    log.info("Single-instance mutex acquired: %s", MUTEX_NAME)
    return handle


# ---------------------------------------------------------------------------
# 2. Windows Job Object — KILL_ON_JOB_CLOSE
# ---------------------------------------------------------------------------
def create_job_object() -> ctypes.wintypes.HANDLE:
    """
    Create a Windows Job Object configured so that every child process assigned
    to it is killed when the Job handle is closed (i.e. when the main process
    exits for any reason, including crashes).

    CLAUDE.md invariant: JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
    """
    job = _kernel32.CreateJobObjectW(None, "Local\\HarleyJobObject")
    if not job:
        log.critical("CreateJobObjectW failed — errno %d", ctypes.get_last_error())
        sys.exit(1)

    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

    ok = _kernel32.SetInformationJobObject(
        job,
        _JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        log.critical(
            "SetInformationJobObject failed — errno %d", ctypes.get_last_error()
        )
        _kernel32.CloseHandle(job)
        sys.exit(1)

    log.info("Job Object created with KILL_ON_JOB_CLOSE.")
    return job


def assign_process_to_job(job: ctypes.wintypes.HANDLE, pid: int) -> None:
    """Open a process handle for *pid* and assign it to *job*."""
    proc_handle = _kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not proc_handle:
        log.error(
            "OpenProcess failed for pid=%d — errno %d", pid, ctypes.get_last_error()
        )
        return

    ok = _kernel32.AssignProcessToJobObject(job, proc_handle)
    _kernel32.CloseHandle(proc_handle)

    if ok:
        log.info("PID %d assigned to Job Object.", pid)
    else:
        log.error(
            "AssignProcessToJobObject failed for pid=%d — errno %d",
            pid,
            ctypes.get_last_error(),
        )


# ---------------------------------------------------------------------------
# 3. SQLite Initialization — WAL mode + 5 000 ms busy timeout
# ---------------------------------------------------------------------------
def init_database(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """
    Initialise the SQLite database with:
      - PRAGMA journal_mode = WAL   (multi-reader, single-writer — CLAUDE.md)
      - PRAGMA busy_timeout = 5000  (5 000 ms before raising OperationalError)
      - PRAGMA synchronous = NORMAL
      - PRAGMA foreign_keys = ON
      - Core schema tables (sessions, utterances, permissions)
    """
    log.info("Initialising database at: %s", db_path)
    con = sqlite3.connect(str(db_path), check_same_thread=False)
    cursor = con.cursor()

    cursor.execute("PRAGMA journal_mode = WAL;")
    cursor.execute("PRAGMA busy_timeout = 5000;")
    cursor.execute("PRAGMA synchronous = NORMAL;")
    cursor.execute("PRAGMA foreign_keys = ON;")

    # 1. Sessions Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            start_time DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 2. Utterances Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS utterances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_text TEXT,
            intent_json TEXT,
            response_text TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        );
    """)

    # 3. Persistent permission overrides set by the user
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS permissions (
            action      TEXT PRIMARY KEY,
            level       TEXT NOT NULL CHECK(level IN ('ALLOW','CONFIRM','DENY','BLOCK')),
            updated_at  REAL NOT NULL DEFAULT (unixepoch('now', 'subsec'))
        );
    """)

    con.commit()
    log.info("SQLite initialized in WAL mode at: %s", db_path)
    return con


# ---------------------------------------------------------------------------
# 4. Worker stubs
# ---------------------------------------------------------------------------

# --- Process 1: Audio Worker -----------------------------------------------
def audio_worker(
    event_queue: "Queue[bytes]",
    shutdown_event: Event,
) -> None:
    """
    Process 1 — Audio capture.

    Responsibilities (from docs/specs/01_process_ipc.md):
      • Run openWakeWord for hot-word detection.
      • Run silero-vad on a 1.5 s circular ring buffer.
      • On 700 ms of detected silence after speech, push raw PCM bytes
        to *event_queue* for the Orchestrator.

    This stub sets up logging and loops until *shutdown_event* is set.
    """
    # Re-configure logging in child — handlers are not inherited on Windows.
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)-8s] %(processName)s/%(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(str(LOG_DIR / "harley.log"), encoding="utf-8"),
        ],
    )
    _log = logging.getLogger("harley.audio")
    _log.info("Audio worker started (PID %d).", multiprocessing.current_process().pid)

    try:
        # TODO: initialise sounddevice input stream, openWakeWord model,
        #       silero-vad model, and 1.5 s ring buffer here.
        while not shutdown_event.is_set():
            # TODO: read audio frames, run VAD, detect silence boundary,
            #       slice ring buffer, push PCM to event_queue.
            time.sleep(0.02)   # 20 ms polling cadence placeholder
    except Exception:
        _log.exception("Unhandled exception in audio worker.")
    finally:
        _log.info("Audio worker exiting.")


# --- Process 3: TTS Worker -------------------------------------------------
# (Implementation moved to voice.tts.tts_playback_worker)


# ---------------------------------------------------------------------------
# 5. Process 2: Orchestrator — HarleyOrchestrator & asyncio main loop
# ---------------------------------------------------------------------------
class HarleyOrchestrator:
    """
    Core Orchestrator class managing event consumer loops and dispatch routing.
    """
    def __init__(
        self,
        db_conn: sqlite3.Connection,
        shutdown_event: Event,
        interrupt_playback_event: Event,
        tts_active_event: Event,
        event_queue: Queue,
        tts_queue: Queue,
    ):
        self.db = db_conn
        self.shutdown_event = shutdown_event
        self.interrupt_playback_event = interrupt_playback_event
        self.tts_active_event = tts_active_event
        self.event_queue = event_queue
        self.tts_queue = tts_queue
        self.running = True
        self.windows_executor = WindowsExecutor()
        self.fs_executor = FileSystemExecutor()
        self.spotify_adapter = SpotifyAdapter()

        # Generate a unique session ID for this boot cycle
        self.session_id = str(uuid.uuid4())
        
        # Log the new session to the database synchronously on startup
        self.db.execute("INSERT INTO sessions (session_id) VALUES (?)", (self.session_id,))
        self.db.commit()
        log.info("[Orchestrator] Session initialized: %s", self.session_id)

        # Initialize the STT Engine (Loads ONNX weights into Memory/GPU)
        log.info("[Orchestrator] Booting Speech-to-Text engine...")
        try:
            self.stt_engine = SpeechToTextEngine()
        except Exception as e:
            log.warning("[Orchestrator] Could not init STT Engine (ignore if testing/model missing): %s", e)
            self.stt_engine = None

        # Initialize the AI Provider
        log.info("[Orchestrator] Booting AI Provider...")
        self.ai_provider = AIProvider()

        # Initialize the Context Manager
        self.context_manager = ContextManager(max_turns=5, expiry_minutes=5)
        self.ipc_server = None

    async def log_turn(self, user_text: str, intent: Any, response_text: str):
        """
        Asynchronously writes the conversation turn to SQLite.
        Runs in a background thread to prevent blocking the asyncio event loop.
        """
        intent_json = intent.model_dump_json() if intent else "{}"

        def _write_to_db():
            self.db.execute(
                """
                INSERT INTO utterances 
                (session_id, user_text, intent_json, response_text) 
                VALUES (?, ?, ?, ?)
                """,
                (self.session_id, user_text, intent_json, response_text)
            )
            self.db.commit()
            
        await asyncio.to_thread(_write_to_db)


    async def event_consumer_loop(self):
        """Polls the IPC event_queue asynchronously without blocking the loop."""
        log.info("[Orchestrator] Event dispatcher loop started.")
        while self.running and not self.shutdown_event.is_set():
            try:
                event = await asyncio.to_thread(self.event_queue.get, timeout=0.1)
                await self.handle_event(event)
            except Exception:
                await asyncio.sleep(0.01)

    async def handle_event(self, event: dict):
        """Routes structured events received from background processes."""
        event_type = event.get("type")
        log.info("[Orchestrator] Inbound Event: %s", event_type)

        if event_type == "WAKE_WORD_DETECTED":
            self.tts_queue.put({"task_id": "wake-ack", "text": "Yes? I'm listening."})
        elif event_type == "BARGE_IN_TRIGGERED":
            log.info("[Orchestrator] User interrupted assistant. Canceling active plan.")
            self.interrupt_playback_event.set()
        elif event_type == "AUDIO_COMMAND_CAPTURED":
            audio_bytes = event.get("data")
            if not audio_bytes or self.stt_engine is None:
                return

            try:
                # 1. Transcribe the audio
                log.info("[Orchestrator] Audio packet received. Transcribing...")
                transcript = await asyncio.to_thread(self.stt_engine.transcribe, audio_bytes)
                if not transcript:
                    return
                log.info("[Orchestrator] User said: '%s'", transcript)

                # 2. Retrieve short-term memory (returns "" if empty or expired)
                turn_summary = self.context_manager.get_context_summary()
                web_context = self.context_manager.get_web_context()

                # Combine conversation history and active browser content
                combined_context = f"{turn_summary}\n\n{web_context}".strip()

                # 3. Route transcript to the AI Gatekeeper, injecting memory
                intent = await self.ai_provider.plan_action(transcript, context_summary=combined_context)

                # 4. Place the parsed command back onto the IPC queue to trigger the Dispatcher
                self.event_queue.put({
                    "type": "COMMAND_PARSED", 
                    "transcript": transcript, 
                    "intent": intent
                })

            except SilenceException:
                log.debug("[Orchestrator] Audio packet discarded (Ambient Silence).")
            except HallucinationException as e:
                log.warning("[Orchestrator] Audio packet discarded (Hallucination): %s", e)
            except Exception as e:
                log.error("[Orchestrator] STT processing failed: %s", e, exc_info=True)
                self.tts_queue.put("I had trouble hearing that.")

        elif event_type == "COMMAND_PARSED":
            intent = event.get("intent")
            user_transcript = event.get("transcript", "Unknown Transcript")
            if not intent:
                log.error("[Orchestrator] COMMAND_PARSED event missing intent payload.")
                return
            log.info("[Orchestrator] Dispatching intent: %s targeting %s", intent.action, intent.application)
            await self.dispatch_intent(intent, user_transcript)

    async def dispatch_intent(self, intent: StructuredIntent, user_transcript: str = "Unknown Transcript"):
        """
        The Core Action Router.
        Maps the UPPER_SNAKE_CASE action to the correct Python Executor.
        Runs synchronous executor functions in asyncio threads to prevent GIL blocking.
        Asynchronously logs turn (user_text, intent, response_text) to database.
        """
        response_text = ""
        try:
            if intent.action in ["OPEN_APP", "CLOSE_APP", "SET_VOLUME"]:
                log.info("[Router] Routing to WindowsExecutor -> %s", intent.action)
                if intent.action == "SET_VOLUME":
                    key = intent.parameters.get("key", "vol_up")
                    await asyncio.to_thread(self.windows_executor.press_media_key, key)
                elif intent.action == "OPEN_APP":
                    page = intent.parameters.get("page", "ms-settings:")
                    await asyncio.to_thread(self.windows_executor.open_settings, page)
                response_text = f"Executed Windows action: {intent.action}"

            elif intent.action in ["READ_FILE", "WRITE_FILE", "DELETE_FILE"]:
                log.info("[Router] Routing to FileSystemExecutor -> %s", intent.action)
                target_path = intent.parameters.get("path", "")
                if intent.action == "DELETE_FILE" and target_path:
                    await asyncio.to_thread(self.fs_executor.delete_file, target_path)
                elif intent.action == "READ_FILE" and target_path:
                    await asyncio.to_thread(self.fs_executor.open_file, target_path)
                response_text = f"Executed File action: {intent.action}"

            elif intent.action in ["PLAY_MUSIC", "PAUSE_MUSIC"]:
                log.info("[Router] Routing to SpotifyAdapter -> %s", intent.action)
                if intent.action == "PLAY_MUSIC":
                    await asyncio.to_thread(self.spotify_adapter.play, intent.parameters.get("query", ""))
                elif intent.action == "PAUSE_MUSIC":
                    await asyncio.to_thread(self.spotify_adapter.pause)
                response_text = f"Executed Spotify action: {intent.action}"

            else:
                log.info("[Router] No explicit executor mapped for %s. Falling back to AI response.", intent.action)
                response_text = f"I understand the request for {intent.action}, but no direct executor is mapped yet."

            # Queue response audio for synthesis
            self.tts_queue.put(response_text)

            # Update short-term memory for the LLM
            self.context_manager.add_turn(
                transcript=user_transcript,
                intent=intent,
                response=response_text
            )

            # Fire-and-forget the database turn logging
            await self.log_turn(
                user_text=user_transcript,
                intent=intent,
                response_text=response_text
            )

        except Exception as e:
            log.error("[Router] Execution failed for %s: %s", intent.action, e, exc_info=True)
            response_text = "There was a problem executing that command."
            self.tts_queue.put(response_text)

            # Update short-term memory on failure
            self.context_manager.add_turn(
                transcript=user_transcript,
                intent=intent,
                response=response_text
            )

            # Log the failure
            await self.log_turn(
                user_text=user_transcript,
                intent=intent,
                response_text=response_text
            )

    async def run(self):
        """Main orchestrator lifecycle."""
        consumer_task = asyncio.create_task(self.event_consumer_loop())
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass

    def stop(self):
        self.running = False


async def orchestrator_loop(
    event_queue: "Queue",
    tts_queue: "Queue[str]",
    interrupt_playback_event: Event,
    shutdown_event: Event,
    is_cli: bool = False,
    is_dry_run: bool = False,
) -> None:
    """
    Process 2 — Orchestrator (this process, asyncio event loop).
    """
    log.info("Orchestrator loop running. CLI=%s, DRY_RUN=%s", is_cli, is_dry_run)

    loop = asyncio.get_running_loop()
    gatekeeper = PermissionGatekeeper()
    context_manager = ContextManager()
    
    stt_engine = None
    if not is_cli:
        # Dummy path, will fail if actually instantiating without model
        try:
            stt_engine = SpeechToTextEngine(model_path="models/whisper_onnx")
        except Exception as e:
            log.warning("Could not init STT Engine (ignore if testing): %s", e)

    windows_executor = WindowsExecutor()
    fs_executor = FileSystemExecutor()
    spotify_adapter = SpotifyAdapter()

    def _get_audio_or_cli() -> Any:
        try:
            if is_cli:
                print("\n[Harley CLI] > ", end="", flush=True)
                return sys.stdin.readline().strip()
            else:
                return event_queue.get_nowait()
        except Exception:
            return None

    while not shutdown_event.is_set():
        if is_cli:
            raw_input = await loop.run_in_executor(None, _get_audio_or_cli)
            if not raw_input:
                await asyncio.sleep(0.1)
                continue
            transcript = str(raw_input)
            if transcript.lower() in ("exit", "quit"):
                shutdown_event.set()
                break
        else:
            raw_pcm = await loop.run_in_executor(None, _get_audio_or_cli)

            if raw_pcm is None:
                await asyncio.sleep(0.02)
                continue
                
            if isinstance(raw_pcm, dict):
                log.info("Orchestrator received system event: %s", raw_pcm.get("type"))
                continue

            log.debug("Orchestrator received audio payload (%d bytes).", len(raw_pcm))

            if stt_engine is None:
                continue

            try:
                transcript = stt_engine.transcribe(raw_pcm)
                if not transcript:
                    continue
                log.info("STT: %s", transcript)
            except SilenceException:
                continue
            except Exception as e:
                log.error("STT error: %s", e)
                continue

        context_str = context_manager.get_context_summary()
        full_transcript = f"{context_str}\n\nUser: {transcript}" if context_str else transcript

        try:
            intent: StructuredIntent = await plan_action(full_transcript)
        except Exception as exc:
            log.warning("Intent validation failed (auto-DENY): %s", exc)
            continue

        verdict: GatekeeperVerdict = await gatekeeper.evaluate(
            intent, event_queue=event_queue,
        )

        execution_status = "DENIED"
        if verdict.level == PermissionLevel.ALLOW:
            if is_dry_run:
                log.info("[DRY RUN] Would execute action: %s", intent.action)
                execution_status = "SUCCESS (DRY_RUN)"
            else:
                try:
                    # Core Action Router
                    if intent.action in ("OPEN_APP", "CLOSE_APP", "SET_VOLUME"):
                        log.info("[Router] Routing to WindowsExecutor -> %s", intent.action)
                        if intent.action == "SET_VOLUME":
                            key = intent.parameters.get("key", "vol_up")
                            await asyncio.to_thread(windows_executor.press_media_key, key)
                        elif intent.action == "OPEN_APP":
                            page = intent.parameters.get("page", "ms-settings:")
                            await asyncio.to_thread(windows_executor.open_settings, page)
                        elif intent.action == "CLOSE_APP":
                            log.info("Close app requested for %s", intent.parameters.get("app"))
                            
                    elif intent.action in ("READ_FILE", "WRITE_FILE", "DELETE_FILE"):
                        log.info("[Router] Routing to FileSystemExecutor -> %s", intent.action)
                        target_path = intent.parameters.get("path", "")
                        if intent.action == "DELETE_FILE" and target_path:
                            await asyncio.to_thread(fs_executor.delete_file, target_path)
                        elif intent.action == "READ_FILE" and target_path:
                            await asyncio.to_thread(fs_executor.open_file, target_path)
                        elif intent.action == "WRITE_FILE":
                            log.info("Write file requested for %s", target_path)
                            
                    elif intent.action in ("PLAY_MUSIC", "PAUSE_MUSIC", "SKIP_MUSIC"):
                        log.info("[Router] Routing to SpotifyAdapter -> %s", intent.action)
                        if intent.action == "PLAY_MUSIC":
                            query = intent.parameters.get("query", "")
                            await asyncio.to_thread(spotify_adapter.play, query)
                        elif intent.action == "PAUSE_MUSIC":
                            await asyncio.to_thread(spotify_adapter.pause)
                    else:
                        log.info("[Router] No explicit executor mapped for %s. Falling back to AI response.", intent.action)
                    
                    execution_status = "SUCCESS"
                except Exception as e:
                    log.error("[Router] Execution failed for %s: %s", intent.action, e, exc_info=True)
                    execution_status = f"FAILED: {e}"
            
            reply_text = f"Processed intent: {intent.action}. {execution_status}"
            tts_queue.put(reply_text)
            log.info("Intent APPROVED — action=%r", intent.action)
        else:
            execution_status = "DENIED"
            log.info("Intent REJECTED — action=%r level=%s", intent.action, verdict.level.value)

        context_manager.add_turn(transcript, intent, execution_status)

    log.info("Orchestrator loop exiting.")


# ---------------------------------------------------------------------------
# 6. Graceful shutdown helpers
# ---------------------------------------------------------------------------
def _build_shutdown_handler(
    shutdown_event: Event,
    workers: list[Process],
    mutex_handle: ctypes.wintypes.HANDLE,
    job_handle: ctypes.wintypes.HANDLE,
) -> None:
    def _handler(signum: int, frame) -> None:
        sig_name = signal.Signals(signum).name
        log.info("Received %s — initiating graceful shutdown.", sig_name)
        shutdown_event.set()

        for w in workers:
            w.join(timeout=5)
            if w.is_alive():
                log.warning("Worker %s did not exit cleanly — terminating.", w.name)
                w.terminate()

        _kernel32.CloseHandle(job_handle)
        _kernel32.CloseHandle(mutex_handle)
        log.info("Harley shutdown complete.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _handler)
    signal.signal(signal.SIGTERM, _handler)


# ---------------------------------------------------------------------------
# 7. Entry Point
# ---------------------------------------------------------------------------
def main() -> None:
    multiprocessing.freeze_support()
    
    parser = argparse.ArgumentParser(description="Harley Voice Assistant")
    parser.add_argument("--cli", action="store_true", help="Enable CLI text input mode (bypasses microphone)")
    parser.add_argument("--dry-run", action="store_true", help="Mock execution step (for permission testing)")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("  Harley Voice Assistant — starting up")
    log.info("=" * 60)

    mutex_handle = acquire_single_instance_mutex()
    job_handle = create_job_object()
    init_database()

    ctx = multiprocessing.get_context("spawn")
    event_queue:             Queue = ctx.Queue(maxsize=AUDIO_QUEUE_MAXSIZE)
    tts_queue:               Queue = ctx.Queue(maxsize=TTS_QUEUE_MAXSIZE)
    interrupt_playback_event: Event = ctx.Event()
    shutdown_event:           Event = ctx.Event()
    tts_active_event:         Event = ctx.Event()
    pause_event:              Event = ctx.Event()

    # Start Tray & Lifecycle
    tray_manager = SystemTrayManager(
        pause_event=pause_event,
        interrupt_playback_event=interrupt_playback_event,
        shutdown_event=shutdown_event,
        event_queue=event_queue,
        tts_queue=tts_queue,
    )
    tray_manager.start()

    lifecycle = LifecycleWatchdog(event_queue=event_queue)
    lifecycle.start_lifecycle_watchdog()

    workers: list[Any] = []

    if not args.cli:
        audio_proc = ctx.Process(
            target=audio_capture_worker,
            args=(shutdown_event, interrupt_playback_event, tts_active_event, event_queue),
            name="HarleyAudio",
            daemon=False,
        )
        audio_proc.start()
        assert audio_proc.pid is not None
        assign_process_to_job(job_handle, audio_proc.pid)
        log.info("Process 1 (Audio) started — PID %d.", audio_proc.pid)
        workers.append(audio_proc)
    else:
        log.info("Skipping Process 1 (Audio) because --cli mode is active.")

    tts_proc = ctx.Process(
        target=tts_playback_worker,
        args=(tts_queue, interrupt_playback_event, shutdown_event, event_queue, tts_active_event),
        name="HarleyTTS",
        daemon=False,
    )
    tts_proc.start()
    assert tts_proc.pid is not None
    assign_process_to_job(job_handle, tts_proc.pid)
    log.info("Process 3 (TTS) started — PID %d.", tts_proc.pid)
    workers.append(tts_proc)

    _build_shutdown_handler(shutdown_event, workers, mutex_handle, job_handle)

    log.info("Process 2 (Orchestrator) running in main process — PID %d.", multiprocessing.current_process().pid)
    try:
        asyncio.run(
            orchestrator_loop(
                event_queue,
                tts_queue,
                interrupt_playback_event,
                shutdown_event,
                is_cli=args.cli,
                is_dry_run=args.dry_run,
            )
        )
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt caught at top level — shutting down.")
        shutdown_event.set()
    finally:
        for w in workers:
            w.join(timeout=5)
            if w.is_alive():
                w.terminate()
        _kernel32.CloseHandle(job_handle)
        _kernel32.CloseHandle(mutex_handle)
        log.info("Harley exited cleanly.")

if __name__ == "__main__":
    main()
