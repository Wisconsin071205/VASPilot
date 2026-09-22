# VASPKIT 接入：从一个 .vasp 文件到一条跑完的计算链

日期：2026-09-23 · 状态：待批准 · 方案：A（一次批准整条链，每阶段远端生成后自检）

## 1. 目标与边界

你把 VESTA 导出的 `.vasp` 文件交给智能体，在文件末尾用中文写清要算什么，
系统自动完成：结构体检 → 在 HPC 上用 VASPKIT 生成 `INCAR`/`KPOINTS`/`POTCAR`
→ 渲染作业脚本 → 提交 → 按阶段依赖串起整条链 → 在「作业」标签里持续跟踪
调度器状态与科学收敛。

第一版覆盖四个阶段：**结构优化、静态自洽、能带、态密度**。

不在本次范围内：光学/介电、弹性常数、声子、过渡态；VASPKIT 的后处理绘图
任务（`task 21x` 系列）；本地运行 VASPKIT；多结构批量投递；结果可视化。

## 2. 关键决策与理由

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| VASPKIT 执行位置 | HPC 服务器 | 赝势库在服务器上，POTCAR 生成必须就地；同时维持项目既有的「POTCAR 内容永不落到本地」策略 |
| 批准粒度 | 一次批准整条链（方案 A） | 02–04 阶段的输入文件在批准时尚不存在（依赖前序阶段产物），逐段批准等于放弃无人值守。被否：方案 B（逐段取回过目）太啰嗦；方案 C（首段过目其余自动）保留为退让选项 |
| 批准绑定对象 | `campaign_hash`（配方 + 阶段图 + INCAR 断言 + 服务器 + 远端根 + 作业参数） | 文件哈希不可得，改为锁死**意图**；失去的文件级保证由第 6 节的运行时自检补回 |
| 自然语言的位置 | 只用于「中文标注 → 配方 JSON」一步 | 一切会真正动服务器的决定由确定性代码做出；模型输出必须过 schema 与范围校验 |
| 阶段执行 | 每阶段 = 一个 plan + 一次 run，复用 `WorkflowEngine` | 上传校验、作业监控、失败重试、审计全部复用，不重写 |
| 依赖判据 | 科学收敛（`scientific_status`），而非调度器 COMPLETED | 拿未收敛结构去算能带不会报错，但结果是废的——这是自动链最危险的失效模式 |
| VASPKIT 调用方式 | 由 `vaspkit_doctor` 探测后选路，不写死 | 版本间 `-task 101` 是否需要 stdin 喂菜单、`~/.vaspkit` 键名均有差异 |
| CHGCAR 传递 | 服务器内拷贝（`remote_copy`） | 软链接在部分并行文件系统上跨节点不可靠 |

## 3. 架构

```
Fe2O3.vasp  (VESTA 导出 + 末尾中文标注)
   │
   ├─ split_annotation()        纯函数：按原子数切出 POSCAR 与标注文本
   ├─ normalize_poscar()        体检 + 规范化，产出结构摘要
   ├─ parse_annotation(model)   唯一的模型调用：中文 → recipe JSON
   ├─ validate_recipe()         schema + 范围校验，不合法即拒
   │                            ↓ 配方摊开给人修改
   ├─ build_campaign()          配方 → 阶段图 + 各阶段 INCAR 断言
   │                            ↓ campaign_hash
   ├─ 人工批准（本地，一次）      HMAC 绑定 campaign_hash
   │
   └─ CampaignRunner            逐阶段：
         stage_inputs → generate → verify → validate → submit
                      → monitor → progress → download → parse
         每阶段结束后按「科学收敛」决定是否放行下一阶段
```

## 4. 组件与文件

