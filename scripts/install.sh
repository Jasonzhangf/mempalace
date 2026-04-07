#!/bin/bash
# Quick install script for mempalace CLI
# Usage: ./scripts/install.sh [--user]

set -euo pipefail
cd "$(dirname "$0")/.."

USER_INSTALL="${1:-}"

if [[ "$USER_INSTALL" == "--user" ]]; then
    echo "==> Installing mempalace to user site-packages..."
    pip3 install --user -e .
else
    echo "==> Creating venv and installing mempalace..."
    if [[ ! -d ".venv" ]]; then
        python3 -m venv .venv
    fi
    source .venv/bin/activate
    pip install -e .
fi

echo ""
echo "==> Installation complete!"
echo ""
echo "    Usage:"
echo "    source .venv/bin/activate && mempalace --help"
echo "    Or if installed --user:"
echo "    mempalace --help"
echo ""
echo "    Quick commands:"
echo "    mempalace init ~/projects/myapp"
echo "    mempalace mine ~/projects/myapp"
echo "    mempalace search \"database decision\""
echo "    mempalace status"
echo "    mempalace wake-up"
