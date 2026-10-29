"""Start and stop the PostgreSQL CPU probe with a Locust test."""

import csv
import select
import shlex
import subprocess
from pathlib import Path


class DBCpuMonitor:
    def __init__(self, output, compose, project, compose_file="compose.yaml"):
        self.output = Path(output)
        self.compose = shlex.split(compose)
        self.project = project
        self.compose_file = compose_file
        self.process = None
        self.output_file = None

    def _expect(self, message):
        ready, _, _ = select.select([self.process.stderr], [], [], 10)
        if not ready:
            raise RuntimeError(f"Timed out waiting for DB CPU probe {message}")
        response = self.process.stderr.readline().strip()
        if response != message:
            raise RuntimeError(f"DB CPU probe expected {message}, got {response or 'EOF'}")

    def start(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output_file = self.output.open("w", newline="")
        probe = Path(__file__).with_name("db_cpu_probe.py").read_text()
        command = [
            *self.compose,
            "-p", self.project,
            "-f", self.compose_file,
            "exec", "-T", "pulp",
            "python3", "-u", "-c", probe,
        ]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=self.output_file,
                stderr=subprocess.PIPE,
                text=True,
            )
            self._expect("READY")
            self.process.stdin.write("start\n")
            self.process.stdin.flush()
            self._expect("STARTED")
        except Exception:
            self.close()
            raise

    def stop(self):
        if self.process is None:
            return
        try:
            self.process.stdin.write("stop\n")
            self.process.stdin.flush()
            self.process.wait(timeout=10)
            error = self.process.stderr.read().strip()
            if self.process.returncode:
                raise RuntimeError(f"DB CPU probe failed: {error or self.process.returncode}")
        finally:
            self.close()
        self.summarize()

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            for stream in (self.process.stdin, self.process.stderr):
                if stream is not None:
                    stream.close()
            self.process = None
        if self.output_file is not None:
            self.output_file.close()
            self.output_file = None

    def summarize(self):
        with self.output.open(newline="") as source:
            rows = list(csv.DictReader(source))
        if not rows:
            raise RuntimeError("DB CPU probe produced no samples")

        duration = float(rows[-1]["elapsed_s"])
        cpu_seconds = sum(float(row["postgres_cpu_seconds"]) for row in rows)
        peak = max(float(row["postgres_cpu_pct"]) for row in rows)
        summary = self.output.with_name("db_cpu_summary.csv")
        with summary.open("w", newline="") as destination:
            writer = csv.writer(destination)
            writer.writerow(("phase", "duration_s", "postgres_cpu_seconds", "postgres_cpu_mean_pct", "postgres_cpu_peak_pct", "samples"))
            writer.writerow(
                ("requests", f"{duration:.3f}", f"{cpu_seconds:.4f}", f"{cpu_seconds / duration * 100:.2f}" if duration else "0.00", f"{peak:.2f}", len(rows))
            )
