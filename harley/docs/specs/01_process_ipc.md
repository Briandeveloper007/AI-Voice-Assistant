# Process & IPC Topology
1. Process 1 (Audio): Runs openWakeWord and silero-vad. Must maintain a 1.5s continuous circular ring buffer. Once silero-vad detects 700ms of silence, push the audio bytes to the Orchestrator via event_queue.
2. Process 2 (Orchestrator): The asyncio main loop. Reads event_queue, processes AI logic, and pushes to tts_queue.
3. Process 3 (TTS): Synthesizes audio in 50ms chunks. Checks the `interrupt_playback_event` OS atomic flag every frame for instant barge-in cancellation.
