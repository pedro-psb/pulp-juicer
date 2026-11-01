"""Request a collector run and wait for its completion response."""

import argparse
import json
import socket
from collections.abc import Callable


def request(
    command: str,
    collector: str | None,
    socket_path: str,
    timeout_seconds: float,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    payload = {"command": command}
    if collector is not None:
        payload["collector"] = collector
    if command.startswith("wait-"):
        payload["timeout_seconds"] = timeout_seconds
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(
            timeout_seconds + (5 if command.startswith("wait-") else 0)
        )
        connection.connect(socket_path)
        connection.sendall(json.dumps(payload).encode() + b"\n")
        with connection.makefile("rb") as response_file:
            while True:
                line = response_file.readline()
                if not line:
                    raise ConnectionError("Collector closed without a final reply")
                response = json.loads(line)
                if response.get("status") != "progress":
                    break
                if on_progress:
                    on_progress(response)
    if response.get("status") != "finished":
        raise RuntimeError(response.get("message", "Collector did not finish"))
    return response


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("collect", "wait-tasks", "wait-postgres-idle", "finalize")
    )
    parser.add_argument("collector", nargs="?")
    parser.add_argument("--socket", default="/tmp/pulp-juicer-collector.sock")
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()
    if (args.command == "collect") != (args.collector is not None):
        parser.error("collect requires a collector name; finalize takes none")
    try:
        print(
            json.dumps(
                request(
                    args.command,
                    args.collector,
                    args.socket,
                    args.timeout,
                    on_progress=lambda response: print(
                        json.dumps(response), flush=True
                    ),
                )
            ),
            flush=True,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"Collector {args.collector or args.command}: {exc}\n")
