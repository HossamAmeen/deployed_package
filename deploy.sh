#!/usr/bin/env bash
# Connect to a server over SSH and install nginx if it is missing.
set -euo pipefail

CONFIG="$(dirname "$0")/config.yml"
[ -f "$CONFIG" ] || { echo "config.yml not found. Copy config.yml.example to config.yml and fill it in." >&2; exit 1; }

# Read a top-level "key: value" from the YAML file (strips quotes and comments)
get() {
    sed -n "s/^$1:[[:space:]]*//p" "$CONFIG" | sed 's/[[:space:]]*#.*$//; s/^["'\'']//; s/["'\'']$//' | head -n1
}

IP=$(get ip)
USERNAME=$(get username)
SSH_FILE=$(get ssh_file)
SSH_FILE="${SSH_FILE/#\~/$HOME}"

for v in IP USERNAME SSH_FILE; do
    [ -n "${!v}" ] || { echo "Missing value in config.yml: $v" >&2; exit 1; }
done

echo "Deploying to $USERNAME@$IP ..."
ssh -i "$SSH_FILE" -o StrictHostKeyChecking=accept-new "$USERNAME@$IP" 'bash -s' <<'EOF'
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
EOF
