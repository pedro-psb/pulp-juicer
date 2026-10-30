"""Experiment declarations available to the CLI and plan loader."""

from pathlib import Path


def experiments():
    from experiments.maven_pull_through.experiment import EXPERIMENT

    compose = (
        Path(__file__).resolve().parents[1]
        / "experiments"
        / "maven_pull_through"
        / "compose.yaml"
    )
    return {EXPERIMENT.id: (EXPERIMENT, compose)}
