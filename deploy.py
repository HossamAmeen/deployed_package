#!/usr/bin/env python3
"""Deploy a Vue frontend and a Django backend to a server over SSH.

- installs nginx and git (plus Python/Node, or Docker when `docker: true`) if missing
- pulls both projects from git
- docker: false -> builds the Vue project on the server and serves dist/ with nginx,
                   runs the Django project with gunicorn under systemd
- docker: true  -> runs each project with `docker compose`
- nginx proxies each domain to the right place
- optional: Let's Encrypt certificates (`ssl: true`) and a desktop pop-up when done
"""
import argparse
import http.client
import json
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import time
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
    if cfg.get("ssl") and not cfg.get("ssl_email"):
        sys.exit(f"Missing in {CONFIG.name} ({env}): ssl_email (required when ssl: true)")

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

    def test(self, script):
        """Run a bash script on the server and return True if it exits 0."""
        cmd = ["ssh", *self.ssh_opts, self.target, "bash -s"]
        return subprocess.run(cmd, input=script, text=True, capture_output=True).returncode == 0

    def write_file(self, path, content):
        """Write a file on the server as root."""
        cmd = ["ssh", *self.ssh_opts, self.target, f"sudo tee {shlex.quote(path)} >/dev/null"]
        subprocess.run(cmd, input=content, text=True, check=True)


_current_step = "starting"


def step(msg):
    global _current_step
    _current_step = msg
    print(f"\n==> {msg}")


def scheme(cfg):
    return "https" if cfg.get("ssl") else "http"


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


def already_provisioned(srv, cfg, docker):
    """True if both projects are checked out on the server and (without docker)
    the backend service exists, so the one-time server setup can be skipped."""
    q = lambda k: shlex.quote(cfg[k]["remote_path"])
    script = f"{{ command -v nginx || [ -x /usr/sbin/nginx ]; }} && [ -d {q('frontend')}/.git ] && [ -d {q('backend')}/.git ]"
    if docker:
        script += " && command -v docker"
    else:
        script += f" && systemctl cat {shlex.quote(cfg['backend']['service_name'])}.service"
    return srv.test(script + " >/dev/null 2>&1")


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
    api_url = fe.get("api_url") or f"{scheme(cfg)}://{be['domain']}"
    port = fe.get("port", 8080)
    dockerfile = fe.get("dockerfile", f"docker/{cfg['_env']}/Dockerfile")
    compose_file = fe.get("compose_file", f"docker/{cfg['_env']}/docker-compose.yml")
    path = shlex.quote(fe["remote_path"])
    conf = f"/etc/nginx/conf.d/{be['service_name']}-frontend.conf"

    step(f"Updating frontend from git ({fe['git_url']})")
    git_sync(srv, fe["remote_path"], fe["git_url"], fe.get("branch", "main"))

    if docker:
        step(f"Starting frontend with docker compose (VITE_API_URL={api_url})")
        srv.run(f"""
cd {path}
printf 'VITE_API_URL=%s\\nFRONTEND_PORT=%s\\nDOCKERFILE=%s\\n' {shlex.quote(api_url)} {port} {shlex.quote(dockerfile)} > .env
sudo docker compose -f {shlex.quote(compose_file)} --env-file .env -p {shlex.quote(be['service_name'] + '-frontend')} up -d --build
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
    dockerfile = be.get("dockerfile", f"docker/{cfg['_env']}/Dockerfile")
    compose_file = be.get("compose_file", f"docker/{cfg['_env']}/docker-compose.yml")
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
sed -i '/^DJANGO_ALLOWED_HOSTS=/d;/^CORS_ALLOWED_ORIGINS=/d;/^BACKEND_PORT=/d;/^DOCKERFILE=/d' .env
echo 'DJANGO_ALLOWED_HOSTS={be["domain"]}' >> .env
echo 'CORS_ALLOWED_ORIGINS={scheme(cfg)}://{fe["domain"]}' >> .env
echo 'BACKEND_PORT={port}' >> .env
echo 'DOCKERFILE={dockerfile}' >> .env
""")

    if docker:
        step(f"Starting backend with docker compose ({name})")
        srv.run(f"""
cd {q}
mkdir -p data
sudo docker compose -f {shlex.quote(compose_file)} --env-file .env -p {shlex.quote(name)} up -d --build
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

        step(f"Setting up systemd service {name}")
        unit = f"/etc/systemd/system/{name}.service"
        tmp = f"/tmp/{name}.service.new"
        srv.write_file(tmp, f"""[Unit]
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
        # If the service already exists with the same definition, only restart it;
        # otherwise (new service or changed settings) install the unit first.
        srv.run(f"""
if [ -f {unit} ] && cmp -s {tmp} {unit}; then
    echo "Service {name} already exists and is unchanged: restarting only"
else
    echo "Installing service {name}"
    sudo install -m 644 {tmp} {unit}
    sudo systemctl daemon-reload
    sudo systemctl enable {name}
fi
sudo rm -f {tmp}
sudo systemctl restart {name}
""")
        step(f"Checking {name} is serving port {port}")
        srv.run(f"""
sleep 6
pid=$(sudo ss -ltnp "sport = :{port}" | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)
if [ -z "$pid" ] || ! grep -q '{name}.service' /proc/$pid/cgroup; then
    echo "ERROR: port {port} is not served by {name}.service" >&2
    [ -n "$pid" ] && echo "Port {port} is held by: $(ps -o pid,cmd -p $pid | tail -1)" >&2
    sudo journalctl -u {name} -n 8 --no-pager >&2
    exit 1
fi
echo "ok: {name} is listening on port {port}"
""")

    step("Configuring nginx for backend")
    srv.write_file(f"/etc/nginx/conf.d/{name}-backend.conf", proxy_conf(be["domain"], port))


def reload_nginx(srv):
    step("Reloading nginx")
    srv.run("sudo nginx -t && sudo systemctl reload nginx")


def dns_points_to_server(domain, ip):
    """Return (ok, resolved addresses)."""
    try:
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(domain, 80, socket.AF_INET)})
    except socket.gaierror:
        return False, []
    return ip in addrs, addrs


