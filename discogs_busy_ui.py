"""What shows that Discogs Enrichment is still working (discogs_activity):

- a busy bar in Picard's status bar, beside its own counters: a moving bar and "Discogs: 2
  albums" while any album is worked on, hidden when none is; its tooltip lists each album and
  what is being done ("asking Discogs", "reading your scans (OCR)" ...);
- a marker after the album's title in the album list: "⏳ Discogs…" while it is worked on,
  "⚠ choose the Discogs pressing" while the picker waits for you.

The marker is added by wrapping Album.column ("title"), which Picard's album rows read for their
text; install() wraps it, uninstall() puts it back.
"""
from __future__ import annotations

from PyQt6 import QtCore, QtWidgets

from . import discogs_activity as activity
from .discogs_runtime import log

BUSY_MARK = '⏳ Discogs…'
WAIT_MARK = '⚠ choose the Discogs pressing'
PRUNE_MS = 5000

_STATE = {'widget': None, 'timer': None, 'column': None, 'shown': {}, 'queued': False, 'alive': None,
          'window': None}


def mark_for(album) -> str:
    st = activity.state_of(album)
    return BUSY_MARK if st == 'busy' else WAIT_MARK if st == 'waiting' else ''


class BusyIndicator(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(6)
        self.bar = QtWidgets.QProgressBar(self)
        self.bar.setRange(0, 0)                 # (no end known: the bar moves to and fro)
        self.bar.setTextVisible(False)
        self.bar.setFixedSize(60, 10)
        self.label = QtWidgets.QLabel(self)
        lay.addWidget(self.bar)
        lay.addWidget(self.label)
        self.hide()

    def show_state(self, busy: int, waiting: int, tip: str) -> None:
        if not busy and not waiting:
            self.hide()
            return
        parts = []
        if busy:
            parts.append(f'{busy} album{"s" if busy != 1 else ""}')
        if waiting:
            parts.append(f'{waiting} waiting for you')
        self.label.setText('Discogs: ' + ', '.join(parts))
        self.bar.setVisible(bool(busy))
        self.setToolTip('Discogs Enrichment is working on:\n' + tip if tip else '')
        self.show()

    def mousePressEvent(self, event):           # noqa: N802 (Qt)
        QtWidgets.QToolTip.showText(event.globalPosition().toPoint(), self.toolTip(), self)


def _refresh() -> None:
    """On the main thread: the bar, and the rows whose marker changed."""
    _STATE['queued'] = False
    widget = _STATE['widget']
    try:
        busy, waiting, tip = activity.summary()
        if widget is not None:
            widget.show_state(busy, waiting, tip)
    except Exception:
        log.debug('Discogs Enrichment: busy bar update failed', exc_info=True)
    now = {id(a): (a, mark_for(a)) for a, _t, _s, _w in activity.snapshot()}
    shown = _STATE['shown']
    for key in set(shown) | set(now):
        album, mark = now.get(key) or (shown[key][0], '')
        if shown.get(key, (None, ''))[1] == mark:
            continue
        _redraw(album)
    _STATE['shown'] = {k: v for k, v in now.items() if v[1]}


def _redraw(album) -> None:
    """The album's row text again (not its tracks)."""
    try:
        item = getattr(album, 'ui_item', None)
        if item is None:
            return
        redraw = getattr(item, 'update_colums_text', None)
        if redraw is not None:
            redraw()
        else:
            album.update(update_tracks=False)
    except Exception:
        pass


def _changed() -> None:
    """Any thread: one refresh queued on the main thread, however many changes come first."""
    if _STATE['queued']:
        return
    _STATE['queued'] = True
    try:
        from picard.util.thread import to_main
        to_main(_refresh)
    except Exception:
        _STATE['queued'] = False


def _prune() -> None:
    try:
        activity.prune(_STATE['alive'])
    except Exception:
        pass


def install(window, alive=None) -> None:
    """Add the busy bar to WINDOW's status bar and the marker to the album rows."""
    if _STATE['widget'] is not None:
        return
    _STATE['alive'] = alive
    if _STATE['column'] is None:
        _wrap_column()
    if window is not None:
        try:
            widget = BusyIndicator(window)
            window.statusBar().insertPermanentWidget(0, widget)
            _STATE['widget'] = widget
            _STATE['window'] = window
        except Exception:
            log.debug('Discogs Enrichment: busy bar not installed', exc_info=True)
    if _STATE['timer'] is None:
        timer = QtCore.QTimer()
        timer.setInterval(PRUNE_MS)
        timer.timeout.connect(_prune)
        timer.start()
        _STATE['timer'] = timer
    if _changed not in activity.LISTENERS:
        activity.LISTENERS.append(_changed)
    _changed()


def _wrap_column() -> None:
    try:
        from picard.album import Album
        original = Album.column

        def column(self, key, *args, **kwargs):
            text = original(self, key, *args, **kwargs)
            if key == 'title':
                mark = mark_for(self)
                if mark:
                    return f'{text}  {mark}'
            return text
        column._discogs_original = original
        Album.column = column
        _STATE['column'] = original
    except Exception:
        log.debug('Discogs Enrichment: album row marker not installed', exc_info=True)


def uninstall() -> None:
    if _changed in activity.LISTENERS:
        activity.LISTENERS.remove(_changed)
    if _STATE['timer'] is not None:
        _STATE['timer'].stop()
        _STATE['timer'] = None
    if _STATE['column'] is not None:
        try:
            from picard.album import Album
            Album.column = _STATE['column']
        except Exception:
            pass
        _STATE['column'] = None
    widget = _STATE['widget']
    if widget is not None:
        try:
            _STATE['window'].statusBar().removeWidget(widget)
        except Exception:
            pass
        widget.deleteLater()
        _STATE['widget'] = None
    for album, _mark in _STATE['shown'].values():
        _redraw(album)
    _STATE['shown'] = {}
