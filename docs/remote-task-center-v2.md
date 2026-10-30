# 远端任务中心 v2：第一轮

在“作业”页汇总所有已配置服务器上当前用户的作业，包括在终端提交的作业。
现有单服务器记录和 VASP 计算视图保留在“单台服务器记录与专业计算”内。

## 使用流程

1. 连接服务器，打开“作业”，刷新所有服务器。
2. 打开作业详情，核对调度器工作目录，关联已有项目。
3. 预览目录中的脚本、日志和结果；诊断列出规则命中的原始日志片段。
4. 对已确认结束的作业选择最多 100 个文件，加入结果回收队列。
5. 在项目 `results/remote/<task_id>/` 查看回收文件；回收记录保留各文件 SHA-256。

关联不执行提交、重跑或取消。诊断建议不会自动修改远端环境或作业脚本。
调度器状态、回收状态和科学收敛结论分别表示。

## 恢复行为

- 每个回收任务由一个独立隐藏进程执行，使用跨进程锁保证单执行者。
- 关闭窗口后仍继续回收。本机关机期间暂停，再次启动应用恢复未完成任务。
- 断网最多自动重试五次，间隔 5–60 秒；认证失效和文件冲突暂停，需人工处理后继续。
- 文件逐个原子发布，校验回收哈希后才登记完成；恢复跳过已完成且哈希一致的文件。
- 目前支持**按文件检查点恢复**，中断文件会重新传输，尚无文件内部字节级断点续传。
- 文件元信息在入队及传输前后核对，源文件改变时停止；已有目标文件不覆盖。
- 重复入队相同文件快照复用同一记录。恢复与回收代码没有提交作业入口。
- 停止回收只停止本机下载，当前单个传输可能先完成，远端作业不受影响。

## 状态和身份

作业快照绑定服务器名称、目标账户/主机、端口、根目录、调度器、路由和中转身份。
服务器目标改变时不复用旧快照，原回收任务停止。项目关联使用稳定项目 ID；
完成回收后重命名项目仍可解析记录。有未完成回收时禁止删除和重命名项目。

队列消失、历史服务不可用及 UNKNOWN 都不能证明成功。失败的服务器独立显示错误，
保留最后已知状态和时间；普通数字作业支持详情、关联与回收，数组或特殊作业标识暂仅展示。
目录详情最多展示前 200 个直属文件，日志最多读取 8 个文件的末尾 240 行。

## CLI 和 API

```console
rsci --config-dir <目录> job fleet
rsci job details 713 --server cluster
rsci job associate 713 my-project --server cluster
rsci job logs 713 /remote/work/job.err --server cluster
rsci job analyze 713 --server cluster
rsci job collect 713 /remote/work/result.csv --server cluster
rsci job collections
rsci job resume-collection <task_id>
rsci job stop-collection <task_id>
```

对应 API：`job.center/details/associate/logs/diagnosis`，
`collection.enqueue/list/get/resume/cancel`。沿用本地 API 的令牌认证。
`job.center` 接受 `refresh:false` 读取缓存。运行记录在实际配置目录的
`remote-center/` 与 `remote-collections/`，支持 `--config-dir` 与 `RSCI_HOME`。

## 验证

`tests/test_remote_center.py` 覆盖跨服务器同号作业、目录与项目关联、
失联、身份变化、文件冲突、重复回收和 HTTP 接口。
`tests/test_collection_processes.py` 使用磁盘持久化假 HPC 和真实子进程，
验证关闭 UI 后回收、重新打开读取结果、杀死 worker 后双重恢复以及不重复传输。

本轮未增加远端常驻任务代理、自动修复重提、通用计算模板或文件内部断点续传。
