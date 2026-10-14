"""Disc-mapping dialog for multi-disc Discogs releases.

Picard resolves a box set into several separate albums/clusters, and the
order they appear in Picard's pane has nothing to do with the order the
discs appear on Discogs — a five-disc set can easily show as 1, 3, 2, 5, 4.
Applying disc numbers positionally would therefore tag the wrong discs.

This dialog lists the Discogs discs in their real order (1..N) and lets
you pick, for each one, which of your selected albums it corresponds to.
An initial guess is made by matching each Discogs disc title against the
album titles, so a well-tagged box usually needs no manual correction.
"""

from __future__ import annotations

import re

from PyQt6 import QtCore, QtGui, QtWidgets

_UNASSIGNED = '— not assigned —'


def _norm(text: str) -> str:
    return ''.join(ch for ch in (text or '').lower() if ch.isalnum())


def _label_track_count(label: str) -> int:
    """Pull the track count out of a group label like "... (6 tracks)"."""
    match = re.search(r'\((\d+)\s+(?:tracks|files|items)\)\s*$', label or '')
    return int(match.group(1)) if match else 0


# Trailing "(1975)" style year suffixes on Discogs disc titles, and any
# bracketed qualifier, are noise when comparing titles.
_YEAR_SUFFIX_RE = re.compile(r'\s*[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]\s*$')
_BRACKETED_RE = re.compile(r'[\(\[][^\)\]]*[\)\]]')
_STOPWORDS = {'the', 'a', 'an', 'and', 'of', 'de', 'di', 'il', 'la', 'le', 'los', 'e'}


def _tokens(text: str) -> set:
    """Comparable word tokens: lowercased, punctuation and stopwords dropped."""
    text = _YEAR_SUFFIX_RE.sub('', text or '')
    text = _BRACKETED_RE.sub(' ', text)
    words = re.split(r'[^0-9a-z]+', text.lower())
    return {w for w in words if w and w not in _STOPWORDS and len(w) > 1}


def _overlap(a: set, b: set) -> float:
    """Fraction of the smaller token set that the two share (0..1)."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


# Weights. Track titles are by far the most reliable signal — two discs of a
# box may share an album title and a disc number, but their track listings
# are unique. Track count is strong corroboration. The disc's own title is
# useful when the box names its discs, and useless when it doesn't.
_W_TRACK_TITLES = 60
_W_TRACK_COUNT = 25
_W_DISC_TITLE = 30
_W_POSITION = 4

# Above this, a row is shown as a confident match.
CONFIDENT_DISC_SCORE = 40


def score_disc_against_group(disc: dict, group: dict, disc_index: int, group_index: int) -> int:
    """How well one Discogs disc matches one of the user's groups."""
    score = 0.0

    disc_titles = set()
    for track in disc.get('tracks') or []:
        disc_titles |= _tokens(track.get('title', ''))
    group_titles = set()
    for title in group.get('titles') or []:
        group_titles |= _tokens(title)
    score += _W_TRACK_TITLES * _overlap(disc_titles, group_titles)

    want = len(disc.get('tracks') or [])
    have = group.get('count') or 0
    if want and have:
        if want == have:
            score += _W_TRACK_COUNT
        elif abs(want - have) == 1:
            score += _W_TRACK_COUNT * 0.4   # a bonus track or a missing rip

    score += _W_DISC_TITLE * _overlap(_tokens(disc.get('title', '')),
                                      _tokens(group.get('label', '')))

    if disc_index == group_index:
        score += _W_POSITION

    return int(round(score))


