"""Release picker: the dialog that decides which Discogs release gets applied.

Two Discogs releases of the same album routinely differ only in pressing
detail — a 2018 Japanese SHM-CD reissue versus the 1987 UK original — and
that difference is exactly what the user is choosing between. A one-line
"title, year, country" label hides it, so this presents every distinguishing
fact Discogs returns, in a sortable table.

What /database/search actually returns is limited: title, year, country,
label, catalogue number, barcode, genre, style, format, and (on the current
API) formats[] with disc quantity and edition descriptors. Track counts and
company/publisher credits are NOT in a search response — they require a
separate /releases/{id} fetch each. Rather than multiply every search by N
requests against a 60-per-minute budget, those columns fill in lazily: the
selected row is fetched automatically, and the user can ask for the rest.
"""

from __future__ import annotations

from typing import Any, Callable

from PyQt6 import QtCore, QtGui, QtWidgets

from .discogs_mapping import (
    describe_match,
    summarize_release_detail,
    summarize_search_result,
)

# (header, row key, numeric, default width, tooltip)
#
# A width of 0 means "size to contents" — right for the short columns whose
# content never varies much (a year, a disc count). The text columns get an
# explicit starting width instead, because sizing them to contents lets a
# single long release title set the width for the whole column, and leaving
# them all on Stretch lets the fixed columns claim the space first and
# squeeze the names down to "P…". One column (Release) stretches to absorb
# whatever is left over.
COLUMNS: tuple[tuple[str, str, bool, int, str], ...] = (
    ('Match', 'score', True, 0,
     'How well this release agrees with what Picard already knows about the '
     'files — barcode, catalogue number, year, country and medium. Hover a '
     'cell to see exactly which facts agreed. Blank when there was nothing '
     'to compare against.'),
    ('Artist', 'artist', False, 130, 'Artist as Discogs credits it on this release.'),
    ('Release', 'title', False, -1, 'Release title as it appears on Discogs.'),
    ('Year', 'year', True, 0, 'Year of THIS pressing, not of the original release.'),
    ('Country', 'country', False, 0, 'Country this pressing was released in.'),
    ('Media', 'media', False, 115,
     'Physical carrier, with digital quality in brackets (FLAC, 320 kbps).'),
    ('Discs', 'discs', True, 0,
     'Number of physical discs. Digital releases count as one regardless of '
     'how many files they contain.'),
    ('Tracks', 'tracks', True, 0,
     'Audio track count. Needs a per-release lookup, so it fills in when you '
     'select a row or press "Load all details".'),
    ('Edition', 'edition', False, 145,
     'What distinguishes this pressing: Reissue, Remastered, Deluxe, Limited, '
     'Promo, Mono and so on.'),
    ('Label', 'label', False, 125, 'Record label(s) on this pressing.'),
    ('Cat #', 'catno', False, 115, 'Catalogue number — often the single most reliable identifier.'),
    ('Have / Want', 'community', True, 0,
     'How many Discogs users own this pressing versus want it. A high "have" '
     'usually marks the common, well-documented pressing.'),
)

# Fetching detail for every row costs one Discogs request per row against a
# 60-requests-per-minute budget, so bulk loading asks first above this many.
BULK_CONFIRM_THRESHOLD = 10


class _SortableItem(QtWidgets.QTableWidgetItem):
    """Table cell that sorts on a supplied key rather than its own text.

    QTableWidgetItem compares display strings, which sorts years and track
    counts lexically — "9" after "10", and a blank cell above everything.
    Holding the sort key separately keeps numeric columns numeric and parks
    not-yet-known values at the end of either sort direction.
    """

    def __init__(self, text: str, sort_key: Any = None) -> None:
        super().__init__(text)
        self._sort_key = sort_key

    def __lt__(self, other: Any) -> bool:
        mine = self._sort_key
        theirs = getattr(other, '_sort_key', None)
        if mine is None:
            return False          # unknown values sink
        if theirs is None:
            return True
        try:
            return mine < theirs
        except TypeError:
            return str(mine) < str(theirs)


