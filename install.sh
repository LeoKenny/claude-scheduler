#!/bin/sh
# Copy the mod into the Herdr app and install the service on this machine.
#
#   ./install.sh              mod + service
#   ./install.sh --mod-only   only the mod (install the service from its popup)

set -eu

here=$(cd "$(dirname "$0")" && pwd)
app=${HERDR_APP_HOME:-$HOME/.config/herdr-app}
dst=$app/mods/claude-scheduler

mkdir -p "$dst"
cp "$here/mod/claude-scheduler/main.js" "$here/mod/claude-scheduler/mod.json" \
	"$here/mod/claude-scheduler/claude_scheduler.py" "$dst/"
echo "mod copied to $dst"

if command -v herdr-app >/dev/null 2>&1; then
	herdr-app check "$dst"
fi

if [ "${1:-}" != --mod-only ]; then
	python3 -I "$dst/claude_scheduler.py" install
fi

cat <<EOF

To open it with a shortcut, add this to the "global" section of
$app/keys.json:

    "ctrl-shift-s": "mod.open:claude-scheduler",
EOF
