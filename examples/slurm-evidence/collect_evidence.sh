#!/usr/bin/env bash
set -u

if [ "$#" -ne 2 ]; then
  echo "Usage: bash collect_evidence.sh JOB_ID CASE_DIRECTORY" >&2
  exit 2
fi

job_id="$1"
case_dir="$2"

if ! [[ "$job_id" =~ ^[0-9]+([_.][0-9]+)?$ ]]; then
  echo "JOB_ID format is invalid: $job_id" >&2
  exit 2
fi

if [ ! -d "$case_dir" ]; then
  echo "Case directory does not exist: $case_dir" >&2
  exit 2
fi

case_dir="$(cd "$case_dir" && pwd)"
stamp="$(date +%Y%m%d-%H%M%S)"
evidence_dir="$case_dir/evidence-$job_id-$stamp"
mkdir -p "$evidence_dir"

{
  echo "Collected at: $(date --iso-8601=seconds 2>/dev/null || date)"
  echo "Collector host: $(hostname)"
  echo "User: ${USER:-unknown}"
  echo "Job ID: $job_id"
  echo "Case directory: $case_dir"
  echo "Slurm version: $(sinfo --version 2>/dev/null || echo unavailable)"
} > "$evidence_dir/00_environment.txt"

squeue -j "$job_id" -o "%.18i %.24j %.10P %.10T %.10M %.10l %.6D %R" \
  > "$evidence_dir/01_squeue.txt" 2>&1 || true
scontrol show job "$job_id" \
  > "$evidence_dir/02_scontrol_job.txt" 2>&1 || true
sacct -j "$job_id" \
  --format=JobID,JobName,Partition,State,Elapsed,Timelimit,AllocCPUS,ReqMem,MaxRSS,MaxVMSize,ExitCode,NodeList \
  > "$evidence_dir/03_sacct.txt" 2>&1 || true
sstat -j "${job_id}.batch" \
  --format=JobID,AveCPU,AveRSS,MaxRSS,AveVMSize,MaxVMSize \
  > "$evidence_dir/04_sstat.txt" 2>&1 || true

if command -v seff >/dev/null 2>&1; then
  seff "$job_id" > "$evidence_dir/05_seff.txt" 2>&1 || true
fi

for output_file in "vasploom-$job_id.out" "vasploom-$job_id.err" vasp.out OSZICAR OUTCAR; do
  if [ -f "$case_dir/$output_file" ]; then
    tail -n 200 "$case_dir/$output_file" \
      > "$evidence_dir/$(basename "$output_file").tail.txt"
  fi
done

if [ -f "$case_dir/OUTCAR" ]; then
  grep -E "(Elapsed time|Maximum memory used|Total CPU time used|reached required accuracy|General timing and accounting|free  energy|magnetization)" \
    "$case_dir/OUTCAR" > "$evidence_dir/06_outcar_key_lines.txt" || true
fi

(
  cd "$case_dir" || exit 1
  for input_file in INCAR KPOINTS POSCAR POTCAR run.slurm; do
    if [ -f "$input_file" ]; then
      sha256sum "$input_file"
    fi
  done
) > "$evidence_dir/07_input_sha256.txt"

cat > "$evidence_dir/08_screenshot_checklist.txt" <<'EOF'
建议另行截图并与本目录一起归档：
1. sbatch 返回 Job ID 的终端画面。
2. squeue 中 PENDING/RUNNING 状态及等待原因或节点名。
3. VaspLoom 作业页面中的服务器、目录、状态、运行时长和自动分析结果。
4. 作业结束后的 sacct 或 seff 资源统计。
5. OUTCAR/OSZICAR 与结构结果，注意不要展示密码、TOTP、私钥或 API Key。
EOF

echo "Evidence collected in: $evidence_dir"

