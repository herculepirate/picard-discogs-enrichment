#!/usr/bin/env bash
#
# Install, update or repair Discogs Enrichment in MusicBrainz Picard 3.
#
# Everything goes through Picard's own plugin manager (picard-cli), so Picard
# keeps a git checkout of the plugin that its Update button can fast-forward.
# Never copy or edit files inside Picard's plugins3 folder by hand: Picard
# refuses to update a checkout with local edits ("has uncommitted changes").
#
# Usage:
#   ./install.sh                  # install from GitHub (normal use)
#   ./install.sh --local          # install from this folder's git repo (development:
#                                 #   commit here, then ./install.sh --update)
#   ./install.sh --update         # update to the latest commit of the install source
#   ./install.sh --repair         # back up hand edits in the installed copy, discard them, update
#   ./install.sh --status         # show the installed copy's state
#   ./install.sh --uninstall
#   ./install.sh --flavour deb|flatpak   # pick a Picard install when several exist
#
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_URL="https://github.com/herculepirate/picard-discogs-enrichment"

die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }

manifest_value() {
    sed -n "s/^$1[[:space:]]*=[[:space:]]*\"\(.*\)\"[[:space:]]*\$/\1/p" "$SRC_DIR/MANIFEST.toml" | head -1
}

[ -f "$SRC_DIR/MANIFEST.toml" ] || die "MANIFEST.toml not found next to this script; run it from the plugin folder."
UUID="$(manifest_value uuid)"
NAME="$(manifest_value name)"
[ -n "$UUID" ] && [ -n "$NAME" ] || die "could not read name/uuid from MANIFEST.toml"

# Picard names the folder "<sanitized name>_<uuid>" (picard/plugin3/manager/install.py).
plugin_dir_name() {
    local n
    n=$(printf '%s' "$NAME" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]\{1,\}/_/g; s/^_//; s/_$//')
    printf '%s_%s' "${n:0:50}" "$UUID"
}

plugins_root_for() {
    case "$1" in
        deb)     printf '%s/.local/share/MusicBrainz/Picard/plugins3' "$HOME" ;;
        flatpak) printf '%s/.var/app/org.musicbrainz.Picard/data/MusicBrainz/Picard/plugins3' "$HOME" ;;
        *)       return 1 ;;
    esac
}

detect_flavours() {
    command -v picard-cli >/dev/null 2>&1 && echo deb
    if command -v flatpak >/dev/null 2>&1 && flatpak list --app 2>/dev/null | grep -qi 'org\.musicbrainz\.Picard'; then
        echo flatpak
    fi
    return 0
}

MODE=install
FLAVOUR=""
while [ $# -gt 0 ]; do
    case "$1" in
        --local)      MODE=local; shift ;;
        --update)     MODE=update; shift ;;
        --repair)     MODE=repair; shift ;;
        --status)     MODE=status; shift ;;
        --uninstall)  MODE=uninstall; shift ;;
        --flavour|-f) FLAVOUR="${2:-}"; shift 2 ;;
        -h|--help)    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)            die "unknown option: $1 (try --help)" ;;
    esac
done

if [ -z "$FLAVOUR" ]; then
    mapfile -t FOUND < <(detect_flavours)
    case "${#FOUND[@]}" in
        0) die "No Picard 3 found (looked for picard-cli and the Flatpak). A Snap install: use Options > Plugins > Install Plugin… with $DEFAULT_URL" ;;
        1) FLAVOUR="${FOUND[0]}" ;;
        *) die "Several Picard installs found (${FOUND[*]}). Choose one with --flavour deb|flatpak." ;;
    esac
fi
case "$FLAVOUR" in
    deb)     CLI=(picard-cli) ;;
    flatpak) CLI=(flatpak run --command=picard-cli org.musicbrainz.Picard) ;;
    *)       die "unknown flavour: $FLAVOUR (expected deb or flatpak)" ;;
esac
DEST="$(plugins_root_for "$FLAVOUR")/$(plugin_dir_name)"

picard_running() { pgrep -x picard >/dev/null 2>&1 || pgrep -f 'org.musicbrainz.Picard' >/dev/null 2>&1; }
warn_running() {
    if picard_running; then
        info "Note: Picard seems to be running. Restart it afterwards to load the change."
    fi
}

is_git() { [ -d "$DEST/.git" ]; }

dirty_files() { git -C "$DEST" status --porcelain --untracked-files=normal 2>/dev/null || true; }

