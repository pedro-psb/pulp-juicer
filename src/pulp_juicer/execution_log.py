"""Write timestamped execution events after the result directory is reserved."""

import io
import logging
import time
from pathlib import Path


class ExecutionLog:
    def __init__(self, execution_id: str):
        self.logger = logging.getLogger(f"pulp_juicer.execution.{execution_id}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.buffer = io.StringIO()
        self.handler = logging.StreamHandler(self.buffer)
        formatter = logging.Formatter(
            "%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
        )
        formatter.converter = time.gmtime
        self.handler.setFormatter(formatter)
        self.logger.addHandler(self.handler)

    def save_to(self, path: Path) -> None:
        """Move buffered planning events into the execution log file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, mode="w", encoding="utf-8")
        file_handler.setFormatter(self.handler.formatter)
        file_handler.stream.write(self.buffer.getvalue())
        self.logger.removeHandler(self.handler)
        self.handler.close()
        self.buffer.close()
        self.logger.addHandler(file_handler)
        self.handler = file_handler

    def close(self) -> None:
        self.logger.removeHandler(self.handler)
        self.handler.close()
        if not self.buffer.closed:
            self.buffer.close()
