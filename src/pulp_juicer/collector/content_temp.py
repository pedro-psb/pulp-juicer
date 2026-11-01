"""Sample the footprint of Pulp's shared WORKING_DIRECTORY.

active_files/active_bytes include files anywhere below the directory, including
task-worker subdirectories. root_files/root_bytes include only files directly
under it, where content downloads commonly stage their NamedTemporaryFile.
These are shared-path counts, not proof that a particular content worker owns
the files; API uploads and tasks can also use this directory. Byte sizes use
st_size (apparent length); allocated_bytes uses st_blocks * 512. peak_bytes is
the largest active_bytes observed by this one-second sampler, so a file that
appears and disappears between samples will not contribute to the peak.
"""

import os
import stat

if __package__:
    from .components import PulpComponents
else:
    from components import PulpComponents


class PulpTempSampler:
    """Measure files in the shared Pulp working tree at each sample instant."""

    def __init__(self, components: PulpComponents):
        self.components = components
        self.peak_bytes = 0

    def sample(self) -> dict[str, int]:
        """Count live files in Pulp's working tree and retain sampled peak bytes."""
        active_files = active_bytes = allocated_bytes = 0
        root_files = root_bytes = 0
        pending = [(self.components.working_directory, True)]
        while pending:
            directory, is_root = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except FileNotFoundError:
                continue
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    pending.append((entry.path, False))
                elif stat.S_ISREG(info.st_mode):
                    active_files += 1
                    active_bytes += info.st_size
                    allocated_bytes += info.st_blocks * 512
                    if is_root:
                        root_files += 1
                        root_bytes += info.st_size
        self.peak_bytes = max(self.peak_bytes, active_bytes)
        return {
            "active_files": active_files,
            "active_bytes": active_bytes,
            "allocated_bytes": allocated_bytes,
            "root_files": root_files,
            "root_bytes": root_bytes,
            "peak_bytes": self.peak_bytes,
        }
