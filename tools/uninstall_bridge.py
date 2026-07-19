#!/usr/bin/env python3
"""Uninstall the per-user StickS3 Usage Bridge LaunchAgent."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


LABEL = "ai.vibecafe.sticks3-usage-bridge"


def main() -> int:
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    support_dir = Path.home() / "Library" / "Application Support" / "StickS3 Usage Bridge"
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if plist_path.exists():
        plist_path.unlink()
    if support_dir.exists():
        for child in support_dir.iterdir():
            if child.is_file():
                child.unlink()
        support_dir.rmdir()
    print("StickS3 Usage Bridge has been removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