backup_edits() {
    local changes stamp out
    changes="$(dirty_files)"
    [ -n "$changes" ] || return 0
    stamp="$(date +%Y%m%d-%H%M%S)"
    out="$HOME/discogs-enrichment-local-edits-$stamp"
    mkdir -p "$out"
    git -C "$DEST" diff > "$out/changes.diff" || true
    while IFS= read -r line; do
        f="${line:3}"
        [ -e "$DEST/$f" ] && mkdir -p "$out/$(dirname "$f")" && cp -a -- "$DEST/$f" "$out/$f"
    done <<< "$changes"
    info "Backed up the hand edits to $out (changes.diff + the edited files)."
}

case "$MODE" in
    status)
        info "Picard ($FLAVOUR) plugin folder: $DEST"
        if [ ! -d "$DEST" ]; then info "Not installed."; exit 0; fi
        if ! is_git; then
            info "Installed as a plain copy (not managed by Picard's updater). Fix: ./install.sh --repair"
            exit 0
        fi
        git -C "$DEST" fetch -q origin 2>/dev/null || true
        info "Installed commit: $(git -C "$DEST" log -1 --format='%h %s')"
        info "Source:           $(git -C "$DEST" remote get-url origin 2>/dev/null || echo '?')"
        behind="$(git -C "$DEST" rev-list --count HEAD..@{u} 2>/dev/null || echo '?')"
        info "Commits behind:   $behind"
        if [ -n "$(dirty_files)" ]; then
            info "Hand edits (these block Picard's Update):"
            dirty_files | sed 's/^/  /'
            info "Fix: ./install.sh --repair"
        else
            info "No hand edits: Picard can update it."
        fi
        ;;

    install|local)
        if [ "$MODE" = local ]; then
            git -C "$SRC_DIR" rev-parse --git-dir >/dev/null 2>&1 || die "$SRC_DIR is not a git repository"
            if [ -n "$(git -C "$SRC_DIR" status --porcelain --untracked-files=no)" ]; then
                info "Note: uncommitted changes here are NOT installed; Picard installs committed work only."
            fi
            SOURCE="$SRC_DIR"
            if [ "$FLAVOUR" = flatpak ]; then
                # the Flatpak sandbox must be allowed to read this folder
                flatpak override --user --filesystem="$SRC_DIR:ro" org.musicbrainz.Picard
            fi
        else
            SOURCE="$(git -C "$SRC_DIR" remote get-url origin 2>/dev/null || echo "$DEFAULT_URL")"
            case "$SOURCE" in git@github.com:*) SOURCE="https://github.com/${SOURCE#git@github.com:}" ;; esac
        fi
        if [ -d "$DEST" ] && ! is_git; then
            info "Found a plain copy from an older install.sh; replacing it with a Picard-managed install."
            rm -rf -- "$DEST"
        fi
        if is_git; then
            current="$(git -C "$DEST" remote get-url origin 2>/dev/null || true)"
            if [ "$current" = "$SOURCE" ]; then
                info "Already installed from $SOURCE; updating instead."
                exec "$0" --update --flavour "$FLAVOUR"
            fi
            info "Switching the install source from $current to $SOURCE (settings are kept)."
            backup_edits
            "${CLI[@]}" plugins install --reinstall "$SOURCE"
        else
            "${CLI[@]}" plugins install "$SOURCE"
        fi
        warn_running
        info "Then: Options > Plugins, enable \"$NAME\", and paste your Discogs token in its settings."
        ;;

    update)
        is_git || die "Not installed through Picard (run ./install.sh first, or ./install.sh --repair)."
        if [ -n "$(dirty_files)" ]; then
            info "The installed copy has hand edits, which block updates:"
            dirty_files | sed 's/^/  /'
            die "Run ./install.sh --repair (keeps a backup of the edits)."
        fi
        "${CLI[@]}" plugins update "$UUID"
        warn_running
        ;;

    repair)
        if [ ! -d "$DEST" ]; then die "Not installed; run ./install.sh"; fi
        if ! is_git; then
            info "Replacing a plain copy with a Picard-managed install."
            exec "$0" --flavour "$FLAVOUR"
        fi
        backup_edits
        git -C "$DEST" checkout -- .
        git -C "$DEST" clean -fdq   # untracked files only; ignored files (__pycache__) stay
        info "Installed copy is clean again."
        "${CLI[@]}" plugins update "$UUID"
        warn_running
        ;;

    uninstall)
        "${CLI[@]}" plugins remove "$UUID"
        warn_running
        ;;
esac
