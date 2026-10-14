"""Preview dialog: shows exactly which tags would change before anything
is written, with a checkbox per field.
"""

from __future__ import annotations

from PyQt6 import QtCore, QtWidgets

COLUMN_LABELS = ('', 'Field', 'Current Value', 'New Value')


class DiscogsPreviewDialog(QtWidgets.QDialog):
    """Shows a checkable table of field changes; `selected_fields` holds the
    set of field names the user approved after the dialog is accepted.
    """

    def __init__(self, diff_rows: list[dict], parent=None, context_label: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Preview Discogs Tag Changes')
        self.resize(640, 420)

        self._diff_rows = diff_rows
        self.selected_fields: set[str] = set()

        layout = QtWidgets.QVBoxLayout(self)

        if context_label:
            context_widget = QtWidgets.QLabel(context_label)
            context_widget.setWordWrap(True)
            layout.addWidget(context_widget)

        if not diff_rows:
            layout.addWidget(QtWidgets.QLabel(
                'No fields would change \u2014 MusicBrainz already provided everything '
                'this release could add.'
            ))
        else:
            layout.addWidget(QtWidgets.QLabel(
                f'{len(diff_rows)} field(s) would change. Uncheck any you want to skip:'
            ))

        self.table = QtWidgets.QTableWidget(len(diff_rows), len(COLUMN_LABELS), self)
        self.table.setHorizontalHeaderLabels(COLUMN_LABELS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QtWidgets.QHeaderView.ResizeMode.Stretch
        )

        self._checkboxes: list[QtWidgets.QCheckBox] = []
        for row_index, row in enumerate(diff_rows):
            checkbox = QtWidgets.QCheckBox(self)
            checkbox.setChecked(True)
            self._checkboxes.append(checkbox)
            cell = QtWidgets.QWidget(self)
            cell_layout = QtWidgets.QHBoxLayout(cell)
            cell_layout.addWidget(checkbox)
            cell_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(row_index, 0, cell)

            field_label = row['field'] + (' (Discogs-only)' if row['supplementary'] else '')
            self.table.setItem(row_index, 1, QtWidgets.QTableWidgetItem(field_label))
            self.table.setItem(row_index, 2, QtWidgets.QTableWidgetItem(row['current']))
            self.table.setItem(row_index, 3, QtWidgets.QTableWidgetItem(row['new']))

        self.table.resizeRowsToContents()
        layout.addWidget(self.table)

        select_row = QtWidgets.QHBoxLayout()
        select_all_btn = QtWidgets.QPushButton('Select All', self)
        select_none_btn = QtWidgets.QPushButton('Select None', self)
        select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        select_none_btn.clicked.connect(lambda: self._set_all_checked(False))
        select_row.addWidget(select_all_btn)
        select_row.addWidget(select_none_btn)
        select_row.addStretch(1)
        layout.addLayout(select_row)

        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            QtCore.Qt.Orientation.Horizontal,
            self,
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        if not diff_rows:
            button_box.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        layout.addWidget(button_box)

    def _set_all_checked(self, checked: bool) -> None:
        for checkbox in self._checkboxes:
            checkbox.setChecked(checked)

    def accept(self) -> None:
        self.selected_fields = {
            row['field']
            for row, checkbox in zip(self._diff_rows, self._checkboxes)
            if checkbox.isChecked()
        }
        super().accept()
