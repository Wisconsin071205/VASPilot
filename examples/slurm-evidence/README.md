# VASP 小型 Slurm 验证算例

这套算例用于快速验证 VASP、MPI、Slurm 和 rsci 的整条链路，也便于采集比赛要求的真实集群运行证据。三个任务都很小，适合功能演示；它们不是已经完成收敛测试的科研生产参数。

| 目录 | 任务 | 原子数 | 主要验证点 | 建议时限 |
| --- | --- | ---: | --- | ---: |
| `01_si_static` | 金刚石 Si 静态自洽计算 | 2 | 最短 VASP/Slurm 冒烟测试 | 10 分钟 |
| `02_al_relax` | fcc Al 晶格与离子弛豫 | 1 | 几何优化、CONTCAR 生成 | 15 分钟 |
| `03_fe_relax_spin` | bcc Fe 自旋极化弛豫 | 1 | ISPIN/MAGMOM 与磁性计算 | 20 分钟 |

## 为什么目录中没有直接附带 POTCAR

VASP 的 PAW 数据受许可证约束，不能随项目公开分发。每个算例都提供了 `prepare_potcar.sh`，它只从你有权访问的集群赝势库复制对应 POTCAR，不会把内容打印到日志。

集群默认路径为 `/public/sourcecode/vasp/potpaw_PBE.64`。如果实际路径不同，请设置环境变量：

```bash
export VASP_PSP_DIR=/你的/potpaw_PBE.64
```

## 最短运行流程

以 Si 静态计算为例：

```bash
cd 01_si_static
bash prepare_potcar.sh
rsci workflow validate . --local
sbatch run.slurm
```

查询作业：

```bash
squeue -u "$USER"
sacct -j JOB_ID --format=JobID,JobName,Partition,State,Elapsed,AllocCPUS,MaxRSS,ExitCode
tail -f rsci-JOB_ID.out
```

作业结束后采集证据（从任一算例目录执行）：

```bash
bash ../collect_evidence.sh JOB_ID .
```

脚本会创建 `evidence-JOB_ID-时间/`，保存作业详情、排队/记账数据、关键输出尾部、输入文件哈希以及运行截图建议。它不会复制或显示 POTCAR 内容。

## 集群差异

- 默认分区是 `normal`。如分区名不同，修改 `#SBATCH --partition=normal`。
- 默认模块是 `vasp/5.4.4-oneapi-2024.1`。可提交时覆盖：

  ```bash
  sbatch --export=ALL,VASP_MODULE=你的模块名 run.slurm
  ```

- 脚本与 rsci 当前约定一致，使用 `srun --mpi=pmi2`。如果管理员明确要求 `mpirun`，把运行行改为：

  ```bash
  mpirun -np "$SLURM_NTASKS" "$VASP_EXEC" > vasp.out 2>&1
  ```

- 正式科研结果必须对 `ENCUT`、K 点、超胞尺寸和收敛阈值做独立收敛测试。

