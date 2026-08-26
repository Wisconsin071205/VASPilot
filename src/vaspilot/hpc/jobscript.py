"""Deterministic Slurm/PBS job script rendering from constrained parameters."""

from __future__ import annotations

import re

from ..core.errors import ValidationError

WALLTIME_RE = re.compile(r"^\d{1,5}(:\d{1,2}){0,2}$")
JOB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
PARTITION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
VASP_STDIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]{0,15}$")


def _validate(*, job_name: str, partition: str, ntasks: int, walltime: str,
              vasp_executable: str) -> None:
    if not JOB_NAME_RE.fullmatch(job_name):
        raise ValidationError(f"job name {job_name!r} is invalid")
    if partition and not PARTITION_RE.fullmatch(partition):
        raise ValidationError(f"partition {partition!r} is invalid")
    if not isinstance(ntasks, int) or not 1 <= ntasks <= 4096:
        raise ValidationError("ntasks must be an integer in 1..4096")
    if not WALLTIME_RE.fullmatch(walltime):
        raise ValidationError(
            f"walltime {walltime!r} must look like HH:MM:SS / MM:SS / minutes")
    if not VASP_STDIN_RE.fullmatch(vasp_executable):
        raise ValidationError("vasp_executable must be a simple command name")


def render_job_script(*, scheduler: str, job_name: str, partition: str = "",
                      ntasks: int = 8, walltime: str = "24:00:00",
                      vasp_executable: str = "vasp_std",
                      extra_modules: tuple[str, ...] = ()) -> str:
    """Render one deterministic job script. No shell string from the model
    is ever embedded: every value passes the regex gates above."""
    _validate(job_name=job_name, partition=partition, ntasks=ntasks,
              walltime=walltime, vasp_executable=vasp_executable)
    for module in extra_modules:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+/-]{0,63}", module):
            raise ValidationError(f"module name {module!r} is invalid")

    if scheduler == "slurm":
        lines = [
            "#!/bin/bash",
            f"#SBATCH --job-name={job_name}",
        ]
        if partition:
            lines.append(f"#SBATCH --partition={partition}")
        lines += [
            f"#SBATCH --ntasks={ntasks}",
            f"#SBATCH --time={walltime}",
            "#SBATCH --output=vaspilot-%j.out",
            "#SBATCH --error=vaspilot-%j.err",
        ]
        for module in extra_modules:
            lines.append(f"module load {module}")
        lines += [
            "cd \"$SLURM_SUBMIT_DIR\"",
            f"srun --mpi=pmi2 {vasp_executable} > vasp.out 2>&1",
            "",
        ]
        return "\n".join(lines)

    if scheduler == "pbs":
        lines = [
            "#!/bin/bash",
            f"#PBS -N {job_name}",
        ]
        if partition:
            lines.append(f"#PBS -q {partition}")
        lines += [
            f"#PBS -l select=1:mpiprocs={ntasks}",
            f"#PBS -l walltime={walltime}",
            "#PBS -j oe",
            "#PBS -o vaspilot-$PBS_JOBID.out",
        ]
        for module in extra_modules:
            lines.append(f"module load {module}")
        lines += [
            "cd \"$PBS_O_WORKDIR\"",
            f"mpirun -np {ntasks} {vasp_executable} > vasp.out 2>&1",
            "",
        ]
        return "\n".join(lines)

    raise ValidationError(f"unsupported scheduler {scheduler!r}")
