"""Read server diagnostics through Actions using the pinned deployment SSH identity."""

import os
import sys
import tempfile
from pathlib import Path

import paramiko


def main():
    for name in ["SSH_HOST", "SSH_USER", "SSH_PRIVATE_KEY", "SSH_KNOWN_HOSTS"]:
        if not os.environ.get(name, "").strip():
            raise ValueError(f"Configure the {name} GitHub Actions secret")
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
            stdin, stdout, _ = client.exec_command("bash -s 2>&1", timeout=180)
            stdin.write(Path("scripts/status.sh").read_text(encoding="utf-8"))
            stdin.channel.shutdown_write()
            for line in stdout:
                print(line, end="", flush=True)
            return stdout.channel.recv_exit_status()


if __name__ == "__main__":
    sys.exit(main())
