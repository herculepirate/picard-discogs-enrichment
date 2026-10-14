"""Multi-field Discogs search dialog.

Rather than one free-text box, this exposes the fields Discogs'
/database/search endpoint really supports, so a search can be narrowed the
way the catalogue is actually organised. All fields are optional and are
combined into a single query — fill in as many or as few as you know.

The fields are grouped by how they are used in practice. The ones that
identify a specific pressing (catalogue number, barcode) come first, because
a single catalogue number usually settles a search outright; the descriptive
ones (genre, style, credit, track) are for the case where the album title is
wrong, missing, or too generic to search on.

Note: Discogs has no distinct "disc ID" search field. If that's what you're
after, use Catalogue Number instead — it's the closest real equivalent and
something this plugin already extracts from releases.
"""

from __future__ import annotations

from PyQt6 import QtCore, QtWidgets

# Format values Discogs recognises, as the `format` search parameter. Offered
# as a combo box because Discogs matches these as free text: "Vinyl" works,
# "vinyl record" returns nothing, and that failure looks like "no results"
# rather than a bad search term.
FORMAT_CHOICES = (
    '', 'Album', 'CD', 'Vinyl', 'LP', '12"', '7"', 'File', 'FLAC', 'MP3',
    'Cassette', 'Box Set', 'Compilation', 'EP', 'Single', 'SACD', 'DVD',
    'Blu-ray', 'Reissue', 'Remastered', 'Deluxe Edition', 'Limited Edition',
    'Promo', 'Mono', 'Stereo', 'Shellac', 'HDCD', 'SHM-CD',
)

TYPE_RELEASE = 'release'
TYPE_MASTER = 'master'


