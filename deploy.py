#!/usr/bin/env python3
"""Connect to a server over SSH and install nginx if it is missing."""
import subprocess
import sys
from pathlib import Path

import yaml  # pip install pyyaml

CONFIG = Path(__file__).with_name("config.yml")

REMOTE_SCRIPT = r"""
set -e
if command -v nginx >/dev/null 2>&1; then
    echo "nginx already installed: $(nginx -v 2>&1)"
else
    echo "nginx not found, installing..."
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -y && sudo apt-get install -y nginx
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y nginx
    elif command -v yum >/dev/null 2>&1; then
        sudo yum install -y nginx
    else
        echo "No supported package manager found" >&2
        exit 1
    fi
fi
sudo systemctl enable --now nginx
echo "nginx is running"
"""


def load_config():
    if not CONFIG.exists():
        sys.exit(f"{CONFIG.name} not found. Copy config.yml.example to config.yml and fill it in.")
    cfg = yaml.safe_load(CONFIG.read_text()) or {}
    missing = [k for k in ("ip", "username", "ssh_file") if not cfg.get(k)]
    if missing:
        sys.exit(f"Missing in {CONFIG.name}: {', '.join(missing)}")
    return cfg


def main():
    cfg = load_config()
    key = str(Path(cfg["ssh_file"]).expanduser())
    cmd = [
        "ssh",
        "-i", key,
        "-o", "StrictHostKeyChecking=accept-new",
        f"{cfg['username']}@{cfg['ip']}",
        "bash -s",
    ]
    print(f"Deploying to {cfg['username']}@{cfg['ip']} ...")
    sys.exit(subprocess.run(cmd, input=REMOTE_SCRIPT, text=True).returncode)


if __name__ == "__main__":
    main()
