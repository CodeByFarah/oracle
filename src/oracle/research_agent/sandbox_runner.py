"""Runs INSIDE the sandbox container. Depends only on numpy and the standard library.

Contract:
- /in/job.py defines `compute(open, high, low, close, volume) -> sequence of floats`;
- /in/input.npz holds the five float64 arrays and an int array `cuts`;
- writes /out/result.npz with `full` plus `trunc_<t>` and `scram_<t>` arrays for each cut,
  so the host can run the truncation and future-scrambling leakage checks itself.
"""

import importlib.util
import sys
from collections.abc import Callable
from typing import Any

import numpy as np

FIELDS = ("open", "high", "low", "close", "volume")
Compute = Callable[..., Any]


def _load(path: str) -> Compute:
    spec = importlib.util.spec_from_file_location("job", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load job")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    compute: Compute = module.compute
    return compute


def _run(compute: Compute, arrays: dict[str, Any]) -> Any:
    out = np.asarray(compute(*(arrays[f] for f in FIELDS)), dtype=np.float64)
    if out.shape != arrays["close"].shape:
        raise ValueError(f"compute returned shape {out.shape}, expected {arrays['close'].shape}")
    return out


def main() -> int:
    data = np.load("/in/input.npz")
    arrays = {f: np.array(data[f], dtype=np.float64) for f in FIELDS}
    compute = _load("/in/job.py")
    results = {"full": _run(compute, arrays)}
    rng = np.random.default_rng(0)
    for t in data["cuts"].tolist():
        results[f"trunc_{t}"] = _run(compute, {f: v[: t + 1].copy() for f, v in arrays.items()})
        scrambled = {f: v.copy() for f, v in arrays.items()}
        tail = len(arrays["close"]) - t - 1
        if tail > 0:
            factor = np.exp(rng.normal(0, 0.05, tail))
            for f in FIELDS:
                scrambled[f][t + 1 :] = scrambled[f][t + 1 :][::-1] * factor
        results[f"scram_{t}"] = _run(compute, scrambled)
    np.savez("/out/result.npz", **results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
