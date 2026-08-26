# 标准 VASP 工作流规范（v1）

> 确定性优先：工作流由显式状态机驱动，模型只能选择工作流、填写受约束参数、解释结果、发起审批；
> 模型不能直接生成 Shell/Python 脚本操作服务器。所有修改必须生成变更预览并经过审批。

## 通用状态机

所有工作流共享同一状态机：

```
PREPARED → VALIDATED → APPROVED → SUBMITTED → RUNNING → FINISHED → PARSED → REVIEWED
    │          │            │           │           │
    └──────────┴────────────┴───────────┴───────────┴──► FAILED / TIMEOUT / REJECTED（进入诊断与恢复）
```

| 状态 | 含义 | 转换条件 |
|---|---|---|
| PREPARED | 目录、输入文件、作业脚本已生成 | 由 workflow_prepare 生成（确定性、可复现） |
| VALIDATED | 输入通过规则校验 | vasp_validate 无 error 级问题 |
| APPROVED | 人类批准提交 | 审批通过（写操作逐项确认） |
| SUBMITTED | Slurm 已接受作业 | sbatch --parsable 返回 job id |
| RUNNING | 调度器报告运行中 | squeue/sacct 确认 |
| FINISHED | 程序正常退出 | completed=true（≠ 科学收敛） |
| PARSED | 结果已结构化提取 | vasp_parse 生成 CalculationManifest |
| REVIEWED | 专家复核通过 | 人工确认科学结论 |

**科学状态与调度状态分离**：RUNNING ≠ 电子收敛正常；FINISHED ≠ 离子收敛；
REVIEWED 才代表科学结论被接受。

## 工作流 1：relax-static（结构优化 → 静态计算）

目的：获得充分弛豫的结构与精确总能。

### 阶段 A — RELAX（离子弛豫）

- INCAR 关键参数：`IBRION=2`（或 1）、`ISIF` 按课题（体积优化 3 / 固定体积 2 / 仅原子 0）、
  `NSW≥100`、`EDIFF=1E-6`、`EDIFFG=-0.01`（力判据，eV/Å）、`PREC=Normal|Accurate`、
  `LREAL=Auto`、`ISMEAR/SIGMA` 按体系（金属 1/0.2，绝缘体默认）。
- 输出：CONTCAR、OUTCAR、vasprun.xml、XDATCAR。
- 通过条件（全部满足）：
  1. 离子循环达到 EDIFFG/EDIFF 判据（ionic_converged=true）；
  2. 电子步未出现 BRMIX/ZBRENT 反复与 NELM 耗尽；
  3. 能量与力轨迹平滑（无发散、无 NaN）；
  4. 原子未明显逃逸晶胞（可选项，依赖结构比对）。

### 阶段 B — STATIC（静态精确计算）

- 结构来源：阶段 A 的 CONTCAR（保留原始 POSCAR 与哈希比对）。
- INCAR：`IBRION=-1`、`NSW=0`、`LCHARG=.TRUE.`（为能带/DOS 保留 CHGCAR）、
  `LAECHG` 按需、其余继承阶段 A 且**只允许白名单修改**（如 ISMEAR 换回 -5、PREC 提高）。
- 通过条件：电子自洽收敛（未达 NELM）、输出 E0/总磁矩正常。

### 计划产物（workflow_prepare --dry-run 必须输出）

```
relax-static/
├── 00_relax/{INCAR,POSCAR,KPOINTS,POTCAR,run.slurm}
├── 01_static/{INCAR,POSCAR←CONTCAR,KPOINTS,POTCAR,run.slurm}
└── plan.json            # 计划清单：目录、文件哈希、参数来源、依赖关系
```

## 工作流 2：static-band-dos（静态计算 → 能带和 DOS）

目的：基于收敛结构获得电子结构。

### 前置

- 一次已收敛的静态计算；CHGCAR 已保留（阶段 B 要求 LCHARG=.TRUE.）。
- 若缺少 CHGCAR，先执行 band-dos 专用 SCF（ICHARG=2 自洽）再读。

### 阶段 C — BAND（能带）

- KPOINTS：Line-mode，高对称路径（源自结构对称性分析；禁止模型臆造路径，必须引用确定的
  高对称点列表，例如 pymatgen 的 HighSymmKpath 结果）。
- INCAR：`ICHARG=11`（读 CHGCAR）、`ISMEAR=0`、`SIGMA` 按体系、
  `NBANDS` 若需多一倍（默认不增）、`LORBIT` 按需。
- 输出：EIGENVAL、vasprun.xml。

### 阶段 D — DOS（态密度）

- KPOINTS：Γ 中心加密网格（如 16×16×16 或更高，随体系大小）。
- INCAR：`ICHARG=11`、`ISMEAR=-5`（四面体）或 0、`EMIN/EMAX/NEDOS` 按带宽度。
- 输出：DOSCAR、vasprun.xml。

### 通过条件

- 两个阶段电子收敛；费米能级、带隙（若有）数值稳定；
- 能带路径点与 DOS 网格记录在 manifest。

## 工作流 3：convergence-scan（ENCUT / KPOINTS 收敛测试）

目的：给出参数-能量/力收敛曲线，为后续生产计算提供依据。

### 变体 A — ENCUT 扫描

- 固定 KPOINTS（先按体系粗设，如 6×6×6），ENCUT 从 300 eV 起，步长 50 eV，
  扫描至能量变化 < 1 meV/atom 后继续 2 个点确认（上限 1.5×POTCAR ENMAX）。
- 每点：单次静态（IBRION=-1, NSW=0），记录 E0。

### 变体 B — K 网格扫描

- 固定 ENCUT（已收敛值），网格 2×2×2 → 4×4×4 → 6×6×6 → …（或 KSPACING 0.5→0.1）。
- 每点：单次静态，记录 E0 与总力。

### 判定

- 能量判据：相邻两点 ΔE < 1 meV/atom 且单调趋平。
- 力判据（可选）：力随网格加密稳定。
- 输出 convergence.csv + 每点 CalculationManifest。

## 确定性原则（对所有工作流生效）

1. **同输入同计划**：相同输入文件与配置，workflow_prepare 必须生成逐字节相同的
   plan.json（时间戳只进日志，不进计划文件）。
2. **参数白名单**：每个工作流显式列出允许修改的 INCAR/KPOINTS 字段，白名单外一律拒绝。
3. **三级修复策略**：
   - L1 只检测（默认）：输出"检测结果—建议修改—证据"，不改任何文件；
   - L2 生成补丁：diff 预览 + 人工审批后应用；
   - L3 自动修复：仅限经过历史案例验证的白名单规则（初始为空集）。
4. **变更即记录**：每次输入修改追加到 manifest.modifications（时间、字段、前后值、理由、审批人）。
5. **删除即隔离**：任何删除先移入 .vaspilot-trash；永久删除用 purge 另行审批。
6. **原始文件不可变**：POTCAR/WAVECAR/CHGCAR 只读使用；替换必须保留旧文件哈希于 manifest。