| 路径 | 变更 | 职责 |
| --- | --- | --- |
| `src/vaspilot/vaspkit/structure.py` | 新增 | `split_annotation()`、`normalize_poscar()`、`structure_summary()`；纯函数，不联网 |
| `src/vaspilot/vaspkit/recipe.py` | 新增 | 配方 schema、`validate_recipe()`、`build_campaign()`（阶段图 + 断言表） |
| `src/vaspilot/vaspkit/adapter.py` | 新增 | **唯一**知道 VASPKIT 命令行长什么样的地方；`doctor()` 与各 task 的命令构造（两种调用模式） |
| `src/vaspilot/vaspkit/verify.py` | 新增 | INCAR 断言比对与「危险偏离」表 |
| `src/vaspilot/workflow/campaign.py` | 新增 | `CampaignStore`（持久化）、`CampaignRunner`（串阶段、判依赖、写日志） |
| `src/vaspilot/workflow/plan.py` | 修改 | 新增步骤类型 `generate` / `verify` / `stage_inputs`；允许文件 provenance 为 `vaspkit`（无本地路径，哈希在运行时记录） |
| `src/vaspilot/core/config.py` | 修改 | `ServerEntry` 新增 `vaspkit_command` / `vaspkit_version` / `potcar_paths` |
| `src/vaspilot/tools/registry.py` | 修改 | 注册第 8 节的五个工具 |
| `src/vaspilot/ui/server.py` | 修改 | `campaign.*` API |
| `src/vaspilot/ui/static/index.html` | 修改 | 「作业」标签顶部的「我的计算」列表 |

## 5. 配方（recipe）

模型唯一的产出物，必须过 schema 校验：

```json
{
  "structure": {"file": "Fe2O3.vasp", "formula": "Fe2O3", "sha256": "…"},
  "functional": "PBE",
  "stages": ["relax", "static", "band", "dos"],
  "kspacing": {"relax": 0.04, "static": 0.03, "dos": 0.02},
  "spin": true,
  "hubbard_u": {"Fe": {"L": 2, "U": 4.0, "J": 0.0}},
  "overrides": {"relax": {"NSW": 200, "ISIF": 3}},
  "resources": {"server": "hpc1", "ntasks": 32, "walltime": "24:00:00"}
}
```

校验规则：`stages` 必须是已支持阶段的子集且顺序合法（`band`/`dos` 蕴含
`static`，`static` 蕴含 `relax` 除非显式声明结构已优化）；`kspacing` 在
`0.01..0.5`；`hubbard_u` 的元素符号必须出现在结构里；`overrides` 的键必须
是已知 INCAR 参数且不得与该阶段的断言冲突（冲突即拒，不静默覆盖）。

**标注切分**：POSCAR 格式在坐标之后是速度块，因此标注不能靠注释符号识别。
读满 `sum(原子数)` 行坐标即停，其余全部视为标注文本。

## 6. 阶段图与自检

| 阶段 | POSCAR | KPOINTS | INCAR 断言 | 前置条件 |
| --- | --- | --- | --- | --- |
| 01 relax | 规范化后的 `.vasp` | task 102（粗） | `ISIF=3` `IBRION=2` `NSW>0` | — |
| 02 static | 01 的 CONTCAR | task 102（密） | `NSW=0` `ICHARG=2` `LCHARG=.TRUE.` | 01 已收敛 |
| 03 band | 02 的 POSCAR | **task 303**（高对称路径） | `ICHARG=11` `LORBIT=11` `NSW=0` | 02 已收敛 + CHGCAR 就位 |
| 04 dos | 02 的 POSCAR | task 102（最密） | `ICHARG=11` `LORBIT=11` `NEDOS≥1000` | 02 已收敛 + CHGCAR 就位 |

`ISPIN=2` 在 `spin: true` 时对所有阶段生效。

远端目录：`<remote_root>/campaigns/<campaign_id>/{01-relax,02-static,03-band,04-dos}/`

**自检流程**（方案 A 的命门）：生成 INCAR 后立刻回读，用现有 `parse_incar`
解析，逐条核对该阶段断言——断言中的键必须存在且值相等。另有一张「危险偏离」
表（relax 阶段出现 `NSW=0`、band/dos 阶段 `ICHARG≠11` 等），命中即中止。

不通过时：阶段置为 `blocked`，**不提交作业**，写审计，界面红字列出
「期望 vs 实际」差异。不静默修正、不自动重试——断言失败意味着服务器上的
VASPKIT 行为与探测结论不符，需要人来看。

