"""
executors/windows.py — Native Windows Action Executor
=====================================================
Medium-integrity actions for Windows, prioritizing safe API calls
and preventing raw shell injection.
"""

import logging
import os
import subprocess

import win32api
import win32con

log = logging.getLogger("harley.executors.windows")


class WindowsExecutor:
    """
    Executes native Windows commands safely, without using shell=True.
    """

    @staticmethod
    def open_settings(page: str = "ms-settings:"):
        """
        Opens Windows settings pages using URI schemes.
        Examples: 'ms-settings:bluetooth', 'ms-settings:display'
        """
        if not page.startswith("ms-settings:"):
            page = f"ms-settings:{page}"
        log.info("Opening settings page: %s", page)
        
        # os.startfile safely maps to ShellExecute without invoking a subshell.
        os.startfile(page)
        
    @staticmethod
    def press_media_key(key: str) -> None:
        """
        Presses a media or volume key using the Windows API.
        """
        keys = {
            "play_pause": win32con.VK_MEDIA_PLAY_PAUSE,
            "next": win32con.VK_MEDIA_NEXT_TRACK,
            "prev": win32con.VK_MEDIA_PREV_TRACK,
            "vol_up": win32con.VK_VOLUME_UP,
            "vol_down": win32con.VK_VOLUME_DOWN,
            "mute": win32con.VK_VOLUME_MUTE,
        }
        vk = keys.get(key.lower())
        if not vk:
            raise ValueError(f"Unknown media key: {key}")
            
        log.info("Simulating media key press: %s", key)
        # Press
        win32api.keybd_event(vk, 0, 0, 0)
        # Release
        win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
        
    @staticmethod
    def power_action(action: str, delay_seconds: int = 30) -> None:
        """
        Initiates a system shutdown or restart using a strictly structured command.
        NEVER uses shell=True.
        """
        args = ["shutdown", "/t", str(delay_seconds)]
        if action.lower() == "shutdown":
            args.insert(1, "/s")
        elif action.lower() == "restart":
            args.insert(1, "/r")
        else:
            raise ValueError("Action must be 'shutdown' or 'restart'.")
            
        log.warning("Executing system power action: %s", args)
        
        # Medium Integrity ONLY: shell=False prevents shell injection attacks.
        subprocess.run(args, shell=False, check=True)
