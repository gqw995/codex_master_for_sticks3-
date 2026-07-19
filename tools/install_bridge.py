#!/usr/bin/env python3
"""Install the StickS3 Usage Bridge as a per-user macOS LaunchAgent."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


LABEL = "ai.vibecafe.sticks3-usage-bridge"


def main() -> int:
    source_dir = Path(__file__).resolve().parent
    source_bridge = source_dir / "codex_usage_bridge.py"
    if not source_bridge.exists():
        print(f"Missing {source_bridge.name}; keep the installer files together.")
        return 1

    support_dir = Path.home() / "Library" / "Application Support" / "StickS3 Usage Bridge"
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    support_dir.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)

    installed_bridge = support_dir / "codex_usage_bridge.py"
    shutil.copy2(source_bridge, installed_bridge)
    installed_bridge.chmod(0o755)

    python = Path(sys.executable).resolve()
    plist_path = agents_dir / f"{LABEL}.plist"
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(python), str(installed_bridge)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(support_dir / "bridge.log"),
        "StandardErrorPath": str(support_dir / "bridge-error.log"),
    }
    with plist_path.open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=True)
    plist_path.chmod(0o644)

    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=False,
                            capture_output=True, text=True)
    if result.returncode != 0:
        print("Could not start the background bridge:")
        print((result.stderr or result.stdout).strip())
        print(f"The files are installed; try logging out and back in, or rerun {plist_path}.")
        return result.returncode

    subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("StickS3 Usage Bridge is installed and running.")
    print("It will reconnect automatically after USB hot-plug and after login.")
    print(f"Log: {support_dir / 'bridge.log'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
