#!/usr/bin/env python3
"""Quickly update the backend and frontend on the server: git pull, migrate, collectstatic, restart, notify.

Unlike deploy.py this does not provision anything (no packages, nginx or unit files);
use it for routine code updates once the server has been deployed.
"""
import argparse
import shlex
import subprocess
import sys

import deploy
from deploy import Server, app_dir, git_sync, load_config, notify, step


def restart_backend(srv, cfg, docker):
    be = cfg["backend"]
    name, path = be["service_name"], app_dir(be)
    q = shlex.quote(path)

    step(f"Pulling latest code ({be['git_url']})")
    git_sync(srv, be["remote_path"], be["git_url"], be.get("branch", "main"))

    if docker:
        compose_file = be.get("compose_file", f"docker/{cfg['_env']}/docker-compose.yml")
        step(f"Rebuilding and restarting {name} (docker compose)")
        srv.run(f"""
cd {q}
sudo docker compose -f {shlex.quote(compose_file)} --env-file .env -p {shlex.quote(name)} up -d --build
""")
    else:
        step("Installing dependencies")
        srv.run(f"cd {q}\nvenv/bin/pip install -q -r {shlex.quote(be.get('requirements_path', 'requirements.txt'))}")

    deploy.django_release(srv, cfg, docker)

    if not docker:
        step(f"Restarting service {name}")
        srv.run(f"sudo systemctl restart {shlex.quote(name)}\nsleep 3\nsudo systemctl is-active {shlex.quote(name)}")


def restart_frontend(srv, cfg, docker):
    deploy.build_frontend(srv, cfg, docker)


def main():
    parser = argparse.ArgumentParser(description="Pull, migrate, collectstatic and restart the backend and frontend on the server (nginx config is not changed).")
    parser.add_argument("environment", nargs="?", choices=deploy.ENVIRONMENTS, default="staging",
                        help="target environment (default: staging)")
    args = parser.parse_args()
    env = args.environment
    cfg = load_config(env)
    cfg["_env"] = env
    srv = Server(cfg)
    docker = bool(cfg.get("docker", False))
    print(f"Restarting {env} backend and frontend on {srv.target} ({'docker' if docker else 'systemd'})")

    try:
        restart_backend(srv, cfg, docker)
        restart_frontend(srv, cfg, docker)
        healthy, lines = deploy.verify(cfg)
    except Exception:
        notify(cfg, f"Restart {env} FAILED", f"Failed during: {deploy._current_step}", ok=False)
        raise

    notify(cfg, f"Restart {env} {'OK' if healthy else 'FAILED'}", "\n".join(lines), ok=healthy)
    if not healthy:
        sys.exit("\nVerification failed:\n  " + "\n  ".join(l for l in lines if l.startswith("[fail]")))
    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.exit(f"\nFailed: {' '.join(map(str, e.cmd))[:200]} (exit {e.returncode})")
