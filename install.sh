#!/usr/bin/env bash
#
# Install Discogs Enrichment into MusicBrainz Picard 3.
#
# Picard 3 discovers plugins by scanning its plugins3 directory for any
# folder containing a MANIFEST.toml, so installing is a directory copy. The
# only thing that differs between a .deb, a Flatpak and a Snap is where that
# directory lives, because each confines Picard's data directory differently.
#
# Usage:
#   ./install.sh                 # auto-detect; asks if several are installed
#   ./install.sh --flavour deb   # force one of: deb | flatpak | snap | custom
#   ./install.sh --dir /path/to/plugins3
#   ./install.sh --uninstall
#   ./install.sh --list          # show detected Picard installs and exit
#
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Picard names a plugin's directory "<sanitized name>_<uuid>", derived from
# MANIFEST.toml (see get_plugin_directory_name() in
# picard/plugin3/manager/install.py). Using the same name matters: install
# this under a different name and a later install from the registry creates
# a SECOND directory with the same uuid, and both load.
plugin_dir_name() {
    local name uuid
    name=$(sed -n 's/^name[[:space:]]*=[[:space:]]*"\(.*\)"[[:space:]]*$/\1/p' "$SRC_DIR/MANIFEST.toml" | head -1)
    uuid=$(sed -n 's/^uuid[[:space:]]*=[[:space:]]*"\(.*\)"[[:space:]]*$/\1/p' "$SRC_DIR/MANIFEST.toml" | head -1)
    [ -n "$name" ] && [ -n "$uuid" ] || return 1
    name=$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]\{1,\}/_/g; s/^_//; s/_$//')
    printf '%s_%s' "${name:0:50}" "$uuid"
}

die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }

# Picard resolves its plugin folder from Qt's AppDataLocation, with
# PICARD_PLUGIN_DIR overriding it. These are what that resolves to for each
# packaging format on Linux.
plugin_dir_for() {
    case "$1" in
        deb)     printf '%s/.local/share/MusicBrainz/Picard/plugins3' "$HOME" ;;
        flatpak) printf '%s/.var/app/org.musicbrainz.Picard/data/MusicBrainz/Picard/plugins3' "$HOME" ;;
        # A snap confines HOME to ~/snap/<name>/current, so Picard's own
        # idea of $HOME is not the user's.
        snap)    printf '%s/snap/picard/current/.local/share/MusicBrainz/Picard/plugins3' "$HOME" ;;
        *)       return 1 ;;
    esac
}

detect_flavours() {
    local found=()
    command -v picard >/dev/null 2>&1 && found+=(deb)
    if command -v flatpak >/dev/null 2>&1 \
       && flatpak list --app 2>/dev/null | grep -qi 'org\.musicbrainz\.Picard'; then
        found+=(flatpak)
    fi
    if command -v snap >/dev/null 2>&1 \
       && snap list 2>/dev/null | awk '{print $1}' | grep -qx 'picard'; then
        found+=(snap)
    fi
    # Printing "${found[@]:-}" when the array is empty emits one blank line,
    # which mapfile then reads as a single empty element — making a
    # zero-result detection look like one result.
    [ "${#found[@]}" -gt 0 ] && printf '%s\n' "${found[@]}"
    return 0
}

FLAVOUR=""
TARGET_DIR=""
UNINSTALL=0
LIST_ONLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --flavour|-f) FLAVOUR="${2:-}"; shift 2 ;;
        --dir|-d)     TARGET_DIR="${2:-}"; shift 2 ;;
        --uninstall)  UNINSTALL=1; shift ;;
        --list)       LIST_ONLY=1; shift ;;
        -h|--help)    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)            die "unknown option: $1 (try --help)" ;;
    esac
done

mapfile -t DETECTED < <(detect_flavours)

if [ "$LIST_ONLY" -eq 1 ]; then
    if [ "${#DETECTED[@]}" -eq 0 ]; then
        info "No Picard installation detected."
    else
        info "Detected Picard installations:"
        for f in "${DETECTED[@]}"; do
            [ -n "$f" ] || continue
            printf '  %-8s -> %s\n' "$f" "$(plugin_dir_for "$f")"
        done
    fi
    exit 0
fi

if [ -z "$TARGET_DIR" ]; then
    if [ -z "$FLAVOUR" ]; then
        case "${#DETECTED[@]}" in
            0) die "No Picard installation found. Pass --flavour deb|flatpak|snap or --dir <plugins3 path>." ;;
            1) FLAVOUR="${DETECTED[0]}" ;;
            *)
                info "Several Picard installations found: ${DETECTED[*]}"
                info "Pick one with --flavour, or install into all of them one at a time."
                exit 1
                ;;
        esac
    fi
    TARGET_DIR="$(plugin_dir_for "$FLAVOUR")" \
        || die "unknown flavour: $FLAVOUR (expected deb, flatpak or snap)"
fi

PLUGIN_NAME="$(plugin_dir_name)" \
    || die "could not read name/uuid from MANIFEST.toml"
DEST="$TARGET_DIR/$PLUGIN_NAME"

if [ "$UNINSTALL" -eq 1 ]; then
    [ -d "$DEST" ] || { info "Not installed at $DEST — nothing to do."; exit 0; }
    rm -rf -- "$DEST"
    info "Removed $DEST"
    info "Restart Picard for the change to take effect."
    exit 0
fi

[ -f "$SRC_DIR/MANIFEST.toml" ] \
    || die "MANIFEST.toml not found next to this script — run it from the plugin folder."

mkdir -p -- "$TARGET_DIR"

# Replace rather than merge: a stale .py left behind by an older version
# would still be imported and could shadow a module that has since moved.
rm -rf -- "$DEST"
mkdir -p -- "$DEST"

for item in MANIFEST.toml *.py README.md LICENSE; do
    [ -e "$SRC_DIR/$item" ] && cp -R -- "$SRC_DIR/$item" "$DEST/"
done

info "Installed to $DEST"
info ""
info "Next:"
info "  1. Start Picard (restart it if it is already running)."
info "  2. Options -> Plugins, enable \"Discogs Enrichment\"."
info "  3. Options -> Plugins -> Discogs Enrichment, paste your Discogs token."
info ""
info "Get a token at https://www.discogs.com/settings/developers (Generate token)."
