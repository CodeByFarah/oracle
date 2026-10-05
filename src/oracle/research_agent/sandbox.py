"""Isolated execution of AI-generated research code.

Two independent layers:
1. Static policy (`check_code`): an AST scan rejecting imports outside an allowlist, dunder
   access, and builtins that reach the filesystem, network or interpreter. This is defence in
   depth, NOT a security boundary.
2. Container isolation (`DockerSandbox`): no network, read-only root filesystem, every Linux
   capability dropped, no privilege escalation, non-root user, memory/CPU/PID limits and a hard
   timeout. Inputs are mounted read-only; only an output directory is writable.

If Docker is unavailable the sandbox refuses to run. There is no weaker fallback.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import numpy as np

from oracle.features.library import F64, SymbolSeries

ALLOWED_IMPORTS = frozenset({"math", "numpy"})
FORBIDDEN_NAMES = frozenset(
    {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
        "input",
        "breakpoint",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "memoryview",
        "help",
        "exit",
        "quit",
    }
)
SANDBOX_IMAGE = "oracle-sandbox:1"


class PolicyViolationError(ValueError):
    pass


class SandboxUnavailableError(RuntimeError):
    pass


class SandboxExecutionError(RuntimeError):
    pass


class _PolicyVisitor(ast.NodeVisitor):
    """Scope-aware checks. A forbidden builtin name is accepted only where a function
    parameter of an enclosing scope rebinds it (the contract is compute(open, ...))."""

    def __init__(self) -> None:
        self.scopes: list[set[str]] = [set()]
        self.defines_compute = False

    def _function(self, node: ast.FunctionDef | ast.Lambda) -> None:
        a = node.args
        params = {x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
        params |= {x.arg for x in (a.vararg, a.kwarg) if x is not None}
        if isinstance(node, ast.FunctionDef) and node.name == "compute":
            self.defines_compute = True
        self.scopes.append(params)
        self.generic_visit(node)
        self.scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._function(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.split(".")[0] not in ALLOWED_IMPORTS:
                raise PolicyViolationError(f"import of {alias.name!r} is not allowed")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
            raise PolicyViolationError(f"import from {node.module!r} is not allowed")

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in FORBIDDEN_NAMES and not any(node.id in scope for scope in self.scopes):
            raise PolicyViolationError(f"use of {node.id!r} is not allowed")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__"):
            raise PolicyViolationError(f"dunder attribute {node.attr!r} is not allowed")
        self.generic_visit(node)


def check_code(source: str) -> None:
    """Raise PolicyViolationError if `source` breaks the static policy."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise PolicyViolationError(f"syntax error: {exc.msg}") from exc
    visitor = _PolicyVisitor()
    visitor.visit(tree)
    if not visitor.defines_compute:
        raise PolicyViolationError("code must define compute(open, high, low, close, volume)")


@dataclass(frozen=True)
class SandboxLimits:
    timeout_s: float = 60.0
    memory: str = "512m"
    cpus: str = "1"
    pids: int = 64


def docker_available(image: str = SANDBOX_IMAGE) -> bool:
    """True when docker is installed, the daemon answers and the sandbox image exists."""
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


class DockerSandbox:
    def __init__(self, limits: SandboxLimits | None = None, image: str = SANDBOX_IMAGE) -> None:
        self.limits = limits or SandboxLimits()
        self.image = image

    def command(self, in_dir: Path, out_dir: Path, name: str) -> list[str]:
        """The exact docker invocation (exposed so tests can assert every isolation flag)."""
        lim = self.limits
        return [
            "docker", "run", "--rm", "--name", name,
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", "10001:10001",
            "--memory", lim.memory,
            "--memory-swap", lim.memory,
            "--cpus", lim.cpus,
            "--pids-limit", str(lim.pids),
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",  # noqa: S108 - path inside the container
            "-v", f"{in_dir.resolve()}:/in:ro",
            "-v", f"{out_dir.resolve()}:/out:rw",
            self.image, "python", "-I", "/in/runner.py",
        ]  # fmt: skip

    def run(self, in_dir: Path, out_dir: Path) -> None:
        if shutil.which("docker") is None:
            raise SandboxUnavailableError("docker is not installed; refusing to run untrusted code")
        name = f"oracle-sbx-{uuid.uuid4().hex[:12]}"
        try:
            proc = subprocess.run(
                self.command(in_dir, out_dir, name),
                capture_output=True,
                text=True,
                timeout=self.limits.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=30)
            raise SandboxExecutionError(
                f"sandbox timed out after {self.limits.timeout_s}s"
            ) from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout)[-2000:]
            raise SandboxExecutionError(f"sandbox exited with {proc.returncode}: {tail}")


@dataclass(frozen=True)
class GeneratedFeatureReport:
    values: F64
    leakage_violations: tuple[str, ...]

    @property
    def causal(self) -> bool:
        return not self.leakage_violations


def _same(a: F64, b: F64) -> bool:
    return bool(np.all((np.isnan(a) & np.isnan(b)) | np.isclose(a, b, rtol=1e-12, atol=0.0)))


def evaluate_generated_feature(
    source: str,
    series: SymbolSeries,
    cut_points: list[int],
    sandbox: DockerSandbox,
) -> GeneratedFeatureReport:
    """Policy-check, run in the sandbox, then apply truncation and scrambling probes."""
    check_code(source)
    package = resources.files("oracle.research_agent")
    runner = package.joinpath("sandbox_runner.py").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="oracle-sbx-") as tmp:
        in_dir, out_dir = Path(tmp, "in"), Path(tmp, "out")
        in_dir.mkdir()
        out_dir.mkdir()
        (in_dir / "job.py").write_text(source, encoding="utf-8")
        (in_dir / "runner.py").write_text(runner, encoding="utf-8")
        np.savez(
            in_dir / "input.npz",
            open=series.open,
            high=series.high,
            low=series.low,
            close=series.close,
            volume=series.volume,
            cuts=np.asarray(cut_points, dtype=np.int64),
        )
        out_dir.chmod(0o777)  # the container runs as an unprivileged uid
        sandbox.run(in_dir, out_dir)
        with np.load(out_dir / "result.npz") as result:
            full = np.array(result["full"], dtype=np.float64)
            violations = []
            for t in cut_points:
                if not _same(np.array(result[f"trunc_{t}"])[t : t + 1], full[t : t + 1]):
                    violations.append(f"truncation changed the value at t={t}")
                if not _same(np.array(result[f"scram_{t}"])[: t + 1], full[: t + 1]):
                    violations.append(f"future data changed values at or before t={t}")
    return GeneratedFeatureReport(values=full, leakage_violations=tuple(violations))
