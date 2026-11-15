"""Message dialog and the settings editor.

``QMessageBox`` brings the platform icon set and a grid layout that pushes the
text away from the icon, which is why the app draws its own: a line glyph on a
tinted badge, a title over a body, and an explicit primary/secondary button
hierarchy.
"""
from typing import Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSizePolicy, QSpinBox, QVBoxLayout,
)

from core.settings import Settings

from .icons import make_pixmap
from .theme import PRIMARY, DANGER, WARNING


class MessageDialog(QDialog):
    """Flat message dialog.

    ``choice`` is the index of the clicked button, or -1 when dismissed with
    Esc — which callers read as the safe answer.
    """

    _WIDTH = 440
    _CONTENT_WIDTH = _WIDTH - 20 - 20 - 32 - 12
    _SEVERITY = {
        "info": ("info", PRIMARY),
        "warning": ("alert", WARNING),
        "error": ("error", DANGER),
        "question": ("question", PRIMARY),
    }

    def __init__(self, parent, title: str, message: str,
                 severity: str = "info", buttons=None):
        super().__init__(parent)
        self.setObjectName("AppMessageDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.choice = -1

        glyph, colour = self._SEVERITY.get(severity, self._SEVERITY["info"])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 18)
        layout.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(12)
        badge = QLabel()
        badge.setObjectName(f"dlgBadge{severity.capitalize()}")
        badge.setFixedSize(32, 32)
        badge.setAlignment(Qt.AlignCenter)
        badge.setPixmap(make_pixmap(glyph, colour, 18))
        header.addWidget(badge, alignment=Qt.AlignTop)

        text_column = QVBoxLayout()
        text_column.setSpacing(6)
        title_label = QLabel(title)
        title_label.setObjectName("dlgTitle")
        title_label.setWordWrap(True)
        # Pin the width so the wrapped height is computed for the width the
        # label actually gets; otherwise the sizeHint invents extra lines and
        # the leftover height splits the title and body apart.
        title_label.setMinimumWidth(self._CONTENT_WIDTH)
        title_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        text_column.addWidget(title_label)

        body = QLabel(message)
        body.setObjectName("dlgBody")
        body.setWordWrap(True)
        body.setMinimumWidth(self._CONTENT_WIDTH)
        body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text_column.addWidget(body)

        header.addLayout(text_column, stretch=1)
        layout.addLayout(header)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch()
        for index, (text, primary, default) in enumerate(
                buttons or [("OK", True, True)]):
            button = QPushButton(text)
            button.setObjectName("dlgPrimaryBtn" if primary else "dlgSecondaryBtn")
            button.setAutoDefault(False)
            button.clicked.connect(lambda _c=False, i=index: self._choose(i))
            if default:
                button.setDefault(True)
            row.addWidget(button)
        layout.addLayout(row)

        self.setFixedWidth(self._WIDTH)
        self.adjustSize()

    def _choose(self, index: int) -> None:
        self.choice = index
        self.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)


_ICON_SEVERITY = {
    QMessageBox.Information: "info",
    QMessageBox.Warning: "warning",
    QMessageBox.Critical: "error",
    QMessageBox.Question: "question",
}


def show_message(parent, title: str, message: str,
                 icon=QMessageBox.Information) -> int:
    dialog = MessageDialog(parent, title, message,
                           _ICON_SEVERITY.get(icon, "info"))
    return dialog.exec()


class SettingsDialog(QDialog):
    """Edit the four settings and the token, in one place.

    The values written here are exactly the keys in ``settings.json``; nothing
    hidden, nothing nested.
    """

    def __init__(self, parent, settings: Settings, token: str):
        super().__init__(parent)
        self.setObjectName("SettingsDialog")
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 18)
        layout.setSpacing(14)

        title = QLabel("Settings")
        title.setObjectName("dlgTitle")
        layout.addWidget(title)

        hint = QLabel(
            "Stored in config/settings.json. The token lives in "
            "config/credentials.json and is never written to reports or logs."
        )
        hint.setObjectName("dlgBody")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(10)

        self.token_edit = QLineEdit(token)
        self.token_edit.setEchoMode(QLineEdit.Password)
        self.token_edit.setPlaceholderText("Productsup token")
        form.addRow("Token", self.token_edit)

        self.output_edit = QLineEdit(str(settings.output_dir))
        form.addRow("Output folder", self.output_edit)

        self.max_spin = QSpinBox()
        self.max_spin.setRange(1, 1_000_000)
        self.max_spin.setValue(settings.max_per_country)
        form.addRow("Max SKUs per country", self.max_spin)

        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 32)
        self.concurrency_spin.setValue(settings.download_concurrency)
        form.addRow("Parallel downloads", self.concurrency_spin)

        # Sampling: a fresh seed every run by default, so repeated runs check
        # different SKUs instead of the same thousand. Pinning a seed is only
        # for reproducing a report.
        self.random_sample_check = QCheckBox("Pick a different sample each run")
        self.random_sample_check.setChecked(settings.random_seed is None)
        self.random_sample_check.toggled.connect(
            lambda checked: self.seed_spin.setEnabled(not checked))
        form.addRow("Sampling", self.random_sample_check)

        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(0, 2_147_483_647)
        self.seed_spin.setValue(settings.random_seed
                                if settings.random_seed is not None else 42)
        self.seed_spin.setEnabled(settings.random_seed is not None)
        self.seed_spin.setToolTip(
            "Only used when the sample is pinned; copy it from the log to "
            "reproduce a run."
        )
        form.addRow("Fixed seed", self.seed_spin)

        layout.addLayout(form)

        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.setObjectName("dlgSecondaryBtn")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        save = QPushButton("Save")
        save.setObjectName("dlgPrimaryBtn")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        row.addWidget(save)
        layout.addLayout(row)

    def values(self) -> Tuple[str, str, int, int, Optional[int]]:
        return (
            self.token_edit.text().strip(),
            self.output_edit.text().strip(),
            self.max_spin.value(),
            self.concurrency_spin.value(),
            None if self.random_sample_check.isChecked() else self.seed_spin.value(),
        )
