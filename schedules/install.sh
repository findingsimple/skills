#!/bin/zsh
# Install Claude Code scheduled tasks by generating plists from templates
# and symlinking them into ~/Library/LaunchAgents/
#
# Usage:
#   ./install.sh           # install and load all schedules
#   ./install.sh --unload  # unload and remove all symlinks

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

# Find all template files
templates=("$SCRIPT_DIR"/*.plist.template(N))

if [[ ${#templates[@]} -eq 0 ]]; then
    echo "No .plist.template files found in $SCRIPT_DIR"
    exit 0
fi

if [[ "${1:-}" == "--unload" ]]; then
    for template in "${templates[@]}"; do
        plist_name="$(basename "$template" .template)"
        label="$(basename "$plist_name" .plist)"
        target="$LAUNCH_AGENTS_DIR/$plist_name"

        if launchctl list "$label" &>/dev/null; then
            echo "Unloading $label..."
            launchctl bootout "gui/$(id -u)" "$target" 2>/dev/null || true
        fi

        if [[ -L "$target" ]]; then
            echo "Removing symlink $target"
            rm "$target"
        fi

        # Remove generated plist
        generated="$SCRIPT_DIR/$plist_name"
        if [[ -f "$generated" ]]; then
            echo "Removing generated $generated"
            rm "$generated"
        fi
    done
    echo "Done — all schedules unloaded."
    exit 0
fi

# Install mode
mkdir -p "$LAUNCH_AGENTS_DIR"

for template in "${templates[@]}"; do
    plist_name="$(basename "$template" .template)"
    label="$(basename "$plist_name" .plist)"
    generated="$SCRIPT_DIR/$plist_name"
    target="$LAUNCH_AGENTS_DIR/$plist_name"

    # Generate plist from template
    sed "s|{{HOME}}|$HOME|g" "$template" > "$generated"
    echo "Generated $generated"

    # Unload if already loaded
    if launchctl list "$label" &>/dev/null; then
        echo "Unloading existing $label..."
        launchctl bootout "gui/$(id -u)" "$target" 2>/dev/null || true
    fi

    # Create or update symlink
    if [[ -L "$target" ]]; then
        rm "$target"
    elif [[ -f "$target" ]]; then
        echo "WARNING: $target exists and is not a symlink — skipping"
        continue
    fi

    ln -s "$generated" "$target"
    echo "Linked $target → $generated"

    # Load the agent
    launchctl bootstrap "gui/$(id -u)" "$target"
    echo "Loaded $label"
done

echo "Done — all schedules installed."
