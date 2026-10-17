#!/usr/bin/env python3
"""Deploy a Vue frontend and a Django backend to a server over SSH.

- installs nginx and git (plus Python/Node, or Docker when `docker: true`) if missing
- pulls both projects from git
- docker: false -> builds the Vue project on the server and serves dist/ with nginx,
                   runs the Django project with gunicorn under systemd
- docker: true  -> runs each project with `docker compose`
- nginx proxies each domain to the right place
"""
import argparse
import shlex
import subprocess
import sys
from pathlib import Path

import yaml  # pip install pyyaml

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.yml"
ENVIRONMENTS = ("staging", "production")


def load_config(env):
    if not CONFIG.exists():
        sys.exit(f"{CONFIG.name} not found. Copy config.yml.example to config.yml and fill it in.")
    cfg = (yaml.safe_load(CONFIG.read_text()) or {}).get(env)
    if not isinstance(cfg, dict):
        sys.exit(f"No '{env}' section in {CONFIG.name}")

    required = {
        "": ["ip", "username", "ssh_file", "frontend", "backend"],
        "frontend": ["domain", "remote_path", "git_url"],
        "backend": ["domain", "git_url", "remote_path", "service_name"],
    }
    missing = []
    for section, keys in required.items():
        node = cfg.get(section, {}) if section else cfg
        missing += [f"{section + '.' if section else ''}{k}" for k in keys if not node.get(k)]
    if missing:
        sys.exit(f"Missing in {CONFIG.name} ({env}): {', '.join(missing)}")

    return cfg


class Server:
    def __init__(self, cfg):
        self.target = f"{cfg['username']}@{cfg['ip']}"
        self.key = str(Path(cfg["ssh_file"]).expanduser())
        self.ssh_opts = ["-i", self.key, "-o", "StrictHostKeyChecking=accept-new"]

    def run(self, script, stdin=None):
        """Run a bash script on the server."""
        cmd = ["ssh", *self.ssh_opts, self.target, "bash -s"]
        subprocess.run(cmd, input=f"set -e\n{script}", text=True, check=True)

    def write_file(self, path, content):
        """Write a file on the server as root."""
        cmd = ["ssh", *self.ssh_opts, self.target, f"sudo tee {shlex.quote(path)} >/dev/null"]
        subprocess.run(cmd, input=content, text=True, check=True)


def step(msg):
    print(f"\n==> {msg}")


def bootstrap(srv, docker):
    step("Installing required tools if missing")
    srv.run(r"""
install_pkgs() {
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -y && sudo apt-get install -y "$@"
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y "$@"
    elif command -v yum >/dev/null 2>&1; then
        sudo yum install -y "$@"
    else
        echo "No supported package manager found" >&2; exit 1
    fi
}
install_pkgs nginx git curl
sudo systemctl enable --now nginx
""")
    if docker:
        srv.run(r"""
command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable --now docker
sudo docker compose version
""")
        return
    srv.run(r"""
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get install -y python3 python3-venv python3-pip
else
    sudo dnf install -y python3 python3-pip 2>/dev/null || sudo yum install -y python3 python3-pip
fi

# Node 18+ is needed to build the frontend on the server
if ! command -v node >/dev/null 2>&1 || [ "$(node -p 'process.versions.node.split(".")[0]')" -lt 18 ]; then
    if command -v apt-get >/dev/null 2>&1; then
        curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
        sudo apt-get install -y nodejs
    else
        curl -fsSL https://rpm.nodesource.com/setup_22.x | sudo bash -
        sudo dnf install -y nodejs 2>/dev/null || sudo yum install -y nodejs
    fi
fi
""")


def prepare_dirs(srv, cfg):
    user = cfg["username"]
    for path in (cfg["frontend"]["remote_path"], cfg["backend"]["remote_path"]):
        srv.run(f"sudo mkdir -p {shlex.quote(path)} && sudo chown -R {user}: {shlex.quote(path)}")


def git_sync(srv, remote_path, git_url, branch):
    """Clone on first run, `git pull` afterwards."""
    path, url, br = shlex.quote(remote_path), shlex.quote(git_url), shlex.quote(branch)
    srv.run(f"""
cd {path}
if [ ! -d .git ]; then
    git init -q
    git remote add origin {url}
    git fetch -q origin {br}
    git checkout -q -f -B {br} origin/{br}
else
    git remote set-url origin {url}
    git checkout -q {br}
    git pull --ff-only origin {br}
fi
""")


