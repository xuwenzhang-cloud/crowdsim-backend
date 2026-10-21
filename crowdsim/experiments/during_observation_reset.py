"""Permanent, scoped deletion of class-one execution data; no backup copies."""
from pathlib import Path
import re
import shutil

from .contracts import ContractError

RUNTIME_ROOT = Path(__file__).resolve().parents[2] / "runs"
TERMINAL = {"completed", "failed", "cancelled"}


def validate_reset(experiment, runs, expected_run_ids):
    if not experiment:
        raise LookupError("EXPERIMENT_NOT_FOUND")
    if not experiment.get("duringObservation"):
        raise ContractError("重新运行仅用于事中第一类平行实验。")
    if (not isinstance(expected_run_ids, list) or len(expected_run_ids) > 30
            or any(not isinstance(value, str) for value in expected_run_ids)
            or len(set(expected_run_ids)) != len(expected_run_ids)):
        raise ContractError("请提供当前整组实验的轮次编号。")
    if not runs and experiment["status"] in {"draft", "completed", "completed_with_failures"}:
        return  # A lost reset response can be retried without deleting new runs.
    if set(expected_run_ids) != {run["run_id"] for run in runs}:
        raise ContractError("实验运行记录已变化，请刷新后重新确认清除。")
    if experiment["status"] not in {"completed", "completed_with_failures"} or any(run["status"] not in TERMINAL for run in runs):
        raise ContractError("请先结束本组实验，并等待后台收尾完成，再重新运行。")


def runtime_paths(runs, other_runs):
    root = RUNTIME_ROOT.resolve()
    runtime_ids = {run["runtime_run_id"] for run in runs if run.get("runtime_run_id")}
    if any(run.get("runtime_run_id") in runtime_ids for run in other_runs):
        raise ContractError("运行文件仍被其他实验引用，未清除。")
    paths = []
    for runtime_id in runtime_ids:
        if not isinstance(runtime_id, str) or not re.fullmatch(r"run-[0-9a-f]{32}", runtime_id):
            raise ContractError("运行目录编号无效，未清除。")
        path = root / runtime_id
        if path.is_symlink() or path.resolve().parent != root or path.exists() and not path.is_dir():
            raise ContractError("运行目录不符合清理范围，未清除。")
        paths.append(path)
    return paths


def remove_runtime_files(runs, other_runs):
    # Validate every target first. A failed deletion leaves the old matrix in
    # place, so retrying can finish cleanup rather than starting a new matrix.
    for path in runtime_paths(runs, other_runs):
        if path.exists():
            shutil.rmtree(path)