def _format_summary_text(row: dict[str, Any]) -> str:
    """Build the detail pane's text for one row.

    Deliberately plain text rather than a second table: this is the pane
    someone reads to settle a tie between two near-identical pressings, and
    a label/value list reads faster than a grid. Only fields with a value
    appear, so the pane never shows a column of em-dashes.
    """
    lines: list[str] = []

    heading = ' – '.join(part for part in (row.get('artist'), row.get('title')) if part)
    if heading:
        lines.append(f'<b>{_escape(heading)}</b>')

    def add(label: str, value: Any, suffix: str = '') -> None:
        if value in (None, '', 0):
            return
        lines.append(f'<b>{_escape(label)}:</b> {_escape(str(value))}{suffix}')

    if row.get('is_master'):
        lines.append(
            '<i>This is a Discogs <b>master</b> release — it groups every '
            'pressing of the work and carries no per-pressing barcode, '
            'catalogue number, country or credits. Pick a specific pressing '
            'instead.</i>'
        )

    if row.get('match_explanation'):
        lines.append(f"<b>Match:</b> {_escape(row['match_explanation'])}")

    add('Released', row.get('released') or row.get('year'))
    add('Country', row.get('country'))
    add('Format', ', '.join(p for p in (row.get('pieces'), row.get('format')) if p))
    add('Packaging', row.get('notes'))
    add('Discs', row.get('discs'))
    if row.get('tracks') is not None:
        total = row['tracks']
        duration = row.get('duration')
        add('Tracks', f'{total}' + (f'  ({duration})' if duration else ''))
    else:
        lines.append('<b>Tracks:</b> <i>not loaded yet</i>')
    add('Label', row.get('label'))
    add('Catalogue no.', row.get('catno'))
    add('Barcode', row.get('barcode'))
    add('Genre', row.get('genre'))
    add('Style', row.get('style'))
    add('Companies', row.get('companies'))
    add('Discogs data quality', row.get('quality_rating'))
    have, want = row.get('have') or 0, row.get('want') or 0
    if have or want:
        add('Community', f'{have} have, {want} want')
    add('Release ID', row.get('release_id'))

    return '<br>'.join(lines) if lines else '<i>Nothing to show for this row.</i>'


