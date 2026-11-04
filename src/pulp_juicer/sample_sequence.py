"""Build balanced, deterministic candidate sampling orders."""


def sample_sequence(n: int, candidates: tuple[int, ...]) -> tuple[int, ...]:
    """Sample every candidate n times, reversing alternate rounds."""
    if type(n) is not int or n < 1:
        raise ValueError("Sample count must be a positive integer")
    if not candidates or len(set(candidates)) != len(candidates):
        raise ValueError("Candidates must be a nonempty tuple of unique IDs")
    return tuple(
        candidate
        for index in range(n)
        for candidate in (candidates if index % 2 == 0 else candidates[::-1])
    )
