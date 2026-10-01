from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import Engine, RowMapping

from store.models import Repo, RunItem, Runs

MARKER_NAME = ".gitcrawl-clone-done"
PROGRESS_NAME = "clone-progress.json"
CLONE_URL_TEMPLATE = "https://github.com/{full_name}.git"
LOW_DISK_THRESHOLD_MB = 2048.0

GitRunner = Callable[[Sequence[str], str], None]


class CloneMode(StrEnum):
    SHALLOW = "shallow"
    FILE_ONLY = "file_only"
    WINDOWED = "windowed"


MODE_FACTORS: dict[CloneMode, float] = {
    CloneMode.SHALLOW: 1.0,
    CloneMode.FILE_ONLY: 0.25,
    CloneMode.WINDOWED: 0.5,
}

MODE_FLAGS: dict[CloneMode, tuple[str, ...]] = {
    CloneMode.SHALLOW: ("--depth", "1"),
    CloneMode.FILE_ONLY: ("--depth", "1", "--no-checkout"),
    CloneMode.WINDOWED: ("--filter=blob:none", "--no-checkout"),
}


@dataclass(frozen=True)
class CloneEstimate:
    repos: int
    estimated_mb: float
    warnings: tuple[str, ...]


@dataclass
class CloneProgress:
    status: str
    total: int
    completed: int
    failed: int
    current: str | None = None
    errors: list[str] = field(default_factory=list)

    def emit(self) -> None:
        return None


@dataclass
class CloneStats:
    requested: int
    completed: int
    skipped: int
    failed: int
    dest_root: str


def parse_mode(value: str) -> CloneMode:
    try:
        return CloneMode(value)
    except ValueError:
        raise ValueError(f"unknown clone mode: {value!r}") from None


def free_disk_mb(path: str | Path = ".") -> float:
    target = Path(path)
    while not target.exists() and target != target.parent:
        target = target.parent
    return shutil.disk_usage(target).free / (1024 * 1024)


def _run_row(engine: Engine, run_id: int) -> RowMapping:
    with engine.connect() as connection:
        row = connection.execute(select(Runs).where(Runs.id == run_id)).mappings().one_or_none()
    if row is None:
        raise KeyError(run_id)
    return row


def _top_items(engine: Engine, run_id: int, limit: int) -> list[RowMapping]:
    if limit <= 0:
        return []
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(
                    RunItem.repo_id,
                    RunItem.full_name,
                    func.coalesce(Repo.size_kb, 0).label("size_kb"),
                )
                .outerjoin(Repo, Repo.id == RunItem.repo_id)
                .where(RunItem.run_id == run_id)
                .order_by(RunItem.stargazers.desc(), RunItem.repo_id)
                .limit(limit)
            ).mappings()
        )


def estimate_clone(
    engine: Engine,
    run_id: int,
    *,
    limit: int,
    mode: CloneMode,
    disk_free_mb: float | None = None,
    low_disk_threshold_mb: float = LOW_DISK_THRESHOLD_MB,
) -> CloneEstimate:
    mode = CloneMode(mode)
    _run_row(engine, run_id)
    items = _top_items(engine, run_id, limit)
    size_kb = sum(int(row["size_kb"] or 0) for row in items)
    estimated_mb = size_kb * MODE_FACTORS[mode] / 1024
    warnings: list[str] = []
    if disk_free_mb is not None and estimated_mb > disk_free_mb - low_disk_threshold_mb:
        warnings.append(
            f"low disk: estimated {estimated_mb:.1f} MB with {disk_free_mb:.1f} MB free "
            f"(reserve {low_disk_threshold_mb:.0f} MB)"
        )
    return CloneEstimate(repos=len(items), estimated_mb=estimated_mb, warnings=tuple(warnings))


def _clone_argv(full_name: str, destination: Path, mode: CloneMode) -> list[str]:
    return [
        "git",
        "clone",
        *MODE_FLAGS[mode],
        CLONE_URL_TEMPLATE.format(full_name=full_name),
        str(destination),
    ]


def _default_git_runner(argv: Sequence[str], cwd: str) -> None:
    subprocess.run(list(argv), cwd=cwd, check=True)


def _error_message(full_name: str, exc: BaseException) -> str:
    return f"{full_name}: {type(exc).__name__}: {exc}"[:300]


def clone_repos(
    engine: Engine,
    run_id: int,
    *,
    limit: int,
    mode: CloneMode,
    dest_root: str = "clones",
    git_runner: Callable[[Sequence[str], str], None] | None = None,
    progress: CloneProgress | None = None,
    disk_free_mb: float | None = None,
) -> CloneStats:
    mode = CloneMode(mode)
    run_row = _run_row(engine, run_id)
    items = _top_items(engine, run_id, limit)
    progress = progress or CloneProgress(status="running", total=0, completed=0, failed=0)
    progress.status = "running"
    progress.total = len(items)
    progress.current = None
    progress.emit()
    stats = CloneStats(
        requested=len(items),
        completed=0,
        skipped=0,
        failed=0,
        dest_root=str(dest_root),
    )
    if not items:
        progress.status = "done"
        progress.emit()
        return stats
    run_dir = Path(dest_root) / run_row["filter_hash"] / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    runner = git_runner or _default_git_runner
    for row in items:
        full_name = str(row["full_name"])
        destination = run_dir / full_name.replace("/", "__")
        progress.current = full_name
        progress.emit()
        if (destination / MARKER_NAME).exists():
            stats.skipped += 1
            progress.completed += 1
            progress.emit()
            continue
        try:
            runner(_clone_argv(full_name, destination, mode), str(destination.parent))
        except Exception as exc:
            stats.failed += 1
            progress.failed += 1
            progress.errors.append(_error_message(full_name, exc))
            shutil.rmtree(destination, ignore_errors=True)
            progress.emit()
            continue
        destination.mkdir(parents=True, exist_ok=True)
        (destination / MARKER_NAME).write_text(
            json.dumps({"full_name": full_name, "mode": mode.value}), encoding="utf-8"
        )
        stats.completed += 1
        progress.completed += 1
        progress.emit()
    progress.current = None
    progress.status = "done"
    progress.emit()
    return stats
