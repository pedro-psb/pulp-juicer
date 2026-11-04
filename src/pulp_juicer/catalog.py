"""Experiment declarations available to the CLI and plan loader."""

from pathlib import Path


def experiments():
    from experiments.bucket_repository.experiment import EXPERIMENT
    from experiments.version_retention.experiment import EXPERIMENT as RETENTION

    compose = (
        Path(__file__).resolve().parents[1]
        / "experiments"
        / "bucket_repository"
        / "compose.yaml"
    )
    retention_compose = compose.parent.parent / "version_retention" / "compose.yaml"
    return {
        EXPERIMENT.id: (EXPERIMENT, compose),
        RETENTION.id: (RETENTION, retention_compose),
    }
