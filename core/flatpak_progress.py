# dendro/core/flatpak_progress.py
"""
Read Flatpak's progress output so the dialog can show a progress bar and steps.

Flatpak prints progress as plain text lines when its output is not a terminal
(which is how Dendro runs it). Observed on Flatpak 1.16.6:

    Looking for updates…
     1.\\t   \\torg.test.App\\tstable\\tu\\tlocaltest\\t< 40.0 MB     <- planned steps
    Updating…                        0%  0 bytes/s                 <- progress
    Updating… ███████████████▍      77%  6.3 MB/s  00:01
    Updates complete.

With ``--noninteractive`` Flatpak prints no progress lines at all, so the stage is
known but no percentage is.

Honesty rules:
- The percentage is the one Flatpak reports for the operation in progress. The
  overall figure is an estimate built from the number of planned steps, and is shown
  only when the step count is known.
- Lines this module does not recognise are ignored. Nothing is inferred from them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

STAGE_WAITING = "waiting"
STAGE_LOOKING = "looking"
STAGE_UPDATING = "updating"
STAGE_COMPLETE = "complete"

_STEP_PATTERN = re.compile(r"^\s*(\d+)\.\t(.*)$")
_PERCENT_PATTERN = re.compile(r"(\d{1,3})%")
_SPEED_PATTERN = re.compile(r"(\d+(?:[.,]\d+)?)\s?(bytes|B|kB|MB|GB)/s")
_REF_PATTERN = re.compile(r"^Updating\s+(?:app|runtime)?/?([A-Za-z0-9_.\-]+)/")
_ETA_PATTERN = re.compile(r"(\d{2}:\d{2}(?::\d{2})?)\s*$")


@dataclass(frozen=True)
class FlatpakProgress:
    """One snapshot of an update, ready for the dialog to display."""

    stage: str = STAGE_WAITING
    steps: tuple[str, ...] = ()  # planned operations, e.g. "org.test.App stable"
    current: int = 0  # 1-based operation in progress; 0 if unknown
    percent: int | None = None  # Flatpak's percentage for the current operation
    speed: str | None = None
    eta: str | None = None
    completed_steps: int = 0
    finished_ok: bool = False
    name: str = ""  # application name from the "Updating <ref>" line, if printed

    @property
    def overall_percent(self) -> int | None:
        """Estimated share of the whole update. None when it cannot be estimated."""
        if self.stage == STAGE_COMPLETE:
            return 100
        if self.stage != STAGE_UPDATING or self.percent is None:
            return None
        if not self.steps:
            return self.percent
        fraction = (self.completed_steps + self.percent / 100) / len(self.steps)
        return max(0, min(99, int(fraction * 100)))

    @property
    def message(self) -> str:
        if self.stage == STAGE_LOOKING:
            return "Checking for updates…"
        if self.stage == STAGE_COMPLETE:
            return "Update finished."
        if self.stage == STAGE_UPDATING:
            name = self._current_name()
            if self.steps and self.current:
                return f"Updating {name} (step {self.current} of {len(self.steps)})"
            return f"Updating {name}"
        return "Starting update…"

    @property
    def status_text(self) -> str:
        """One line for the status label: step, percentage, speed, and time left."""
        parts = [self.message]
        shown = self.overall_percent
        if shown is None and self.stage == STAGE_UPDATING:
            shown = self.percent
        if shown is not None and self.stage != STAGE_COMPLETE:
            parts.append(f"{shown}%")
        if self.speed and self.stage == STAGE_UPDATING:
            parts.append(self.speed)
        if self.eta and self.stage == STAGE_UPDATING:
            parts.append(f"{self.eta} left")
        return " · ".join(parts)

    @property
    def steps_text(self) -> str:
        """The planned operations, one per line, marked done, current, or pending."""
        lines = []
        finished = self.stage == STAGE_COMPLETE
        for index, step in enumerate(self.steps):
            if finished or index < self.completed_steps:
                mark = "✓"
            elif index + 1 == self.current:
                mark = "▶"
            else:
                mark = "·"
            lines.append(f"{mark} {step}")
        return "\n".join(lines)

    def _current_name(self) -> str:
        if self.steps and 1 <= self.current <= len(self.steps):
            return self.steps[self.current - 1].split(" ", 1)[0]
        return self.name or "the app"


@dataclass
class ProgressTracker:
    """Turns Flatpak's output lines, in order, into successive ``FlatpakProgress`` values.

    Feed one line at a time with ``feed``. Lines that carry no progress information
    return ``None``, so the caller keeps the previous snapshot.
    """

    _steps: list[str] = field(default_factory=list)
    _stage: str = STAGE_WAITING
    _completed: int = 0
    _last_percent: int | None = None
    _speed: str | None = None
    _eta: str | None = None
    _percent: int | None = None
    _finished_ok: bool = False
    _name: str = ""

    def feed(self, line: str) -> FlatpakProgress | None:
        text = line.strip()
        if not text:
            return None
        if text.startswith("Looking for updates"):
            self._stage = STAGE_LOOKING
            return self._snapshot()
        if text.startswith("Nothing to do"):
            self._stage = STAGE_COMPLETE
            self._finished_ok = True
            return self._snapshot()
        if text.startswith("Updates complete"):
            self._stage = STAGE_COMPLETE
            self._finished_ok = True
            self._percent = 100
            return self._snapshot()
        step = _STEP_PATTERN.match(line)
        if step is not None and self._stage in (STAGE_LOOKING, STAGE_WAITING):
            self._steps.append(_describe_step(step.group(2)))
            return self._snapshot()
        if text.startswith("Updating"):
            return self._feed_updating(text)
        return None

    def _feed_updating(self, text: str) -> FlatpakProgress:
        self._stage = STAGE_UPDATING
        ref = _REF_PATTERN.match(text)
        if ref is not None:
            self._name = ref.group(1)
        percent_match = _PERCENT_PATTERN.search(text)
        if percent_match is None:
            # Start of an operation, or a --noninteractive run with no percentage.
            self._percent = None
            self._speed = None
            self._eta = None
            return self._snapshot()
        percent = min(100, int(percent_match.group(1)))
        # A drop from 100% means the previous operation finished and a new one began.
        if (
            self._last_percent is not None
            and self._last_percent >= 100
            and percent < 100
        ):
            self._completed += 1
        self._last_percent = percent
        self._percent = percent
        speed = _SPEED_PATTERN.search(text)
        self._speed = f"{speed.group(1)} {speed.group(2)}/s" if speed else None
        eta = _ETA_PATTERN.search(text)
        self._eta = eta.group(1) if eta else None
        return self._snapshot()

    def _snapshot(self) -> FlatpakProgress:
        steps = tuple(self._steps)
        completed = min(self._completed, len(steps)) if steps else 0
        current = 0
        if self._stage == STAGE_UPDATING:
            current = min(completed + 1, len(steps)) if steps else 0
        return FlatpakProgress(
            stage=self._stage,
            steps=steps,
            current=current,
            percent=self._percent,
            speed=self._speed,
            eta=self._eta,
            completed_steps=completed,
            finished_ok=self._finished_ok,
            name=self._name,
        )


def progress_line_handler(
    on_progress: Callable[[FlatpakProgress], None],
) -> Callable[[str], None]:
    """Return a line callback for ``CancellableRunner`` that reports each new snapshot.

    Each run needs its own handler, because the tracker keeps state between lines.
    The handler is called on the runner's reader thread.
    """
    tracker = ProgressTracker()

    def handle(line: str) -> None:
        snapshot = tracker.feed(line)
        if snapshot is not None:
            on_progress(snapshot)

    return handle


def _describe_step(raw: str) -> str:
    """Return ``"<app id> <branch>"`` from one planned-step row, else the raw text."""
    fields = [part.strip() for part in raw.split("\t") if part.strip()]
    if len(fields) >= 2:
        return f"{fields[0]} {fields[1]}"
    return raw.strip()


__all__ = (
    "STAGE_COMPLETE",
    "STAGE_LOOKING",
    "STAGE_UPDATING",
    "STAGE_WAITING",
    "FlatpakProgress",
    "ProgressTracker",
)
