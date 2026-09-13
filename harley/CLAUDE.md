# HARLEY VOICE ASSISTANT - GLOBAL INVARIANTS

You are an expert Windows Python developer building a secure, multi-process desktop voice assistant.

## 1. Architectural Boundaries (NEVER VIOLATE)
- **No Direct Execution:** The AI model is untrusted. It must output a `Structured Intent` JSON. Only Python Executors run the action.
- **Medium Integrity ONLY:** Never run or suggest running Harley as Administrator.
- **Process Isolation:** Audio capture (Process 1) and TTS playback (Process 3) MUST run in dedicated `multiprocessing.Process` workers to bypass the Python GIL.
- **Job Object Enforcement:** All child processes must be wrapped in a Windows Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
- **Database Concurrency:** SQLite must strictly use `PRAGMA journal_mode = WAL` and a 5000ms busy timeout.

## 2. Coding Standards
- Use `asyncio` for the main Orchestrator loop (Process 2).
- Use `multiprocessing.Queue` and `multiprocessing.Event` for all Inter-Process Communication (IPC).
- Never use `print()` for debugging inside the Chrome Native Messaging host (it corrupts the `stdio` pipe). Use `sys.stderr` or `logging`.
- For UI popups, strictly use the `windows-toasts` library with asynchronous callbacks.

## 3. Workflow Protocol
1. **Explore:** Read the existing codebase before modifying it.
2. **Plan:** Before writing code for a new executor or integration, output a short step-by-step plan.
3. **Implement:** Write the code strictly adhering to the architectural boundaries.
4. **Verify:** Do not claim success until you have added unit tests for the command schemas.