def proxy_conf(domain, port):
    return f"""server {{
    listen 80;
    server_name {domain};

    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""


def deploy_frontend(srv, cfg, docker):
    fe, be = cfg["frontend"], cfg["backend"]
    api_url = fe.get("api_url") or f"http://{be['domain']}"
    port = fe.get("port", 8080)
    path = shlex.quote(fe["remote_path"])
    conf = f"/etc/nginx/conf.d/{be['service_name']}-frontend.conf"

    step(f"Updating frontend from git ({fe['git_url']})")
    git_sync(srv, fe["remote_path"], fe["git_url"], fe.get("branch", "main"))

    if docker:
        step(f"Starting frontend with docker compose (VITE_API_URL={api_url})")
        srv.run(f"""
cd {path}
printf 'VITE_API_URL=%s\\nFRONTEND_PORT=%s\\n' {shlex.quote(api_url)} {port} > .env
sudo docker compose -p {shlex.quote(be['service_name'] + '-frontend')} up -d --build
""")
        srv.write_file(conf, proxy_conf(fe["domain"], port))
        return

    step(f"Building frontend on the server (VITE_API_URL={api_url})")
    srv.run(f"""
cd {path}
export VITE_API_URL={shlex.quote(api_url)}
if [ -f package-lock.json ]; then npm ci; else npm install; fi
npm run build
""")

    step("Configuring nginx for frontend")
    srv.write_file(conf, f"""server {{
    listen 80;
    server_name {fe['domain']};

    root {fe['remote_path']}/dist;
    index index.html;

    location / {{
        try_files $uri $uri/ /index.html;
    }}
}}
""")


def deploy_backend(srv, cfg, docker):
    be, fe = cfg["backend"], cfg["frontend"]
    path, name, user = be["remote_path"], be["service_name"], cfg["username"]
    port = be.get("port", 8000)
    wsgi = be.get("wsgi_module", "config.wsgi:application")
    q = shlex.quote(path)

    step(f"Updating backend from git ({be['git_url']})")
    git_sync(srv, path, be["git_url"], be.get("branch", "main"))

    step("Writing backend environment")
    # Hosts/CORS come from the domains in config.yml; the secret key is generated
    # once on the server and kept across deploys.
    srv.run(f"""
cd {q}
touch .env && chmod 600 .env
grep -q '^DJANGO_SECRET_KEY=' .env || echo "DJANGO_SECRET_KEY=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 50)" >> .env
sed -i '/^DJANGO_ALLOWED_HOSTS=/d;/^CORS_ALLOWED_ORIGINS=/d;/^BACKEND_PORT=/d' .env
echo 'DJANGO_ALLOWED_HOSTS={be["domain"]}' >> .env
echo 'CORS_ALLOWED_ORIGINS=http://{fe["domain"]}' >> .env
echo 'BACKEND_PORT={port}' >> .env
""")

    if docker:
        step(f"Starting backend with docker compose ({name})")
        srv.run(f"""
cd {q}
mkdir -p data
sudo docker compose -p {shlex.quote(name)} up -d --build
""")
    else:
        step("Installing dependencies and migrating")
        srv.run(f"""
cd {q}
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
set -a; . ./.env; set +a
venv/bin/python manage.py migrate --noinput
""")

        step(f"Configuring systemd service {name}")
        srv.write_file(f"/etc/systemd/system/{name}.service", f"""[Unit]
Description={name} (gunicorn)
After=network.target

[Service]
User={user}
WorkingDirectory={path}
EnvironmentFile={path}/.env
ExecStart={path}/venv/bin/gunicorn {wsgi} --bind 127.0.0.1:{port} --workers 2
Restart=always

[Install]
WantedBy=multi-user.target
""")
        srv.run(f"""
sudo systemctl daemon-reload
sudo systemctl enable {name}
sudo systemctl restart {name}
""")

    step("Configuring nginx for backend")
    srv.write_file(f"/etc/nginx/conf.d/{name}-backend.conf", proxy_conf(be["domain"], port))


def reload_nginx(srv):
    step("Reloading nginx")
    srv.run("sudo nginx -t && sudo systemctl reload nginx")


def main():
    parser = argparse.ArgumentParser(description="Deploy frontend and backend over SSH.")
    parser.add_argument("environment", nargs="?", choices=ENVIRONMENTS, default="staging",
                        help="target environment (default: staging)")
    env = parser.parse_args().environment
    cfg = load_config(env)
    srv = Server(cfg)
    docker = bool(cfg.get("docker", False))
    print(f"Deploying {env} to {srv.target} ({'docker' if docker else 'systemd'})")
    bootstrap(srv, docker)
    prepare_dirs(srv, cfg)
    deploy_frontend(srv, cfg, docker)
    deploy_backend(srv, cfg, docker)
    reload_nginx(srv)
    print(f"\nDone.\n  Frontend: http://{cfg['frontend']['domain']}\n  Backend:  http://{cfg['backend']['domain']}/api/health/")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.exit(f"\nFailed: {' '.join(map(str, e.cmd))[:200]} (exit {e.returncode})")