class _HTTPSToIP(http.client.HTTPSConnection):
    """HTTPS to a fixed IP while sending the domain as SNI and verifying its certificate."""

    def __init__(self, domain, ip, timeout):
        super().__init__(domain, 443, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def http_get(ip, domain, path, use_ssl=False, retries=10, delay=3):
    """GET <scheme>://<domain><path> by connecting straight to the server IP, so it
    works before DNS is set up. Returns (status, body) or (None, error)."""
    last = "no response"
    for _ in range(retries):
        try:
            if use_ssl:
                conn = _HTTPSToIP(domain, ip, 10)
            else:
                conn = http.client.HTTPConnection(ip, 80, timeout=10)
            conn.request("GET", path, headers={"Host": domain})
            res = conn.getresponse()
            body = res.read().decode(errors="replace")
            conn.close()
            if res.status < 500:
                return res.status, body
            last = f"HTTP {res.status}"
        except (OSError, ssl.SSLError) as e:
            last = str(e)
        time.sleep(delay)
    return None, last


def verify(cfg):
    """Check that each domain points at the server and that the API answers.
    Returns (ok, report lines)."""
    step("Verifying deployment")
    ip, fe, be = cfg["ip"], cfg["frontend"], cfg["backend"]
    health_path = be.get("health_path", "/api/health/")
    use_ssl = bool(cfg.get("ssl"))
    sch = scheme(cfg)
    ok, lines = True, []

    def report(line):
        print(f"  {line}")
        lines.append(line)

    for domain in (fe["domain"], be["domain"]):
        points, addrs = dns_points_to_server(domain, ip)
        if points:
            report(f"[ok] DNS {domain} -> {ip}")
        else:
            found = ", ".join(addrs) if addrs else "does not resolve"
            report(f"[warn] DNS {domain} -> {found} (expected {ip})")

    status, body = http_get(ip, be["domain"], health_path, use_ssl)
    version = None
    if status == 200:
        try:
            version = json.loads(body).get("version")
        except (ValueError, AttributeError):
            pass
    url = f"{sch}://{be['domain']}{health_path}"
    if status == 200 and version:
        report(f"[ok] API {url} -> version {version}")
    else:
        ok = False
        report(f"[fail] API {url} -> {status or body}")

    status, body = http_get(ip, fe["domain"], "/", use_ssl)
    if status == 200:
        report(f"[ok] Frontend {sch}://{fe['domain']}/ -> 200")
    else:
        ok = False
        report(f"[fail] Frontend {sch}://{fe['domain']}/ -> {status or body}")
    return ok, lines


def setup_ssl(srv, cfg):
    """Get a Let's Encrypt certificate for each domain that points at the server."""
    step("Installing SSL certificates (Let's Encrypt)")
    srv.run(r"""
if ! command -v certbot >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get install -y certbot python3-certbot-nginx
    else
        sudo dnf install -y certbot python3-certbot-nginx 2>/dev/null || sudo yum install -y certbot python3-certbot-nginx
    fi
fi
""")
    for domain in (cfg["frontend"]["domain"], cfg["backend"]["domain"]):
        points, addrs = dns_points_to_server(domain, cfg["ip"])
        if not points:
            print(f"  [warn] skipping {domain}: DNS does not point to {cfg['ip']} (certbot would fail)")
            continue
        srv.run(f"sudo certbot --nginx --non-interactive --agree-tos --redirect "
                f"-m {shlex.quote(cfg['ssl_email'])} -d {shlex.quote(domain)}")


def notify(cfg, title, message, ok):
    """Show a desktop pop-up on this machine when the deploy finishes. Never raises."""
    if (cfg.get("notify") or {}).get("desktop", True) and shutil.which("notify-send"):
        urgency = "normal" if ok else "critical"
        subprocess.run(["notify-send", "-u", urgency, title, message],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description="Deploy frontend and backend over SSH.")
    parser.add_argument("environment", nargs="?", choices=ENVIRONMENTS, default="staging",
                        help="target environment (default: staging)")
    parser.add_argument("--full", action="store_true",
                        help="always run the one-time server setup (packages, directories)")
    parser.add_argument("--verify", action="store_true",
                        help="only run the verification checks, do not deploy")
    args = parser.parse_args()
    env = args.environment
    cfg = load_config(env)
    cfg["_env"] = env
    if args.verify:
        healthy, lines = verify(cfg)
        sys.exit(0 if healthy else "\nVerification failed:\n  " + "\n  ".join(l for l in lines if l.startswith("[fail]")))
    srv = Server(cfg)
    docker = bool(cfg.get("docker", False))
    print(f"Deploying {env} to {srv.target} ({'docker' if docker else 'systemd'})")

    try:
        if not args.full and already_provisioned(srv, cfg, docker):
            step("Project and service found on the server: skipping setup, updating and restarting")
        else:
            bootstrap(srv, docker)
            prepare_dirs(srv, cfg)
        deploy_frontend(srv, cfg, docker)
        deploy_backend(srv, cfg, docker)
        reload_nginx(srv)
        if cfg.get("ssl"):
            setup_ssl(srv, cfg)
        healthy, lines = verify(cfg)
    except Exception as e:
        notify(cfg, f"Deploy {env} FAILED", f"Failed during: {_current_step}", ok=False)
        raise

    sch = scheme(cfg)
    print(f"\nDone.\n  Frontend: {sch}://{cfg['frontend']['domain']}\n  Backend:  {sch}://{cfg['backend']['domain']}/api/health/")
    notify(cfg, f"Deploy {env} {'OK' if healthy else 'FAILED'}", "\n".join(lines), ok=healthy)
    if not healthy:
        sys.exit("\nVerification failed:\n  " + "\n  ".join(l for l in lines if l.startswith("[fail]")))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.exit(f"\nFailed: {' '.join(map(str, e.cmd))[:200]} (exit {e.returncode})")
