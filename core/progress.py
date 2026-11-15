"""Progress reporting shared by the engine and the UI.

The engine never touches Qt. It reports through ``Reporter``, which forwards
plain ``ProgressUpdate`` objects to whatever callback the caller installed —
a QThread signal in the app, a stdout printer in the CLI, a no-op in tests.

``Throttle`` exists because a download loop can emit thousands of updates a
second; crossing a thread boundary that often starves the UI thread.
"""
import time
from dataclasses import dataclass
from typing import Callable, Optional

# The run is split into weighted phases so the bar never jumps backwards when
# the engine moves from downloading to comparing to querying.
PHASE_GOS = 'gos'
PHASE_DOWNLOAD = 'download'
PHASE_COMPARE = 'compare'
PHASE_API = 'api'
PHASE_REPORT = 'report'

PHASE_WEIGHTS = {
    PHASE_GOS: (0.00, 0.08),
    PHASE_DOWNLOAD: (0.08, 0.42),
    PHASE_COMPARE: (0.42, 0.55),
    PHASE_API: (0.55, 0.95),
    PHASE_REPORT: (0.95, 1.00),
}


@dataclass
class ProgressUpdate:
    """One progress tick: a phase, a position inside it, and a label."""
    phase: str
    fraction: float          # 0..1 inside the phase
    label: str = ""
    detail: str = ""

    @property
    def overall(self) -> float:
        start, span = PHASE_WEIGHTS.get(self.phase, (0.0, 1.0))
        return max(0.0, min(1.0, start + span * self.fraction))

    @property
    def percent(self) -> int:
        return int(round(self.overall * 100))


class Throttle:
    """Allow at most one event per ``interval`` seconds.

    ``allow(force=True)`` always passes, so the final "100%" tick is never
    swallowed by the rate limiter.
    """

    def __init__(self, interval: float = 0.1):
        self.interval = interval
        self._last = 0.0

    def allow(self, force: bool = False) -> bool:
        now = time.monotonic()
        if force or now - self._last >= self.interval:
            self._last = now
            return True
        return False


class Reporter:
    """Fan out progress and log lines to optional callbacks."""

    def __init__(self,
                 progress_callback: Optional[Callable[[ProgressUpdate], None]] = None,
                 log_callback: Optional[Callable[[str, str], None]] = None,
                 throttle: Optional[Throttle] = None):
        self._progress_cb = progress_callback
        self._log_cb = log_callback
        self._throttle = throttle or Throttle()

    def log(self, level: str, message: str) -> None:
        if self._log_cb is not None:
            self._log_cb(level.upper(), message)

    def progress(self, phase: str, fraction: float, label: str = "",
                 detail: str = "", force: bool = False) -> None:
        if self._progress_cb is None or not self._throttle.allow(force):
            return
        self._progress_cb(ProgressUpdate(
            phase=phase,
            fraction=max(0.0, min(1.0, fraction)),
            label=label,
            detail=detail,
        ))