def auto_map(discs: list[dict], groups: list[dict]) -> list[tuple]:
    """Assign each Discogs disc to at most one group.

    Scores every disc/group pair, then takes the best pairings first so a
    strong match cannot be stolen by a weaker one earlier in the list —
    the failure mode of the previous sequential passes. Returns a list
    parallel to `discs` of (group_index, score); group_index is -1 when
    nothing is left to assign.
    """
    pairs = []
    for i, disc in enumerate(discs):
        for j, group in enumerate(groups):
            pairs.append((score_disc_against_group(disc, group, i, j), i, j))
    pairs.sort(key=lambda t: (-t[0], t[1], t[2]))

    result = [(-1, 0)] * len(discs)
    taken_discs, taken_groups = set(), set()
    for score, i, j in pairs:
        if i in taken_discs or j in taken_groups:
            continue
        result[i] = (j, score)
        taken_discs.add(i)
        taken_groups.add(j)
    return result


class DiscMappingDialog(QtWidgets.QDialog):
    """`mapping` holds disc-index -> album-index (or -1) after accept()."""

    def __init__(self, discs: list[dict], groups: list, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Match Discs to Albums')
        self.resize(720, 420)

        self._discs = discs
        # Accept either rich group dicts or plain label strings.
        self._groups = [g if isinstance(g, dict)
                        else {'label': g, 'count': _label_track_count(g), 'titles': []}
                        for g in groups]
        self._album_labels = [g['label'] for g in self._groups]
        self.mapping: list[int] = []

        layout = QtWidgets.QVBoxLayout(self)

        intro = QtWidgets.QLabel(
            f'{len(discs)} Discogs discs matched against {len(self._groups)} of your '
            'selections, using track titles, track counts and disc titles. '
            'Check the Match column and correct anything that looks wrong \u2014 '
            'unassigned rows are skipped.'
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QtWidgets.QTableWidget(len(discs), 4, self)
        self.table.setHorizontalHeaderLabels(('Discogs disc', 'Tracks', 'Your album', 'Match'))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)

        initial_pairs = auto_map(discs, self._groups)
        initial = [index for index, _score in initial_pairs]
        self._combos: list[QtWidgets.QComboBox] = []
        for row, disc in enumerate(discs):
            title = disc.get('title') or ''
            label = f"Disc {disc.get('number', row + 1)}"
            if title:
                label += f' — {title}'
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(label))
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(len(disc.get('tracks', [])))))

            combo = QtWidgets.QComboBox(self)
            combo.addItem(_UNASSIGNED, -1)
            for index, album_label in enumerate(self._album_labels):
                combo.addItem(album_label, index)
            chosen = initial[row] if row < len(initial) else -1
            combo.setCurrentIndex(chosen + 1 if chosen >= 0 else 0)
            self._combos.append(combo)
            self.table.setCellWidget(row, 2, combo)

            score = initial_pairs[row][1] if row < len(initial_pairs) else 0
            if chosen < 0:
                verdict = 'no match'
            elif score >= CONFIDENT_DISC_SCORE:
                verdict = f'confident ({score})'
            else:
                verdict = f'check this ({score})'
            item = QtWidgets.QTableWidgetItem(verdict)
            if chosen >= 0 and score < CONFIDENT_DISC_SCORE:
                item.setForeground(QtGui.QBrush(QtGui.QColor('#b06000')))
            self.table.setItem(row, 3, item)

        self.table.resizeRowsToContents()
        layout.addWidget(self.table)

        self.warning_label = QtWidgets.QLabel('')
        self.warning_label.setWordWrap(True)
        layout.addWidget(self.warning_label)
        for combo in self._combos:
            combo.currentIndexChanged.connect(self._check_duplicates)
        self._check_duplicates()

        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            QtCore.Qt.Orientation.Horizontal,
            self,
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _check_duplicates(self) -> None:
        chosen = [c.currentData() for c in self._combos if c.currentData() >= 0]
        duplicates = {v for v in chosen if chosen.count(v) > 1}
        if duplicates:
            names = ', '.join(self._album_labels[v] for v in sorted(duplicates))
            self.warning_label.setText(
                f'Warning: the same album is assigned to more than one disc ({names}). '
                'It will receive the last disc\'s numbering.'
            )
        else:
            self.warning_label.setText('')

    def accept(self) -> None:
        self.mapping = [c.currentData() for c in self._combos]
        super().accept()