class DiscogsSearchDialog(QtWidgets.QDialog):
    """Collects search terms; `values` holds the result after accept()."""

    def __init__(self, defaults: dict, parent=None, context_label: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Search Discogs')
        self.resize(640, 660)

        self.values: dict = {}

        outer = QtWidgets.QVBoxLayout(self)

        if context_label:
            context_widget = QtWidgets.QLabel(context_label)
            context_widget.setWordWrap(True)
            outer.addWidget(context_widget)

        # The form can outgrow a short screen once every group is expanded,
        # and an unreachable OK button is the one failure that makes the
        # dialog useless, so the body scrolls and the buttons stay put.
        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        # AsNeeded, never AlwaysOff: with the body's minimum width larger than
        # the viewport — which happens as soon as the window is narrowed —
        # AlwaysOff clips the right-hand side of every field with no way to
        # reach it.
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        body = QtWidgets.QWidget(scroll)
        layout = QtWidgets.QVBoxLayout(body)

        # -- direct ID / URL ------------------------------------------------
        direct_group = QtWidgets.QGroupBox('Go straight to one release', body)
        direct_layout = QtWidgets.QVBoxLayout(direct_group)
        direct_label = QtWidgets.QLabel(
            'Paste a Discogs release ID or URL to skip searching entirely '
            '(e.g. R1234567, [r1234567], 1234567, or a discogs.com/release/… link):'
        )
        direct_label.setWordWrap(True)
        direct_layout.addWidget(direct_label)
        self.direct_field = QtWidgets.QLineEdit(direct_group)
        self.direct_field.setText(defaults.get('direct', ''))
        self.direct_field.setClearButtonEnabled(True)
        direct_layout.addWidget(self.direct_field)
        if defaults.get('direct'):
            linked_note = QtWidgets.QLabel(
                'Pre-filled from the Discogs link recorded on the MusicBrainz '
                'release, so this is the same pressing Picard matched. Clear it '
                'to search instead.'
            )
            linked_note.setWordWrap(True)
            direct_layout.addWidget(linked_note)
        layout.addWidget(direct_group)

        # -- identifiers ---------------------------------------------------
        id_group = QtWidgets.QGroupBox('Identifiers — the fastest way to the right pressing', body)
        id_form = QtWidgets.QFormLayout(id_group)
        self.barcode_field = self._line(id_group, defaults.get('barcode', ''),
                                        'Digits from the back of the case; spaces and dashes are fine')
        id_form.addRow('Barcode:', self.barcode_field)
        self.catno_field = self._line(id_group, defaults.get('catalog_number', ''),
                                      'e.g. UICY-77821, 7243 8 50999 2 4')
        id_form.addRow('Catalogue number:', self.catno_field)
        layout.addWidget(id_group)

        # -- the album -----------------------------------------------------
        album_group = QtWidgets.QGroupBox('The album', body)
        album_form = QtWidgets.QFormLayout(album_group)
        self.artist_field = self._line(album_group, defaults.get('artist', ''))
        album_form.addRow('Artist:', self.artist_field)
        self.title_field = self._line(album_group, defaults.get('release_title', ''))
        album_form.addRow('Album / release title:', self.title_field)
        self.year_field = self._line(album_group, defaults.get('year', ''),
                                     'Year of this pressing, not of the original release')
        album_form.addRow('Year:', self.year_field)
        self.label_field = self._line(album_group, defaults.get('label', ''),
                                      'Record label, e.g. Parlophone, Blue Note')
        album_form.addRow('Label:', self.label_field)
        self.country_field = self._line(album_group, defaults.get('country', ''),
                                        'e.g. Japan, UK, US, Europe')
        album_form.addRow('Country:', self.country_field)
        layout.addWidget(album_group)

        # -- format --------------------------------------------------------
        format_group = QtWidgets.QGroupBox('Format', body)
        format_form = QtWidgets.QFormLayout(format_group)
        self.format_combo = QtWidgets.QComboBox(format_group)
        self.format_combo.setEditable(True)
        self.format_combo.addItems(FORMAT_CHOICES)
        self.format_combo.setToolTip(
            'Narrows to one medium or edition. Pick from the list or type your '
            'own — Discogs matches these as text, so "Vinyl" finds records '
            'but "vinyl record" finds nothing.')
        default_format = defaults.get('format', '')
        if default_format:
            self.format_combo.setEditText(default_format)
        else:
            self.format_combo.setCurrentIndex(0)
        format_form.addRow('Format:', self.format_combo)
        layout.addWidget(format_group)

        # -- descriptive ---------------------------------------------------
        extra_group = QtWidgets.QGroupBox(
            'When you do not know the title — search by what is on the record', body)
        extra_form = QtWidgets.QFormLayout(extra_group)
        self.track_field = self._line(extra_group, defaults.get('track', ''),
                                      'A track title from the release')
        extra_form.addRow('Track title:', self.track_field)
        self.credit_field = self._line(extra_group, defaults.get('credit', ''),
                                       'Anyone credited: producer, engineer, session player')
        extra_form.addRow('Credited person:', self.credit_field)
        self.genre_field = self._line(extra_group, defaults.get('genre', ''),
                                      'Discogs genre, e.g. Electronic, Jazz, Rock')
        extra_form.addRow('Genre:', self.genre_field)
        self.style_field = self._line(extra_group, defaults.get('style', ''),
                                      'Discogs style, e.g. Synth-pop, Hard Bop')
        extra_form.addRow('Style:', self.style_field)
        self.query_field = self._line(extra_group, defaults.get('query', ''),
                                      'Searches everything at once — use when nothing else fits')
        extra_form.addRow('Free text:', self.query_field)
        layout.addWidget(extra_group)

        # -- result scope --------------------------------------------------
        scope_group = QtWidgets.QGroupBox('What to return', body)
        scope_layout = QtWidgets.QVBoxLayout(scope_group)
        self.releases_radio = QtWidgets.QRadioButton(
            'Specific pressings (recommended — these carry barcode, '
            'catalogue number, country and credits)', scope_group)
        self.masters_radio = QtWidgets.QRadioButton(
            'One entry per album, to see which pressings exist — cannot be '
            'applied directly', scope_group)
        self.releases_radio.setChecked(defaults.get('release_type', TYPE_RELEASE) != TYPE_MASTER)
        self.masters_radio.setChecked(not self.releases_radio.isChecked())
        scope_layout.addWidget(self.releases_radio)
        scope_layout.addWidget(self.masters_radio)
        layout.addWidget(scope_group)

        layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        self.hint_label = QtWidgets.QLabel(
            'Fewer fields usually find more. Discogs treats every field as a '
            'requirement, so one wrong year or country returns nothing at all — '
            'start with the barcode or catalogue number alone, and add terms '
            'only if there are too many results.', self)
        self.hint_label.setWordWrap(True)
        outer.addWidget(self.hint_label)

        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            QtCore.Qt.Orientation.Horizontal,
            self,
        )
        reset_button = QtWidgets.QPushButton('Clear all', self)
        reset_button.clicked.connect(self._clear_all)
        button_box.addButton(reset_button, QtWidgets.QDialogButtonBox.ButtonRole.ResetRole)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        outer.addWidget(button_box)

    @staticmethod
    def _line(parent, text: str, placeholder: str = '') -> QtWidgets.QLineEdit:
        field = QtWidgets.QLineEdit(parent)
        field.setText(text or '')
        field.setClearButtonEnabled(True)
        if placeholder:
            field.setPlaceholderText(placeholder)
            field.setToolTip(placeholder)
        return field

    def _text_fields(self) -> dict:
        """Map each `values` key to the widget holding it.

        One place defines the mapping, so accept() and _clear_all() cannot
        drift apart and quietly leave a field out of the search.
        """
        return {
            'direct': self.direct_field,
            'artist': self.artist_field,
            'release_title': self.title_field,
            'year': self.year_field,
            'catalog_number': self.catno_field,
            'barcode': self.barcode_field,
            'label': self.label_field,
            'country': self.country_field,
            'track': self.track_field,
            'credit': self.credit_field,
            'genre': self.genre_field,
            'style': self.style_field,
            'query': self.query_field,
        }

    def _clear_all(self) -> None:
        for field in self._text_fields().values():
            field.clear()
        self.format_combo.setCurrentIndex(0)
        self.format_combo.setEditText('')

    def accept(self) -> None:
        self.values = {name: field.text().strip()
                       for name, field in self._text_fields().items()}
        self.values['format'] = self.format_combo.currentText().strip()
        self.values['release_type'] = (
            TYPE_MASTER if self.masters_radio.isChecked() else TYPE_RELEASE)
        super().accept()
