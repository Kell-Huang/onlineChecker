"""QThread wrappers around the core layer.

Contract shared by every worker:

  - ``request_stop()`` sets a ``threading.Event``; core objects check it at
    natural yield points and unwind. Callers should stop and wait rather than
    ``terminate()``, which can leave a half-written file behind.
  - Exactly one terminal signal fires: ``finished`` / ``failed`` / ``cancelled``.
    Emitting nothing would leave the UI stuck in its running state.
  - Workers never touch widgets; they only emit.
"""
import threading
import traceback

from PySide6.QtCore import QThread, Signal

from core import runner
from core.errors import TokenError
from core.models import Selection
from core.productsup import Client
from core.progress import Reporter
from core.settings import Settings


class RunWorker(QThread):
    """Run one comparison and stream progress back to the UI."""

    progress = Signal(object)          # ProgressUpdate
    log = Signal(str, str)             # level, message
    finished_ok = Signal(object)       # RunResult
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, selection: Selection, settings: Settings,
                 credentials: dict, parent=None):
        super().__init__(parent)
        self._selection = selection
        self._settings = settings
        self._credentials = credentials
        self._cancel = threading.Event()

    def request_stop(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        reporter = Reporter(
            progress_callback=self.progress.emit,
            log_callback=lambda level, message: self.log.emit(level, message),
        )
        try:
            result = runner.run(self._selection, self._settings,
                                self._credentials, reporter, self._cancel)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")
            return

        if result.cancelled:
            self.cancelled.emit()
        elif result.success:
            self.finished_ok.emit(result)
        else:
            self.failed.emit(result.error or "The run failed.")


class MetadataWorker(QThread):
    """Fetch projects, sites or channels for the Browse tab."""

    loaded = Signal(str, str, list)     # kind, parent_id, items
    failed = Signal(str, str, str)      # kind, parent_id, message

    def __init__(self, kind: str, parent_id: str, token: str, base_url: str,
                 settings: Settings, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._parent_id = parent_id
        self._token = token
        self._base_url = base_url
        self._settings = settings

    def run(self) -> None:
        try:
            with Client(self._token, self._base_url, self._settings) as client:
                if self._kind == "projects":
                    items = client.projects()
                elif self._kind == "sites":
                    items = client.sites(self._parent_id)
                else:
                    items = client.channels(self._parent_id)
        except TokenError as exc:
            self.failed.emit(self._kind, self._parent_id, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(self._kind, self._parent_id,
                             f"{type(exc).__name__}: {exc}")
            return
        self.loaded.emit(self._kind, self._parent_id, items)
