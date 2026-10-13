"""Upload a tested source archive and run its deployment script over pinned SSH."""

import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

import paramiko


def deployment_command(sha: str, mode: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Invalid commit SHA")
    if mode not in {"prepare", "deploy"}:
        raise ValueError("Invalid deployment mode")
    script = shlex.quote(f"/opt/miem-bot/incoming/{sha}.sh")
    return f"bash {script} {sha} {mode}"


def deploy():
    sha = os.environ["DEPLOY_SHA"]
    command = deployment_command(sha, os.environ.get("DEPLOY_MODE", "deploy"))
    for name in ["SSH_HOST", "SSH_USER", "SSH_PRIVATE_KEY", "SSH_KNOWN_HOSTS"]:
        if not os.environ.get(name, "").strip():
            raise ValueError(f"Configure the {name} GitHub Actions secret")
    archive = Path(os.environ.get("DEPLOY_ARCHIVE", "release.tar.gz"))
    with tempfile.TemporaryDirectory() as directory:
        known = Path(directory) / "known_hosts"
        key = Path(directory) / "key"
        known.write_text(os.environ["SSH_KNOWN_HOSTS"] + "\n", encoding="utf-8")
        key.write_text(os.environ["SSH_PRIVATE_KEY"] + "\n", encoding="utf-8")
        key.chmod(0o600)
        with paramiko.SSHClient() as client:
            client.load_host_keys(str(known))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(
                os.environ["SSH_HOST"],
                port=int(os.environ.get("SSH_PORT") or "22"),
                username=os.environ["SSH_USER"],
                key_filename=str(key),
                allow_agent=False,
                look_for_keys=False,
                timeout=30,
            )
            with client.open_sftp() as sftp:
                sftp.put(str(archive), f"/opt/miem-bot/incoming/{sha}.tar.gz")
                sftp.put("scripts/deploy.sh", f"/opt/miem-bot/incoming/{sha}.sh")
            _, stdout, _ = client.exec_command(command + " 2>&1", timeout=1800)
            for line in stdout:
                print(line, end="", flush=True)
            sys.exit(stdout.channel.recv_exit_status())


if __name__ == "__main__":
    deploy()
