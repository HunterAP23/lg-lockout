#!/bin/sh
# install.sh  (init.d edition)
#
# Install the LG lockout on a rooted webOS TV. Run ON THE TV as root from inside
# this directory:
#   sh install.sh            # dry-run (default)
#   sh install.sh --apply    # actually install (requires root)
#
# Installs the boot hooks (executable, no dot in name, so run-parts runs them):
#   /var/lib/webosbrew/init.d/10-block-lg-telemetry   hosts blocklist bind-mount
#   /var/lib/webosbrew/init.d/20-decline-lg-tos       consent + option/general via luna-send
#   /var/lib/webosbrew/init.d/30-clear-lg-app-update  app-update gate wipe
# and the manual verifier (NOT a boot hook):
#   /var/lib/webosbrew/lg-lockout/verify-lockout
#
# Also enables the built-in Homebrew Channel blocker:
#   /var/luna/preferences/webosbrew_block_updates
#
# NEVER touches startup.sh or jumpstart.sh. NEVER rm -rf.
# Testing: LG_LOCKOUT_ROOT=/path installs into a copy.

set -u

ROOT="${LG_LOCKOUT_ROOT:-}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
HB="$ROOT/var/lib/webosbrew"
INITD="$HB/init.d"
DEST="$HB/lg-lockout"
FLAG="$ROOT/var/luna/preferences/webosbrew_block_updates"

INITD_FILES="10-block-lg-telemetry 20-decline-lg-tos 30-clear-lg-app-update"
DEST_FILES="verify-lockout scan-lg-owners"

MODE=dry
case "${1:-}" in
    --apply) MODE=apply ;;
    ""|--dry-run) MODE=dry ;;
    -h|--help)
        echo "usage: sh install.sh [--apply|--dry-run]"
        echo "  --apply   actually install (requires root); default is dry-run"
        exit 0 ;;
    *) echo "install.sh: unknown argument '$1'" >&2; exit 2 ;;
esac

log() { echo "[install] $*" >&2; }

if [ "$MODE" = "apply" ] && [ "$(id -u 2>/dev/null)" != "0" ]; then
    log "ERROR: --apply must run as root"; exit 1
fi
if [ ! -d "$HB" ]; then
    log "ERROR: $HB not found -- TV not rooted / Homebrew Channel missing"; exit 1
fi
for f in $INITD_FILES $DEST_FILES; do
    [ -f "$SRC_DIR/$f" ] || { log "ERROR: $SRC_DIR/$f missing -- run from the lockout dir"; exit 1; }
done

[ "$MODE" = "dry" ] && log "DRY-RUN -- nothing written. Re-run with --apply."
[ -n "$ROOT" ] && log "using LG_LOCKOUT_ROOT=$ROOT"

# 1. built-in blocker flag
if [ -f "$FLAG" ]; then
    log "ok: $FLAG already present"
elif [ "$MODE" = "apply" ]; then
    mkdir -p "$(dirname "$FLAG")"; : > "$FLAG"; log "created $FLAG"
else
    log "would create $FLAG"
fi

# 2. init.d boot hooks
if [ "$MODE" = "apply" ]; then
    mkdir -p "$INITD"
    for f in $INITD_FILES; do
        cp -f "$SRC_DIR/$f" "$INITD/$f"; chmod 755 "$INITD/$f"
        log "installed $INITD/$f (chmod 755)"
    done
else
    log "would install into $INITD/ : $INITD_FILES"
fi

# 3. manual verifier
if [ "$MODE" = "apply" ]; then
    mkdir -p "$DEST"
    for f in $DEST_FILES; do
        cp -f "$SRC_DIR/$f" "$DEST/$f"; chmod 755 "$DEST/$f"
        log "installed $DEST/$f"
    done
else
    log "would install into $DEST/ : $DEST_FILES"
fi

log ""
log "Next steps (as root on the TV):"
log "  sh $INITD/10-block-lg-telemetry   # apply hosts block now (no reboot)"
log "  sh $INITD/20-decline-lg-tos       # decline consents + settings now"
log "  sh $INITD/30-clear-lg-app-update  # clear app-update nag now"
log "  sh $DEST/verify-lockout           # verify"
log "  reboot                            # confirm the hooks run at boot"
log "  sh $DEST/verify-lockout           # section 7 shows the boot log"
log ""
log "run-parts runs the init.d files in numeric order every boot (once per boot,"
log "AFTER webosbrew's own hosts mount). Filenames must stay dot-free + executable."
exit 0
