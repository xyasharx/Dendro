"""Process-local ownership gate for host-affecting package operations.

The DNF5 controller already serializes its own state machine.  Dendro also has
one deliberately narrow non-daemon operation: deleting an allowed repository
configuration file through its Polkit helper.  This gate makes those two paths
mutually exclusive without introducing polling, timeouts, or a hidden command
queue.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ActiveHostOperation:
    """The operation currently holding the process-local host-operation gate."""

    owner: str
    label: str


class HostOperationGate:
    """Serialize operations that can change host package-management state.

    ``claim`` is re-entrant for the current owner so a resolved DNF plan can
    retain its reservation through review and later execution.  It returns the
    active conflicting operation instead of raising a UI-specific exception;
    each caller converts that result into its own typed error surface.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: ActiveHostOperation | None = None

    @property
    def active(self) -> ActiveHostOperation | None:
        with self._lock:
            return self._active

    def claim(self, owner: str, label: str) -> ActiveHostOperation | None:
        """Claim the gate, or return the conflicting operation if it is held."""
        if not owner or not label:
            raise ValueError("Host-operation gate owner and label must be non-empty.")
        with self._lock:
            if self._active is None:
                self._active = ActiveHostOperation(owner=owner, label=label)
                return None
            if self._active.owner == owner:
                return None
            return self._active

    def release(self, owner: str) -> None:
        """Release the caller's claim; mismatched release is a programming error."""
        with self._lock:
            if self._active is None:
                return
            if self._active.owner != owner:
                raise RuntimeError(
                    f"Host-operation gate is owned by {self._active.owner!r}, not {owner!r}."
                )
            self._active = None
