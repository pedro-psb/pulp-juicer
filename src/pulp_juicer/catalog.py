"""Experiment declarations available to the CLI and plan loader."""

from pathlib import Path


def experiments():
    from experiments.bucket_repository.experiment import EXPERIMENT

    compose = (
        Path(__file__).resolve().parents[1]
        / "experiments"
        / "bucket_repository"
        / "compose.yaml"
    )
    return {EXPERIMENT.id: (EXPERIMENT, compose)}
