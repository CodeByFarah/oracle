"""Time-ordered splits that prevent overlap leakage.

- Chronological discovery / validation / holdout partition by timestamp, purging samples
  whose label window [t, t + horizon] crosses into the next partition.
- Purged walk-forward folds: training samples whose label window reaches the test period are
  removed; an optional embargo drops training samples just after a test block.
- The holdout is sealed: reading it requires a reason and calls an audit hook.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

I64 = npt.NDArray[np.int64]
B = npt.NDArray[np.bool_]


@dataclass(frozen=True)
class ChronologicalSplit:
    discovery_end_ns: int  # exclusive
    validation_end_ns: int  # exclusive; holdout is everything at or after this

    def masks(self, timestamps: I64, label_horizon_ns: int) -> tuple[B, B, B]:
        label_end = timestamps + label_horizon_ns
        discovery = label_end < self.discovery_end_ns
        validation = (timestamps >= self.discovery_end_ns) & (label_end < self.validation_end_ns)
        holdout = timestamps >= self.validation_end_ns
        return discovery, validation, holdout


def chronological_split(
    timestamps: I64, discovery_fraction: float, validation_fraction: float
) -> ChronologicalSplit:
    if not 0 < discovery_fraction < 1 or not 0 <= validation_fraction < 1 - discovery_fraction:
        raise ValueError("fractions must satisfy 0 < discovery and discovery + validation < 1")
    unique = np.unique(timestamps)
    d_end = unique[int(len(unique) * discovery_fraction)]
    v_index = min(int(len(unique) * (discovery_fraction + validation_fraction)), len(unique) - 1)
    return ChronologicalSplit(int(d_end), int(unique[v_index]))


@dataclass(frozen=True)
class Fold:
    train: I64
    test: I64


def purged_walk_forward(
    timestamps: I64,
    *,
    n_folds: int,
    label_horizon_ns: int,
    embargo_ns: int = 0,
    min_train_fraction: float = 0.3,
) -> list[Fold]:
    """Expanding-window folds: train on the past, test on the next block, roll forward."""
    unique = np.unique(timestamps)
    start = int(len(unique) * min_train_fraction)
    edges = np.linspace(start, len(unique), n_folds + 1).astype(int)
    folds = []
    for k in range(n_folds):
        test_start = unique[edges[k]]
        test_end = unique[edges[k + 1] - 1]
        test = np.flatnonzero((timestamps >= test_start) & (timestamps <= test_end))
        train_mask = timestamps + label_horizon_ns < test_start
        if embargo_ns:
            train_mask &= ~((timestamps > test_end) & (timestamps <= test_end + embargo_ns))
        folds.append(Fold(np.flatnonzero(train_mask), test))
    return folds


class SealedHoldout:
    """Holdout rows that can only be read through `open`, which records who and why."""

    def __init__(self, mask: B, audit: Callable[[str, str], None]) -> None:
        self._mask = mask
        self._audit = audit
        self.opened = 0

    def open(self, *, actor: str, reason: str) -> B:
        if not reason.strip():
            raise ValueError("opening the holdout requires a reason")
        self._audit(actor, reason)
        self.opened += 1
        return self._mask
