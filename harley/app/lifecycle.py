"""
app/lifecycle.py — OS Lifecycle Event Hooks
===========================================
Listens to native Windows events (Sleep/Wake, Session Lock/Unlock) 
via a hidden window message pump to gracefully manage audio hardware 
and system states.
"""

import ctypes
import logging
import threading
import time
from typing import Any

import win32api
import win32con
import win32gui

log = logging.getLogger("harley.app.lifecycle")

# WTS API Constants for Session Change notifications
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0


class LifecycleWatchdog:
    """
    Hooks into the Windows native message pump to listen for power and session
    changes, dispatching structured events back to Harley's central orchestrator.
    """
    def __init__(self, event_queue: Any):
        """
        Args:
            event_queue: A multiprocessing Queue to pipe system events back to the main Orchestrator.
        """
        self.event_queue = event_queue
        self._thread = None
        self.hwnd = None

    def _wndproc(self, hwnd, msg, wparam, lparam):
        """Window message procedure callback to process OS broadcasts."""
        try:
            if msg == win32con.WM_POWERBROADCAST:
                if wparam == win32con.PBT_APMSUSPEND:
                    log.info("OS Event: System Suspend (Sleep) detected.")
                    # Signal Audio process to gracefully close WASAPI streams
                    self.event_queue.put_nowait({"type": "OS_SUSPEND"})
                    
                elif wparam == win32con.PBT_APMRESUMEAUTOMATIC:
                    log.info("OS Event: System Resume detected.")
                    # Audio drivers often take a moment to re-initialize from sleep.
                    # We wait 2 seconds asynchronously before attempting stream reconnection.
                    def resume_audio():
                        time.sleep(2.0)
                        log.info("Triggering stream reconnection after resume delay.")
                        self.event_queue.put_nowait({"type": "OS_RESUME"})
                        
                    threading.Thread(target=resume_audio, daemon=True).start()
                    
            elif msg == WM_WTSSESSION_CHANGE:
                if wparam == WTS_SESSION_LOCK:
                    log.info("OS Event: Workstation Locked.")
                    self.event_queue.put_nowait({"type": "OS_LOCKED"})
                    
                elif wparam == WTS_SESSION_UNLOCK:
                    log.info("OS Event: Workstation Unlocked.")
                    self.event_queue.put_nowait({"type": "OS_UNLOCKED"})
                    
            elif msg == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
                
        except Exception as e:
            log.error("Error in Lifecycle WndProc: %s", e)
            
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _message_pump_loop(self):
        """Creates the hidden window and runs the blocking Windows message loop."""
        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = self._wndproc  # type: ignore
        wc.lpszClassName = "HarleyLifecycleHiddenWindow"  # type: ignore
        wc.hInstance = win32api.GetModuleHandle(None)  # type: ignore

        try:
            win32gui.RegisterClass(wc)
        except win32gui.error as e:
            if e.winerror != 1410: # 1410 means "Class already exists"
                log.error("Failed to register window class: %s", e)
                return

        self.hwnd = win32gui.CreateWindow(
            wc.lpszClassName,
            "HarleyLifecycleListener",
            0,          # style
            0, 0, 0, 0, # dimensions (0 ensures hidden)
            0,          # parent
            0,          # menu
            wc.hInstance,
            None
        )

        if not self.hwnd:
            log.error("Failed to create hidden window for OS event hooks.")
            return

        # Register for workstation lock/unlock notifications via ctypes
        wtsapi32 = ctypes.windll.wtsapi32
        success = wtsapi32.WTSRegisterSessionNotification(self.hwnd, NOTIFY_FOR_THIS_SESSION)
        if not success:
            log.warning("Failed to register for WTS Session Notifications. Lock/Unlock events will not fire.")
        else:
            log.debug("Successfully registered for WTS Session Notifications.")

        log.info("Lifecycle watchdog message pump started.")
        # Blocking pump that yields thread control until a message arrives
        win32gui.PumpMessages()

    def start_lifecycle_watchdog(self) -> None:
        """
        Spawns the watchdog loop in a non-blocking daemon thread.
        This must be called from the main thread during initialization.
        """
        self._thread = threading.Thread(
            target=self._message_pump_loop,
            name="LifecycleWatchdogThread",
            daemon=True
        )
        self._thread.start()
        log.info("OS Lifecycle watchdog spawned.")
