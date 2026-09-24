#!/bin/bash
#SBATCH --job-name=al-relax
#SBATCH --partition=normal
#SBATCH --nodes=1
#SBATCH --ntasks=8
#SBATCH --time=00:15:00
#SBATCH --mem=4G
#SBATCH --output=rsci-%j.out
#SBATCH --error=rsci-%j.err

set -u
cd "${SLURM_SUBMIT_DIR:-$PWD}"

for required_file in INCAR KPOINTS POSCAR POTCAR; do
  if [ ! -s "$required_file" ]; then
    echo "Missing or empty input: $required_file" >&2
    exit 2
  fi
done

VASP_MODULE="${VASP_MODULE:-vasp/5.4.4-oneapi-2024.1}"
VASP_EXEC="${VASP_EXEC:-vasp_std}"
export OMP_NUM_THREADS=1
ulimit -s unlimited

module load "$VASP_MODULE"
if ! command -v "$VASP_EXEC" >/dev/null 2>&1; then
  echo "VASP executable not found after loading module: $VASP_EXEC" >&2
  exit 3
fi

echo "Start time: $(date --iso-8601=seconds 2>/dev/null || date)"
echo "Job ID: $SLURM_JOB_ID"
echo "Job name: $SLURM_JOB_NAME"
echo "Submit directory: $PWD"
echo "Node list: $SLURM_JOB_NODELIST"
echo "MPI tasks: $SLURM_NTASKS"
echo "VASP module: $VASP_MODULE"
echo "VASP executable: $(command -v "$VASP_EXEC")"

srun --mpi=pmi2 "$VASP_EXEC" > vasp.out 2>&1
status=$?

echo "End time: $(date --iso-8601=seconds 2>/dev/null || date)"
echo "VASP exit code: $status"
exit "$status"

