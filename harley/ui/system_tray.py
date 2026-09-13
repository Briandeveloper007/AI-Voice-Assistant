"""
ui/system_tray.py — Windows System Tray Integration
===================================================
Provides a non-blocking system tray icon using pystray, running 
in a dedicated thread to ensure the Windows message pump stays responsive.
"""

import logging
import queue
import threading
from typing import Any

import pystray
from PIL import Image, ImageDraw
from pystray import MenuItem as item

log = logging.getLogger("harley.ui.system_tray")


class SystemTrayManager:
    """
    Manages the Harley desktop tray icon, providing cross-thread event 
    dispatching and global safety overrides.
    """
    def __init__(
        self, 
        shutdown_event: Any,
        interrupt_playback_event: Any,
        pause_event: Any,
        event_queue: Any,
        tts_queue: Any
    ):
        self.shutdown_event = shutdown_event
        self.interrupt_playback_event = interrupt_playback_event
        self.pause_event = pause_event
        self.event_queue = event_queue
        self.tts_queue = tts_queue
        
        self.current_state = "Idle"
        self.icon = None
        self._thread = None

    def _create_icon_image(self) -> Image.Image:
        """Generates a simple 'H' icon for the tray using Pillow."""
        width = 64
        height = 64
        color_bg = (0, 120, 215) # Windows blue
        color_fg = (255, 255, 255)
        
        image = Image.new('RGB', (width, height), color_bg)
        dc = ImageDraw.Draw(image)
        # Draw 'H'
        dc.rectangle([16, 16, 24, 48], fill=color_fg)
        dc.rectangle([40, 16, 48, 48], fill=color_fg)
        dc.rectangle([24, 28, 40, 36], fill=color_fg)
        return image

    def _get_status_text(self, icon: pystray.Icon) -> str:
        """Dynamic text for the Status menu item."""
        if self.pause_event.is_set():
            return "Status: Paused"
        return f"Status: {self.current_state}"

    def _toggle_pause(self, icon: pystray.Icon, menu_item: pystray.MenuItem) -> None:
        """Toggles Harley's listening mode (Pause/Resume)."""
        if self.pause_event.is_set():
            self.pause_event.clear()
            log.info("System Tray: Resumed Harley listening mode.")
        else:
            self.pause_event.set()
            log.info("System Tray: Paused Harley listening mode.")
        
        if self.icon:
            self.icon.update_menu()

    def _emergency_stop(self, icon: pystray.Icon, menu_item: pystray.MenuItem) -> None:
        """
        Global Kill Switch. Instantly halts audio out, flushes queues, and cancels tasks.
        """
        log.warning("System Tray: 🚨 EMERGENCY STOP TRIGGERED 🚨")
        
        # 1. Instantly halt TTS playback
        self.interrupt_playback_event.set()
        
        # 2. Clear TTS queue (Best effort flush of multiprocessing queue)
        try:
            while not self.tts_queue.empty():
                self.tts_queue.get_nowait()
        except queue.Empty:
            pass
        except Exception as e:
            log.error("Error flushing TTS queue during emergency stop: %s", e)
            
        # 3. Fire cancel event to orchestrator to stop active LLM / executor tasks
        try:
            self.event_queue.put_nowait({"type": "EMERGENCY_STOP"})
        except queue.Full:
            pass
        except Exception as e:
            log.error("Error sending emergency stop event: %s", e)

    def _exit_app(self, icon: pystray.Icon, menu_item: pystray.MenuItem) -> None:
        """Cleanly sets the global shutdown event."""
        log.info("System Tray: Exit requested by user.")
        self.shutdown_event.set()
        icon.stop()

    def update_state(self, new_state: str) -> None:
        """
        Safely callable from other threads/processes to update the UI text.
        """
        if self.current_state != new_state:
            self.current_state = new_state
            if self.icon:
                self.icon.update_menu()

    def _run_tray(self) -> None:
        """The blocking loop for pystray, executed inside the daemon thread."""
        menu = pystray.Menu(
            item(self._get_status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            item(lambda text: "Resume Harley" if self.pause_event.is_set() else "Pause Harley", self._toggle_pause),
            item("🚨 Emergency Stop", self._emergency_stop),
            pystray.Menu.SEPARATOR,
            item("Exit", self._exit_app)
        )
        
        self.icon = pystray.Icon("Harley", self._create_icon_image(), "Harley AI", menu)
        log.info("System tray icon initialized and running.")
        self.icon.run()

    def start(self) -> None:
        """
        Spawns the dedicated thread to run the tray icon, returning immediately
        to unblock the main asyncio event loop.
        """
        self._thread = threading.Thread(target=self._run_tray, name="SystemTrayThread", daemon=True)
        self._thread.start()
        log.info("System tray thread spawned.")
