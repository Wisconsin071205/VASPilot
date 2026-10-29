#!/bin/bash
#PBS -N NaOH_opt
#PBS -q short
#PBS -l nodes=1:ppn=12
#PBS -l walltime=02:00:00
#PBS -j oe
#PBS -o NaOH_opt.pbsout
#
# NaOH molecule (gas phase, Gamma-point only) geometry optimisation -- cl9 (PBS/Torque)
# Files written for the run report:
#   NaOH_opt.pbsout   combined stdout/stderr of this script
#   job_info.log      jobid / queue / allocated nodes / MPI ranks / start time / POTCAR datasets
#   qstat_f_start.log scheduler's own view of THIS job right after start
#   qstat_Q_start.log queue load snapshot at job start (+ qstat -n)
#   mem_usage.log     per-rank VmRSS / VmHWM samples every 20 s (peak memory evidence)
#   vasp.time.log     /usr/bin/time -v report (wall clock, max RSS, CPU%)
#   vasp.out          VASP stdout
#   summary.log       runtime, memory peak, ionic steps, convergence, OUTCAR timing

cd $PBS_O_WORKDIR
module load vasp/5.4.4-intel2019

NP=$(wc -l < $PBS_NODEFILE)
QUEUE=$(echo "$PBS_QUEUE" | tr -d ' ')
JOBID=$PBS_JOBID

{
  echo "=== JOB RECORD (cl9, PBS/Torque) ==="
  echo "jobid        : $JOBID"
  echo "jobname      : $PBS_JOBNAME"
  echo "queue        : $QUEUE"
  echo "workdir      : $PBS_O_WORKDIR"
  echo "requested    : nodes=1:ppn=12, walltime=02:00:00"
  echo "allocated    : $(uniq $PBS_NODEFILE | tr '\n' ' ')"
  echo "mpi_ranks    : $NP"
  echo "start        : $(date '+%F %T %Z')"
  echo "start_epoch  : $(date +%s)"
  grep -E "^TITEL" POTCAR | awk '{print "  POTCAR dataset: "$2" "$3" "$4}'
} > job_info.log 2>&1

qstat -f "$JOBID"        >  qstat_f_start.log 2>&1
qstat -Q "$QUEUE"        >  qstat_Q_start.log 2>&1
qstat -n "$JOBID"        >> qstat_Q_start.log 2>&1

START=$(date +%s)
: > mem_usage.log

( /usr/bin/time -v mpirun -np $NP vasp_std > vasp.out 2> vasp.time.log ) &
VASP_PID=$!

while kill -0 $VASP_PID 2>/dev/null; do
  TS=$(date '+%F %T')
  TOT=0
  for p in $(pgrep -u "$USER" -x vasp_std 2>/dev/null); do
    RSS=$(awk '/^VmRSS/{r=$2} END{print r+0}'  /proc/$p/status 2>/dev/null)
    HWM=$(awk '/^VmHWM/{h=$2} END{print h+0}'  /proc/$p/status 2>/dev/null)
    [ -n "$RSS" ] && TOT=$((TOT+RSS))
    echo "$TS rank_pid=$p VmRSS=${RSS}kB VmHWM=${HWM}kB"
  done >> mem_usage.log
  [ "$TOT" -gt 0 ] && echo "$TS SUM_VmRSS=${TOT}kB" >> mem_usage.log
  sleep 20
done

wait $VASP_PID
RC=$?
END=$(date +%s)
WALL=$((END-START))

{
  echo
  echo "=== RUNTIME / MEMORY / CONVERGENCE ==="
  echo "end            : $(date '+%F %T %Z')"
  echo "wall_seconds   : $WALL"
  echo "wall_hours     : $(awk -v s=$WALL 'BEGIN{printf "%.3f", s/3600}')"
  echo "vasp_exit_code : $RC"
  echo "--- /usr/bin/time -v (launcher view) ---"
  grep -E "Elapsed \(wall clock\)|Maximum resident set size|Average resident set size|Percent of CPU this job got" vasp.time.log
  echo "--- peak memory from 20 s sampling of all MPI ranks ---"
  awk '/VmRSS=/{split($3,a,"="); gsub(/kB/,"",a[2]); if(a[2]+0>mx)mx=a[2]+0}
       /SUM_VmRSS=/{split($2,b,"="); gsub(/kB/,"",b[2]); if(b[2]+0>smx)smx=b[2]+0}
       END{printf "peak single-rank VmRSS           = %.1f MB\npeak sum of rank VmRSS (upper bound, shared memory counted per rank) = %.1f MB\n", mx/1024, smx/1024}' mem_usage.log
  echo "--- OUTCAR timing ---"
  grep -E "Total CPU time used|Elapsed time \(sec\)" OUTCAR | tail -3
  echo "--- ionic steps (F= records in OSZICAR) ---"
  grep -c 'F=' OSZICAR
  echo "--- convergence flag ---"
  grep -E "reached required accuracy" OUTCAR | tail -1
  echo "--- last OSZICAR lines ---"
  tail -3 OSZICAR
  echo "--- final geometry (CONTCAR) ---"
  tail -4 CONTCAR 2>/dev/null
} > summary.log 2>&1

cat job_info.log summary.log
