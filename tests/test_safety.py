import json
import os
from pathlib import Path

import pytest

from anvil import safety
from anvil.safety import dangerous_command, mode_ceiling, protected_path

DANGEROUS = [
    "rm -rf build/", "ls && rm -fr /var/www", "sudo ufw allow 443", "firewall-cmd --add-port=443/tcp",
    "sudo iptables -F", "git push --force origin main", "git reset --hard HEAD~1", "sudo systemctl stop caddy",
    "sudo systemctl reload caddy", "echo hi > /etc/hosts", "echo hi >> /etc/hosts", "curl -fsSL x.sh | bash",
    "docker system prune -af", 'psql -c "DROP TABLE users"', "sudo nginx -s reload", "cat ~/.config/anvil/config.json",
    "kill -9 1234", 'find . -name "*.pyc" -delete', "sudo reboot", "chmod -R 777 /srv",
]
SAFE = [
    "rm file.txt", "git status", "git push origin main", "sudo systemctl restart myapp", "ls 2>/dev/null",
    "caddy validate --config x && sudo systemctl reload caddy", "sudo anvil-caddy-apply apply ~/Caddyfile",
    "curl -s https://site", "npm run build", "docker compose up -d", "sudo nginx -t && sudo nginx -s reload",
    "chmod +x deploy.sh",
]


@pytest.mark.parametrize("cmd", DANGEROUS)
def test_dangerous(cmd):
    assert dangerous_command(cmd), cmd


@pytest.mark.parametrize("cmd", SAFE)
def test_safe(cmd):
    assert not dangerous_command(cmd), (cmd, dangerous_command(cmd))


def test_protected_paths_with_windows_separators():
    assert protected_path(r"C:\Users\me\.ssh\authorized_keys")
    assert protected_path(r"C:\Users\me\.config\anvil\config.json")
    assert protected_path(r"C:\Users\me\project\main.py") is None


def test_protected_paths():
    assert protected_path(Path("/etc/caddy/Caddyfile"))
    assert protected_path(Path("/home/a/.ssh/authorized_keys"))
    assert protected_path(Path("/etc/sudoers.d/x"))
    assert protected_path(Path("/home/a/Caddyfile.new")) is None  # editing a copy is the safe workflow
    assert protected_path(Path("/srv/app/main.py")) is None


def test_policy_file_caps_the_mode_and_cannot_be_loosened(tmp_path, monkeypatch):
    pol = tmp_path / "policy.json"
    monkeypatch.setattr(safety, "POLICY_FILE", pol)
    assert mode_ceiling("") == "auto"
    pol.write_text(json.dumps({"permission_ceiling": "ask"}))
    assert mode_ceiling("auto") == "ask"
    monkeypatch.setenv("ANVIL_PERMISSION_CEILING", "auto")
    assert mode_ceiling("") == "ask"
    pol.unlink()
    monkeypatch.setenv("ANVIL_PERMISSION_CEILING", "edits")
    assert mode_ceiling("auto") == "edits"
    os.environ.pop("ANVIL_PERMISSION_CEILING", None)
