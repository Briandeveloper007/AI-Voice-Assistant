import json
import struct
import sqlite3
import pytest
import asyncio
import multiprocessing as mp
import sys
from unittest.mock import MagicMock

# Mock out the ML-heavy modules entirely to avoid torch/scipy import conflicts
class DummyException(Exception): pass
stt_mock = MagicMock()
stt_mock.SilenceException = DummyException
stt_mock.HallucinationException = DummyException
stt_mock.SpeechToTextEngine = MagicMock

sys.modules["voice"] = MagicMock()
sys.modules["voice.tts"] = MagicMock()
sys.modules["voice.stt"] = stt_mock
sys.modules["voice.audio_capture"] = MagicMock()

# Import the main Orchestrator class from your application
from app.main import HarleyOrchestrator

@pytest.fixture
def mock_db_conn():
    """Provides a temporary in-memory SQLite database matching the production schema."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, start_time DATETIME)")
    conn.execute(
        "CREATE TABLE utterances (id INTEGER PRIMARY KEY, session_id TEXT, "
        "user_text TEXT, intent_json TEXT, response_text TEXT)"
    )
    conn.commit()
    yield conn
    conn.close()

@pytest.mark.asyncio
async def test_chrome_ipc_to_context_manager_pipeline(mock_db_conn):
    """
    Verifies that a JSON payload sent over the localhost TCP socket successfully 
    traverses the IPC server -> multiprocessing event_queue -> Orchestrator loop -> ContextManager.
    """
    # 1. Setup Orchestrator IPC Dependencies
    shutdown_event = mp.Event()
    interrupt_playback = mp.Event()
    tts_active = mp.Event()
    event_queue = mp.Queue()
    tts_queue = mp.Queue()

    # 2. Initialize the Orchestrator
    orchestrator = HarleyOrchestrator(
        db_conn=mock_db_conn,
        shutdown_event=shutdown_event,
        interrupt_playback_event=interrupt_playback,
        tts_active_event=tts_active,
        event_queue=event_queue,
        tts_queue=tts_queue
    )
    
    # Mock heavy ML engines so the test runs instantly without loading ONNX weights
    orchestrator.stt_engine = MagicMock()
    # Assume AIProvider is also mocked to avoid API calls if we were testing further down
    orchestrator.ai_provider = MagicMock()

    # 3. Start the IPC Server and the Event Consumer Loop asynchronously
    await orchestrator.start_ipc_server()
    consumer_task = asyncio.create_task(orchestrator.event_consumer_loop())

    try:
        # 4. Simulate browser_host.py sending an active DOM capture
        test_content = "This is sanitized DOM text extracted from the Chrome tab."
        payload = {
            "type": "BROWSER_PAGE_CAPTURED",
            "content": test_content
        }
        
        # Connect to the orchestrator's loopback socket (mimicking browser_host.py)
        reader, writer = await asyncio.open_connection("127.0.0.1", 48123)
        
        # Frame the message exactly as required (4-byte length prefix + JSON bytes)
        encoded_data = json.dumps(payload).encode("utf-8")
        writer.write(struct.pack("!I", len(encoded_data)) + encoded_data)
        await writer.drain()
        
        # Close the client socket cleanly
        writer.close()
        await writer.wait_closed()

        # 5. Yield control briefly to allow the orchestrator loop to process the event_queue
        await asyncio.sleep(0.3)

        # 6. Assertions
        web_context = orchestrator.context_manager.get_web_context()
        
        assert test_content in web_context, "The DOM text was not found in the ContextManager."
        assert "<untrusted_web_content>" in web_context, "The web context was not properly delimited for prompt safety."
        
    finally:
        # 7. Clean Teardown
        shutdown_event.set()
        orchestrator.stop()
        
        if orchestrator.ipc_server:
            orchestrator.ipc_server.close()
            await orchestrator.ipc_server.wait_closed()
            
        consumer_task.cancel()
