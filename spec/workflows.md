# 单阶段 VASP 作业提交规范（v1）

本项目当前实现的是“单阶段 VASP 作业提交”，不是 relax → static → DOS/能带
等多阶段编排器。用户每次准备一套完整输入文件，生成一个不可变计划，经过一次
人工审批后提交一个调度器作业；结果解析只负责这一阶段。

## 执行链

```text
PREPARED → APPROVED → mkdir → upload → validate → submit → monitor
                                      ↓
                               progress → download → parse
                                                   ↓
                              completed / needs_review / failed
```

计划由 `workflow_prepare` 或 `rsci workflow prepare` 生成，固定以下内容：

- 服务器、远端目录、调度器、作业名、分区、核数、时限和 VASP 可执行文件；
- `INCAR`、`KPOINTS`、`POSCAR`、`POTCAR`（或明确声明不上传本地 POTCAR）以及
  `run.job.sh` 的 SHA-256；
- 有序步骤和风险说明。计划哈希绑定审批，任何输入、脚本或参数变化都必须重新准备
  和审批。

## 输入门禁

本地和远端都会校验：

- `INCAR` 非空且含有效的 `TAG = VALUE`；`NSW`、`NELM` 等关键数值合法；
- `KPOINTS` 的模式、网格/显式点和位移可解析；
- `POSCAR` 的缩放因子、晶格向量、元素计数、坐标模式和坐标行完整；模板占位结构
  会被拒绝；
- 默认必须存在非空 `POTCAR`。

`--skip-potcar` 的真实含义是“本计划不上传本地 POTCAR”，不是“VASP 不需要
POTCAR”，也不是“提交前自动从远端库组装”。此时远端运行目录在校验阶段必须已经
有正确的、已组装的 `POTCAR`；缺失会阻止提交。POTCAR 内容仍只允许做元数据处理，
不会进入模型上下文或日志。

## 状态语义

调度器状态和科学状态始终分开：

- `COMPLETED` 只表示调度器释放了作业，不表示 VASP 科学收敛；
- `completed` 要求调度器结束且本地解析确认电子/离子判据满足；
- 作业结束但未收敛进入 `needs_review`；超时、失败、输入错误进入 `failed`；
- `COMPLETING` 不是终态，监测器会继续等待。

结果解析必须包含下载的 `INCAR`，否则 `NSW` 缺失会被错误当成静态作业。已有本地
结果文件会与远端重新下载的临时副本比对哈希；不一致时拒绝使用，避免陈旧结果污染
科学判定。`CONTCAR` 对静态作业是可选输出，缺失不会让整个下载步骤失败。

## 审批、并发与恢复

审批引用是绑定服务器、计划哈希、文件哈希和参数哈希的一次性令牌。消费审批和写入
运行状态使用本地锁，运行状态使用临时文件 + `fsync` + 原子替换保存。

每个外部步骤在执行前先写入 `started` 日志。提交调用后异常、没有返回作业号或进程
退出，都进入 `submit_uncertain`，包括 `--force` 在内的恢复不会再次 `sbatch/qsub`。
用户可以只读核对调度器，再以确认短语关联目录匹配、有明确状态的作业号。

运行记录采用 `rsci.run/2`，任务中心继续读取同一份 JSON。入队分为“保存待激活记录、
消费一次性审批、激活任务”，每个阶段均可在进程退出后恢复。仅保存绑定本次运行、
计划、服务器身份及回收目录的签名执行凭据；不保存原始审批引用，也不把它交给后台
子进程。执行者持有跨进程任务锁；PID 和心跳不是抢占依据。

UI 使用每任务独立的隐藏后台进程。关闭窗口或 UI 进程不取消远端作业，也不停止后台
监控；电脑重启后，重新打开应用扫描并接续未完成任务。不安装系统服务或自启动。
Campaign 不迁入此执行模型。

首次提交和其他远端写入需要尚未过期的审批；已知作业号的查询、下载和本地解析可以
在审批过期后继续。恢复会重建上下文并先核对远端状态，不重新提交。服务器目标变化、
输入哈希变化、凭据无效会阻止自动操作；缺少登录条件时等待用户登录，不弹出后台登录窗。
旧记录只有审批账本、计划哈希、作业号与远端工作目录一致时才自动迁移，无法证明归属
或损坏的记录保留只读，不自动执行。

正常轮询默认 30 秒，短暂网络失败按 5、10、20、40、60 秒退避，持续失败转待处理。
监控截止时间落盘，应用重启不重新计时；用户可以显式延长同一个作业的监控。
队列消失或 `UNKNOWN` 不表示完成。回收失败只接续回收，已有文件仍须校验哈希。

CLI 默认仍同步等待；后台执行示例：

```text
rsci workflow run PLAN_ID --approval-ref APPROVAL_REF --background
rsci workflow list
rsci workflow status PLAN_ID
rsci workflow resume PLAN_ID --background
rsci workflow resume PLAN_ID --background --extend-monitor
rsci workflow reconcile PLAN_ID
rsci workflow bind-job PLAN_ID --job-id JOB_ID --confirm "bind PLAN_ID JOB_ID"
```

仅在首次提交前需要新授权时，`resume` 才需要新的 `--approval-ref`。关联作业需要
人工确认，确认后后台继续监控与回收；此操作不提交、取消或重跑作业。
后台日志保存在配置目录的 `runs/PLAN_ID.worker.log`；配置目录统一尊重
`--config-dir` 和 `RSCI_HOME`。

## 不属于当前实现的内容

`recipes/*.json` 和旧版多阶段说明保留作设计草稿/迁移材料，但不会被当前
`WorkflowEngine` 自动加载、展开或串联。若要执行 relax、static、DOS、能带、NEB 或
收敛扫描，必须由用户分别准备每一阶段的输入，并为每一阶段单独生成和审批计划；
阶段之间的 `CONTCAR/CHGCAR` 传递不由本版本自动完成。
