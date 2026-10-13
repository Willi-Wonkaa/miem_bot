"""Host-only standard-library collector. Containers have no access to the Docker socket."""

import argparse
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger("host_monitor")


def memory_usage(value):
    match = re.fullmatch(r"\s*([\d.]+)\s*(B|kB|KB|MB|GB|KiB|MiB|GiB)\s*", value.split("/", 1)[0])
    if not match:
        raise ValueError("Unexpected Docker memory usage")
    unit = {
        "B": 1,
        "kB": 1000,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
    }[match[2]]
    return int(float(match[1]) * unit)


def command(*args):
    return subprocess.check_output(args, text=True, timeout=15).strip()


def memory():
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.split()[0]) * 1024
    return values["MemTotal"], values["MemAvailable"]


def cpu_sample():
    values = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:9]))
    return sum(values), values[3] + values[4]


def budget(
    total_cpu,
    available_cpu,
    total_ram,
    available_ram,
    indexer_cpu=0,
    indexer_ram=0,
    reserve_cpu=1.0,
    reserve_ram=3 * 1024**3,
):
    cpu_free = min(max(0, total_cpu - reserve_cpu), max(0, available_cpu + indexer_cpu))
    ram_free = min(
        max(0, total_ram - reserve_ram), max(0, available_ram + indexer_ram - reserve_ram)
    )
    return {
        "cpus": math.floor(cpu_free * 0.8 * 100) / 100,
        "memory": int(ram_free * 0.8),
        "reserve_cpu": reserve_cpu,
        "reserve_ram": reserve_ram,
    }


def atomic_json(path, payload):
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, delete=False, encoding="utf-8"
    ) as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
        temporary = Path(f.name)
    temporary.chmod(0o644)
    os.replace(temporary, path)


def collect(project):
    containers = {}
    ids = command(
        "docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"
    ).splitlines()
    for container_id in ids:
        details = json.loads(
            command(
                "docker",
                "inspect",
                "--format",
                '{"name":{{json .Name}},"state":{{json .State}},'
                '"service":{{json (index .Config.Labels "com.docker.compose.service")}}}',
                container_id,
            )
        )
        state = details["state"]
        service = details["service"]
        item = {
            "id": container_id,
            "name": details["name"].lstrip("/"),
            "state": state["Status"],
            "health": state.get("Health", {}).get("Status", "—"),
            "started": state["StartedAt"],
            "exit_code": state["ExitCode"],
        }
        if service not in containers or item["state"] == "running":
            containers[service] = item
    return containers


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--project", default="miem-bot")
    parser.add_argument("--reserve-cpu", type=float, default=1.0)
    parser.add_argument("--reserve-ram", type=int, default=3 * 1024**3)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        args.log_dir / "host-monitor.txt", maxBytes=10 * 1024**2, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    previous = cpu_sample()
    applied = None
    next_resume = 0
    while True:
        try:
            current = cpu_sample()
            total_cpu = len(os.sched_getaffinity(0))
            delta = max(1, current[0] - previous[0])
            idle_cpu = total_cpu * max(0, current[1] - previous[1]) / delta
            previous = current
            total_ram, free_ram = memory()
            containers = collect(args.project)
            indexer = containers.get("indexer", {})
            indexer_cpu, indexer_ram = 0.0, 0
            if indexer.get("state") == "running":
                stats = json.loads(
                    command(
                        "docker", "stats", "--no-stream", "--format", "{{json .}}", indexer["id"]
                    )
                )
                indexer_cpu = float(stats["CPUPerc"].rstrip("%")) / 100
                pid = int(command("docker", "inspect", "--format", "{{.State.Pid}}", indexer["id"]))
                try:
                    cgroup = next(
                        line.split(":", 2)[2]
                        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                        if line.startswith("0:")
                    )
                    indexer_ram = int(
                        (Path("/sys/fs/cgroup") / cgroup.lstrip("/") / "memory.current").read_text()
                    )
                except (OSError, StopIteration):
                    indexer_ram = memory_usage(stats["MemUsage"])
            limits = budget(
                total_cpu,
                idle_cpu,
                total_ram,
                free_ram,
                indexer_cpu,
                indexer_ram,
                args.reserve_cpu,
                args.reserve_ram,
            )
            limits["memory"] = min(limits["memory"], 4 * 1024**3)
            if (indexer.get("state") == "exited" and indexer.get("exit_code", 0) != 0
                    and time.monotonic() >= next_resume and limits["memory"] >= 1536 * 1024**2
                    and limits["cpus"] >= 0.2):
                command("docker", "start", indexer["id"])
                next_resume = time.monotonic() + 120
                log.info("indexer_resumed_from_checkpoints")
            if indexer.get("state") == "running":
                # Checkpoint and stop rather than violate the resource reserve or force a cgroup OOM.
                if limits["memory"] < indexer_ram + 128 * 1024**2 or limits["cpus"] < 0.05:
                    log.warning("indexer_resource_pressure; requesting checkpointed stop")
                    subprocess.run(
                        ["docker", "stop", "--timeout", "60", indexer["id"]],
                        check=True,
                        timeout=75,
                        capture_output=True,
                    )
                    applied = None
                    next_resume = time.monotonic() + 120
                    continue
                memory_limit = limits["memory"]
                desired = (indexer["id"], limits["cpus"], memory_limit // (64 * 1024**2))
                if desired != applied:
                    command(
                        "docker",
                        "update",
                        "--cpus",
                        str(limits["cpus"]),
                        "--memory",
                        str(memory_limit),
                        "--memory-swap",
                        str(memory_limit),
                        indexer["id"],
                    )
                    log.info("indexer_budget cpu=%.2f memory=%d", limits["cpus"], memory_limit)
                    applied = desired
                limits["memory"] = memory_limit
            atomic_json(
                args.directory / "host.json",
                {
                    "updated": time.time(),
                    "containers": containers,
                    "budget": limits,
                    "available_ram": free_ram,
                },
            )
        except Exception:
            log.exception("host_collection_failed")
        time.sleep(10)


if __name__ == "__main__":
    main()