def _escape(text: str) -> str:
    """Escape for the rich-text detail pane.

    Release titles and company names come straight from user-submitted
    Discogs data and routinely contain & and <, which would otherwise be
    swallowed or mangled as markup.
    """
    return (str(text)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;'))


def _discogs_url(row: dict[str, Any]) -> str:
    """Public web URL for a row, from whichever field Discogs supplied."""
    uri = str(row.get('uri') or '')
    if uri.startswith('http'):
        return uri
    if uri:
        return 'https://www.discogs.com' + ('' if uri.startswith('/') else '/') + uri
    release_id = row.get('release_id')
    if release_id:
        kind = 'master' if row.get('is_master') else 'release'
        return f'https://www.discogs.com/{kind}/{release_id}'
    return ''


class DiscogsPickerDialog(QtWidgets.QDialog):
    """Shows Discogs search candidates; `selected_result` holds the pick.

    `results` is the raw list from /database/search, best-first.
    `match_details`, when given, is the parallel list of dicts from
    rank_release_candidates_detailed(), which drives the Match column and
    its per-row explanation.
    `detail_fetcher`, when given, is called as detail_fetcher(release_id,
    callback) to load the extras a search response omits; callback receives
    the release dict or None. It is injected rather than imported so this
    dialog stays free of any network or Picard dependency.
    """

    def __init__(
        self,
        results: list[dict[str, Any]],
        parent=None,
        match_details: list[dict[str, Any]] | None = None,
        detail_fetcher: Callable[[Any, Callable[[Any], None]], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle('Choose Discogs Release')
        self.resize(1180, 620)

        self._results = list(results or [])
        self._detail_fetcher = detail_fetcher
        self._detail_requested: set[Any] = set()
        self.selected_result: dict[str, Any] | None = None

        match_details = list(match_details or [])
        self._rows: list[dict[str, Any]] = []
        for index, result in enumerate(self._results):
            detail = match_details[index] if index < len(match_details) else None
            row = summarize_search_result(
                result, score=(detail or {}).get('score'))
            label, explanation = describe_match(detail or {})
            row['match_label'] = label
            row['match_explanation'] = explanation
            self._rows.append(row)

        layout = QtWidgets.QVBoxLayout(self)

        count = len(self._results)
        headline = QtWidgets.QLabel(
            f'{count} Discogs release(s) found — pick the pressing you have. '
            'Click a column header to sort; the Match column ranks how well '
            'each one agrees with the files.'
            if count else 'No matching releases found on Discogs.'
        )
        headline.setWordWrap(True)
        layout.addWidget(headline)

        filter_row = QtWidgets.QHBoxLayout()
        filter_row.addWidget(QtWidgets.QLabel('Filter:'))
        self.filter_field = QtWidgets.QLineEdit(self)
        self.filter_field.setPlaceholderText(
            'Narrow by anything shown — e.g. "japan", "vinyl", "remaster", a catalogue number')
        self.filter_field.setClearButtonEnabled(True)
        self.filter_field.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self.filter_field, 1)
        layout.addLayout(filter_row)

        self.table = QtWidgets.QTableWidget(len(self._rows), len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels([c[0] for c in COLUMNS])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)

        header = self.table.horizontalHeader()
        # Stops any column collapsing to an unreadable "P…" when the dialog
        # is narrow; the user can still drag the interactive ones wider.
        header.setMinimumSectionSize(60)
        for index, (_label, _key, _numeric, width, tooltip) in enumerate(COLUMNS):
            if width < 0:
                mode = QtWidgets.QHeaderView.ResizeMode.Stretch
            elif width == 0:
                mode = QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            else:
                mode = QtWidgets.QHeaderView.ResizeMode.Interactive
            header.setSectionResizeMode(index, mode)
            if width > 0:
                self.table.setColumnWidth(index, width)
            header_item = self.table.horizontalHeaderItem(index)
            if header_item is not None:
                header_item.setToolTip(tooltip)

        for row_index in range(len(self._rows)):
            self._populate_row(row_index)

        # Sorting is switched on only after the rows are in place: with it
        # enabled during population, each setItem() can re-sort and move the
        # row being filled, scattering cells across the table.
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, QtCore.Qt.SortOrder.DescendingOrder)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.doubleClicked.connect(self.accept)

        # A splitter rather than fixed proportions: how much room the table
        # needs depends on how many candidates came back, and how much the
        # detail pane needs depends on how fully catalogued the release is.
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical, self)
        splitter.addWidget(self.table)

        self.detail_pane = QtWidgets.QTextBrowser(splitter)
        self.detail_pane.setOpenExternalLinks(False)
        self.detail_pane.setMinimumHeight(150)
        splitter.addWidget(self.detail_pane)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setCollapsible(0, False)
        layout.addWidget(splitter, 1)

        self.status_label = QtWidgets.QLabel('', self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            QtCore.Qt.Orientation.Horizontal,
            self,
        )
        self.open_button = QtWidgets.QPushButton('Open on Discogs', self)
        self.open_button.setToolTip(
            'Open this release in your browser — the quickest way to check '
            'the sleeve images and track times against the disc in your hand.')
        self.open_button.clicked.connect(self._open_in_browser)
        self.button_box.addButton(self.open_button,
                                  QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)

        self.load_all_button = QtWidgets.QPushButton('Load all details', self)
        self.load_all_button.setToolTip(
            'Fetch track counts and company credits for every row. Costs one '
            'Discogs request per release, against a limit of 60 per minute.')
        self.load_all_button.clicked.connect(self._load_all_details)
        self.load_all_button.setEnabled(bool(self._rows) and detail_fetcher is not None)
        self.button_box.addButton(self.load_all_button,
                                  QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)

        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        if self._rows:
            self.table.selectRow(0)
        else:
            self._set_ok_enabled(False)
            self.open_button.setEnabled(False)
            self.detail_pane.setHtml(
                'Try fewer search fields — the album title alone often works '
                'best — or paste the release ID or URL from discogs.com.')

    # -- row rendering ----------------------------------------------------
    def _populate_row(self, row_index: int) -> None:
        """Write (or rewrite, after a detail fetch) one table row.

        Sorting is suspended for the duration: writing into a sorted table
        can move the row mid-update and leave cells on the wrong one.
        """
        row = self._rows[row_index]
        was_sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        try:
            for column, (_label, key, numeric, _width, _tooltip) in enumerate(COLUMNS):
                text, sort_key = self._cell_for(row, key, numeric)
                item = _SortableItem(text, sort_key)
                if numeric:
                    item.setTextAlignment(int(QtCore.Qt.AlignmentFlag.AlignRight
                                              | QtCore.Qt.AlignmentFlag.AlignVCenter))
                if column == 0:
                    # The row's identity has to travel with a cell, because
                    # sorting reorders rows and the view's row number stops
                    # matching the results list.
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, row_index)
                    if row.get('match_explanation'):
                        item.setToolTip(row['match_explanation'])
                    if row.get('is_master'):
                        item.setToolTip('Master release — pick a specific pressing instead.')
                        font = item.font()
                        font.setItalic(True)
                        item.setFont(font)
                self.table.setItem(row_index, column, item)
        finally:
            self.table.setSortingEnabled(was_sorting)

    def _cell_for(self, row: dict[str, Any], key: str, numeric: bool) -> tuple[str, Any]:
        if key == 'community':
            have, want = row.get('have') or 0, row.get('want') or 0
            return (f'{have} / {want}' if (have or want) else ''), have
        if key == 'score':
            # Shown as a word, sorted on the number behind it: "110" out of a
            # possible 130 is not something a user can act on, while "Exact"
            # with the matching facts in the tooltip is.
            return (row.get('match_label') or ''), row.get('score')
        if key == 'tracks':
            tracks = row.get('tracks')
            # An em-dash, not "0": the count is unknown until the per-release
            # fetch lands, and "0 tracks" would read as a wrong fact.
            return ('—' if tracks is None else str(tracks)), tracks
        if key == 'edition' and row.get('is_master'):
            return 'Master release', None
        value = row.get(key)
        if value in (None, ''):
            return '', None
        if numeric:
            try:
                return str(value), int(str(value))
            except (TypeError, ValueError):
                return str(value), None
        return str(value), str(value).casefold()

    # -- selection and filtering -----------------------------------------
    def _row_index_at(self, view_row: int) -> int | None:
        item = self.table.item(view_row, 0)
        if item is None:
            return None
        value = item.data(QtCore.Qt.ItemDataRole.UserRole)
        return value if isinstance(value, int) and 0 <= value < len(self._rows) else None

    def _current_row_index(self) -> int | None:
        view_row = self.table.currentRow()
        return self._row_index_at(view_row) if view_row >= 0 else None

    def _set_ok_enabled(self, enabled: bool) -> None:
        button = self.button_box.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        if button is not None:
            button.setEnabled(enabled)

    def _on_selection_changed(self) -> None:
        row_index = self._current_row_index()
        if row_index is None:
            self.detail_pane.setHtml('')
            self._set_ok_enabled(False)
            self.open_button.setEnabled(False)
            return
        row = self._rows[row_index]
        self.detail_pane.setHtml(_format_summary_text(row))
        self.open_button.setEnabled(bool(_discogs_url(row)))
        # A master release has no pressing-level data to apply, so selecting
        # one is a dead end — say why rather than letting it fail later.
        is_master = bool(row.get('is_master'))
        self._set_ok_enabled(not is_master)
        self.status_label.setText(
            'Master releases cannot be applied — open it on Discogs and pick '
            'the pressing you own.' if is_master else '')
        self._request_detail(row_index)

    def _apply_filter(self, text: str) -> None:
        """Hide rows that do not contain the filter text.

        Matching is over the row's values rather than the visible cells, so
        a barcode or genre that only shows in the detail pane still filters.
        """
        needle = (text or '').strip().casefold()
        visible_rows = 0
        for view_row in range(self.table.rowCount()):
            row_index = self._row_index_at(view_row)
            if row_index is None:
                continue
            if not needle:
                match = True
            else:
                haystack = ' '.join(
                    str(value) for key, value in self._rows[row_index].items()
                    if key not in ('thumb', 'cover_image', 'resource_url', 'uri')
                    and value not in (None, '')
                ).casefold()
                match = needle in haystack
            self.table.setRowHidden(view_row, not match)
            visible_rows += 1 if match else 0

        if needle:
            self.status_label.setText(
                f'{visible_rows} of {len(self._rows)} release(s) match "{text}".')
        else:
            self.status_label.setText('')

        # Selecting a hidden row would apply a release the user can no longer
        # see, so move to the first visible one.
        current = self.table.currentRow()
        if current < 0 or self.table.isRowHidden(current):
            for view_row in range(self.table.rowCount()):
                if not self.table.isRowHidden(view_row):
                    self.table.selectRow(view_row)
                    return
            self.table.clearSelection()
            self._set_ok_enabled(False)

    # -- lazy detail loading ---------------------------------------------
    def _request_detail(self, row_index: int) -> None:
        """Fetch the extras a search response omits, once per release."""
        if self._detail_fetcher is None:
            return
        row = self._rows[row_index]
        release_id = row.get('release_id')
        # Masters have a different response shape and no pressing data, so
        # there is nothing worth fetching for one.
        if not release_id or row.get('is_master') or release_id in self._detail_requested:
            return
        self._detail_requested.add(release_id)

        def _on_detail(release: Any) -> None:
            # The dialog may already be gone by the time a reply arrives;
            # touching a deleted Qt widget crashes rather than raising.
            try:
                if not self._is_alive():
                    return
                if not isinstance(release, dict):
                    return
                extra = summarize_release_detail(release)
                self._rows[row_index].update(
                    {k: v for k, v in extra.items() if v not in (None, '')})
                self._populate_row(row_index)
                if self._current_row_index() == row_index:
                    self.detail_pane.setHtml(_format_summary_text(self._rows[row_index]))
            except Exception:
                # Never let a callback escape into Qt: PyQt6 calls qFatal()
                # on an unhandled exception in a slot, aborting Picard.
                pass

        try:
            self._detail_fetcher(release_id, _on_detail)
        except Exception:
            pass

    def _is_alive(self) -> bool:
        """Has this dialog's C++ object outlived the Python wrapper?"""
        try:
            self.table.rowCount()
            return True
        except RuntimeError:
            return False

    def _load_all_details(self) -> None:
        pending = [i for i, row in enumerate(self._rows)
                   if row.get('tracks') is None and not row.get('is_master')
                   and row.get('release_id')
                   and row['release_id'] not in self._detail_requested]
        if not pending:
            self.status_label.setText('All details are already loaded.')
            return
        if len(pending) > BULK_CONFIRM_THRESHOLD:
            answer = QtWidgets.QMessageBox.question(
                self, 'Load all details',
                f'This will make {len(pending)} Discogs requests, against a '
                'limit of 60 per minute. Continue?',
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.Yes,
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        self.status_label.setText(
            f'Loading details for {len(pending)} release(s)…')
        for row_index in pending:
            self._request_detail(row_index)

    def _open_in_browser(self) -> None:
        row_index = self._current_row_index()
        if row_index is None:
            return
        url = _discogs_url(self._rows[row_index])
        if url:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

    # -- result -----------------------------------------------------------
    def accept(self) -> None:
        row_index = self._current_row_index()
        if row_index is None:
            return                      # nothing picked; keep the dialog open
        if self._rows[row_index].get('is_master'):
            return                      # explained in the status line already
        self.selected_result = self._results[row_index]
        super().accept()
