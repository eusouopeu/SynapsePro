# -*- coding: utf-8 -*-

from aqt.qt import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QDialogButtonBox, Qt,
)

try:
    from .locales import _
except ImportError:
    def _(text):  # type: ignore
        return text

try:
    from .logging_util import get_log_file_path, read_log_tail
except ImportError:
    def get_log_file_path():  # type: ignore
        return None
    def read_log_tail(max_chars: int = 200_000) -> str:  # type: ignore
        return ""

HELP_TEXT = """
<h2>Troubleshooting & Manual Removal</h2>
<p>If you are unable to activate the addon or close Anki, you can manually remove SynapsePro to regain access.</p>

<b>Follow these steps:</b>
<ol>
    <li>Close this window and click the "Quit Anki" button in the activation dialog. If that fails, force-quit Anki (e.g., via Activity Monitor on Mac or Task Manager on Windows).</li>
    <li>Open Anki while holding down the <b>Shift key</b>. This will temporarily disable all addons.</li>
    <li>Go to <b>Tools > Add-ons</b> from the Anki menu.</li>
    <li>Select "SynapsePro" from the list.</li>
    <li>Click the "Delete" button on the right side.</li>
    <li>Restart Anki normally.</li>
</ol>

<p>If you believe your key is valid but not working, or for any other issues, please contact us:</p>
<p><a href='mailto:help.synapse.pro@gmail.com'>help.synapse.pro@gmail.com</a></p>

<p>If you're reporting a bug, click "Copy Diagnostic Log" below and paste the
result into your report — it's far more useful than a screenshot and never
contains card content or API keys.</p>
"""

class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("SynapsePro - Help"))
        self.setMinimumWidth(500)

        layout = QVBoxLayout()

        label = QLabel(_(HELP_TEXT))
        label.setWordWrap(True)
        label.setOpenExternalLinks(True)

        copy_log_btn = QPushButton(_("Copy Diagnostic Log"))
        copy_log_btn.setToolTip(_(
            "Copies recent SynapsePro log lines to the clipboard. "
            "Contains no card content, API keys, or personal data."
        ))
        copy_log_btn.clicked.connect(self._copy_diagnostic_log)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #86868B;")

        log_row = QHBoxLayout()
        log_row.addWidget(copy_log_btn)
        log_row.addWidget(self._status_label)
        log_row.addStretch(1)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(self.reject)

        layout.addWidget(label)
        layout.addLayout(log_row)
        layout.addWidget(button_box)

        self.setLayout(layout)

    def _copy_diagnostic_log(self):
        text = read_log_tail()
        path = get_log_file_path()
        if not text:
            header = _("No SynapsePro log entries yet.")
            if path:
                header += f"\n({path})"
            self._status_label.setText(_("Nothing to copy yet."))
            QApplication.clipboard().setText(header)
            return
        header = f"SynapsePro diagnostic log ({path})\n{'-' * 40}\n"
        QApplication.clipboard().setText(header + text)
        self._status_label.setText(_("Copied to clipboard."))
