"""
Harley Voice Assistant - UI Confirmations
Handles multi-modal interactive Windows Toasts for Level 2 (CONFIRM) security constraints.
"""

import logging
import threading
import multiprocessing as mp

# Requires: pip install windows-toasts
from windows_toasts import (
    InteractableWindowsToaster,
    Toast,
    ToastButton,
    ToastActivatedEventArgs
)
from dataclasses import dataclass

@dataclass
class ConfirmationResult:
    approved: bool
    timed_out: bool
    intent_id: str
    action: str
    origin: str

logger = logging.getLogger("HarleyToasts")

async def request_action_confirmation(intent, event_queue: mp.Queue) -> ConfirmationResult:
    """
    Creates an interactive Toast with 'Allow' and 'Deny' buttons.
    Runs asynchronously in a daemon thread so it doesn't block the Orchestrator loop.
    Pushes a 'CONFIRMATION_RESULT' dictionary back into the IPC event_queue.
    """
    import asyncio
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def _display_toast():
        try:
            # Toaster must be initialized inside the thread for COM apartment safety
            toaster = InteractableWindowsToaster("Harley Voice Assistant")
            
            toast = Toast()
            
            # Format UI strings from the StructuredIntent
            app_name = intent.application.title()
            action_name = intent.action.replace("_", " ").title()
            
            toast.text_fields = [
                "Security Confirmation Required",
                f"App: {app_name}",
                f"Action: {action_name}. Do you want to allow this?"
            ]

            # Triggered when a ToastButton is clicked
            def on_activated(args: ToastActivatedEventArgs):
                logger.info(f"[UI] Toast Activated with argument: {args.arguments}")
                if event_queue:
                    event_queue.put({
                        "type": "CONFIRMATION_RESULT",
                        "intent": intent,
                        "result": args.arguments  # Expected: 'ALLOW' or 'DENY'
                    })
                result = ConfirmationResult(
                    approved=(args.arguments == "ALLOW"),
                    timed_out=False,
                    intent_id=str(id(intent)),
                    action=intent.action,
                    origin=intent.origin.value
                )
                loop.call_soon_threadsafe(fut.set_result, result)

            # Triggered if the toast expires or the user swipes it away
            def on_dismissed(args):
                logger.warning("[UI] Toast dismissed or expired. Defaulting to DENY.")
                if event_queue:
                    event_queue.put({
                        "type": "CONFIRMATION_RESULT",
                        "intent": intent,
                        "result": "DENY"
                    })
                result = ConfirmationResult(
                    approved=False,
                    timed_out=True,
                    intent_id=str(id(intent)),
                    action=intent.action,
                    origin=intent.origin.value
                )
                loop.call_soon_threadsafe(fut.set_result, result)

            toast.on_activated = on_activated
            toast.on_dismissed = on_dismissed

            # Attach interactive action buttons
            toast.AddAction(ToastButton("Allow", "ALLOW"))
            toast.AddAction(ToastButton("Deny", "DENY"))
            
            # Dispatch the toast to the Windows Action Center
            toaster.show_toast(toast)
            
        except Exception as e:
            logger.error(f"[UI] Failed to display toast: {e}")
            # Failsafe: if the UI engine crashes, instantly deny the sensitive action
            if event_queue:
                event_queue.put({
                    "type": "CONFIRMATION_RESULT",
                    "intent": intent,
                    "result": "DENY"
                })
            result = ConfirmationResult(
                approved=False,
                timed_out=False,
                intent_id=str(id(intent)),
                action=intent.action,
                origin=intent.origin.value
            )
            loop.call_soon_threadsafe(fut.set_result, result)

    # Dispatch to a background thread
    threading.Thread(target=_display_toast, daemon=True).start()
    return await fut
