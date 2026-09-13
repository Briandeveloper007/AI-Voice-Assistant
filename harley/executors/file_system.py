"""
executors/file_system.py — Local File System Executor
=====================================================
Provides controlled file operations with strict immutable path blocking
and non-destructive deletion.
"""

import ctypes
import logging
import os
import shutil
from pathlib import Path
from typing import List

try:
    import send2trash
    HAS_SEND2TRASH = True
except ImportError:
    HAS_SEND2TRASH = False

log = logging.getLogger("harley.executors.filesystem")


class SecurityViolation(Exception):
    """Raised when a file operation targets a protected directory."""
    pass


class FileSystemExecutor:
    """
    Executes file system actions with an enforcing layer of safety guards.
    """
    
    # Resolving common protected paths on Windows
    PROTECTED_PATHS = [
        Path("C:/Windows").resolve(),
        Path("C:/Program Files").resolve(),
        Path("C:/Program Files (x86)").resolve(),
        Path(__file__).parent.parent.resolve()  # Harley's own installation folder
    ]

    @staticmethod
    def _validate_path(target_path: Path) -> None:
        """
        Checks if the target path resides within an immutable/protected directory.
        """
        try:
            resolved_target = target_path.resolve()
        except Exception as e:
            raise ValueError(f"Invalid path format: {target_path}") from e
            
        for protected in FileSystemExecutor.PROTECTED_PATHS:
            try:
                # If target is relative to protected path, it is inside it.
                resolved_target.relative_to(protected)
                raise SecurityViolation(f"Path '{resolved_target}' is protected and cannot be modified.")
            except ValueError:
                # Not relative to this protected path, safe to proceed to next check
                pass
                
            # Direct match check
            if resolved_target == protected:
                raise SecurityViolation(f"Path '{resolved_target}' is protected and cannot be modified.")

    @staticmethod
    def search_files(directory: str, pattern: str) -> List[str]:
        """
        Searches for files matching a glob pattern within a directory.
        """
        base_dir = Path(directory)
        if not base_dir.exists() or not base_dir.is_dir():
            raise FileNotFoundError(f"Directory not found: {directory}")
            
        log.info("Searching for '%s' in '%s'", pattern, base_dir)
        return [str(p) for p in base_dir.rglob(pattern)]

    @staticmethod
    def open_file(path: str) -> None:
        """
        Opens a file or directory using the default Windows handler.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {path}")
        
        log.info("Opening file: %s", p)
        os.startfile(str(p))

    @staticmethod
    def move_file(source: str, destination: str) -> None:
        """
        Moves a file from source to destination. Both paths are validated against the blocklist.
        """
        src = Path(source)
        dst = Path(destination)
        
        if not src.exists():
            raise FileNotFoundError(f"Source file not found: {source}")
            
        FileSystemExecutor._validate_path(src)
        FileSystemExecutor._validate_path(dst)
        
        log.info("Moving file '%s' -> '%s'", src, dst)
        shutil.move(str(src), str(dst))

    @staticmethod
    def delete_file(path: str) -> None:
        """
        Sends a file to the Recycle Bin. Never permanently deletes.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {path}")
            
        FileSystemExecutor._validate_path(p)
        
        log.info("Sending file to Recycle Bin: %s", p)
        if HAS_SEND2TRASH:
            send2trash.send2trash(str(p))
        else:
            # Fallback to Windows Shell COM API (SHFileOperationW) for native Recycle Bin
            FO_DELETE = 3
            FOF_ALLOWUNDO = 0x0040        # Move to recycle bin
            FOF_NOCONFIRMATION = 0x0010   # Don't prompt the user
            FOF_SILENT = 0x0004           # Don't show progress UI
            
            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [
                    ("hwnd", ctypes.c_void_p),
                    ("wFunc", ctypes.c_uint),
                    ("pFrom", ctypes.c_wchar_p),
                    ("pTo", ctypes.c_wchar_p),
                    ("fFlags", ctypes.c_uint),
                    ("fAnyOperationsAborted", ctypes.c_int),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", ctypes.c_wchar_p)
                ]
            
            # Double null termination required for pFrom
            path_str = str(p.resolve()) + "\0"
            
            shfos = SHFILEOPSTRUCTW()
            shfos.hwnd = None
            shfos.wFunc = FO_DELETE
            shfos.pFrom = path_str
            shfos.pTo = None
            shfos.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
            shfos.fAnyOperationsAborted = 0
            shfos.hNameMappings = None
            shfos.lpszProgressTitle = None
            
            result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(shfos))
            if result != 0:
                raise RuntimeError(f"Failed to move file to Recycle Bin. Error code: {result}")
