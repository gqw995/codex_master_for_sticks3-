#!/bin/zsh
set -e
SCRIPT_DIR="${0:A:h}"
PYTHON_BIN=""
for candidate in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [[ -x "$candidate" ]]; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3 was not found."
  read -k 1 "?Press any key to close..."
  exit 1
fi
"$PYTHON_BIN" "$SCRIPT_DIR/uninstall_bridge.py"
echo
read -k 1 "?Done. Press any key to close..."