**事后哈希链**：各阶段实际使用的 POSCAR/INCAR/KPOINTS 的 sha256 在运行时
记入 run 日志。批准时无法预先验证，但事后完全可追溯。01 阶段例外——它的
POSCAR 来自你的 `.vasp`，可预先哈希，保留原有的完整文件级保证。

## 7. VASPKIT 适配与探针

`vaspkit_doctor(server)` 依次执行并把结果写入服务器配置：

1. `command -v vaspkit`；失败则试 `module avail vaspkit` 与常见安装前缀
2. `vaspkit -v` 取版本
3. 读 `~/.vaspkit`，提取 `PBE_PATH` / `GGA_PATH` / `LDA_PATH`
4. 确认赝势目录存在可读，抽样核对若干元素子目录
5. 在临时目录用一个最小 POSCAR 试跑 task 103，判定该版本属于「直出」还是
   「需 stdin 喂菜单」，记录选路结论

适配层为两种调用模式各留一条实现。**探针在真机跑通之前，本设计不声称整条
链可用。**

## 8. 工具面

| 工具 | 类别 | 说明 |
| --- | --- | --- |
| `vaspkit_doctor` | read | 探测服务器上的 VASPKIT 与赝势库 |
| `campaign_plan` | read | `.vasp` + 标注 → 配方 + 阶段图 + 预期 INCAR；**完全不碰服务器** |
| `campaign_start` | write | 需人工批准，启动整条链 |
| `campaign_status` | read | 方案与各阶段进度 |
| `campaign_abort` | write | 中止未完成的阶段 |

均走现有 registry 的 read/write 分级与审计，不新增旁路。

## 9. 「我的计算」列表

「作业」标签顶部新增，一行一个方案，展开见阶段：

```
Fe2O3 · PBE · hpc1                                运行中 2/4   3h12m
  ✓ 01 结构优化   COMPLETED   离子步 37/200   已收敛   E = -42.117 eV
  ● 02 静态自洽   RUNNING     电子步 18/60             E = -42.09 eV
  ○ 03 能带       等待 02 收敛
  ○ 04 态密度     等待 02 收敛
```

数据来源均为现成能力：调度器状态取自 `job_state`，科学进度取自
`vasp_progress`。沿用现有 60 秒心跳，不新增轮询通道。现有「VASP 科学进度」
卡片需手动粘贴目录，方案跑起来后自动进入此列表。

## 10. 错误处理

| 情形 | 行为 |
| --- | --- |
| 模型产出的配方不合 schema | 拒绝，回显校验错误，不联网 |
| `.vasp` 缺元素符号行 | 体检失败并明确指出（VASPKIT task 103 依赖该行选赝势） |
| 服务器无 VASPKIT 或赝势路径不可读 | `campaign_start` 前置拒绝，提示先跑 `vaspkit_doctor` |
| INCAR 断言不通过 | 阶段 `blocked`，不提交，列出差异 |
| 前序阶段未收敛 | 后续阶段不启动，方案停在 `needs_review` |
| 作业失败/超时 | 沿用 `WorkflowEngine` 现有的 attempt 机制 |
| 批准过期或 `campaign_hash` 变更 | 拒绝执行，要求重新批准 |

## 11. 测试

- **纯函数层**（最厚）：标注切分、POSCAR 体检、配方校验、断言比对、危险偏离表
- **适配层**：在 `tests/fake_hpc.py` 中加入假 VASPKIT，验证两种调用模式的命令
  构造与产物解析；验证 doctor 的选路判定
- **整链**：假 HPC 上跑完四阶段，重点验三件事——依赖顺序正确、01 未收敛时
  02 确实不启动、断言不通过时确实没有作业被提交
- **批准**：`campaign_hash` 变更后旧批准失效

## 12. 待你确认的两点

1. **断言表**（第 6 节）：哪些参数必须锁死、锁错会毁掉结果，你比我清楚。
   当前这张表是我按常规做法拟的起点。
2. **`~/.vaspkit` 与赝势库在你服务器上的实际位置**：若已知，探针可少猜一轮。

两点都不阻塞实现——断言表可改，路径由探针发现。
