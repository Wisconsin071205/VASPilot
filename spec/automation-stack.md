# VASP 自动化技术栈分层

当前实现是轻依赖的自研路径：`gateway` 负责 SSH/Vlab 与 Slurm/PBS，
`workflow` 负责计划、审批和执行，`hpc/vasp.py` 负责受限文本解析。它并未
直接使用 pymatgen、custodian、atomate2、DPDispatcher 或 AiiDA。

目标架构按职责分层，而不是让多个框架同时接管同一件事：

1. **材料计算层**：pymatgen 生成和解析 VASP 输入输出；custodian 监控已知
   错误并执行受控修复；atomate2 定义 relax、static、DOS 等工作流。
2. **执行层**：DPDispatcher 适配机器、资源、文件传输和 Slurm/PBS 提交。
   现有 gateway 保留为安全边界和兼容后端，迁移期通过统一接口二选一。
3. **控制与溯源层**：需要跨多台服务器、持久化进程和数据 provenance 时启用
   AiiDA。单服务器桌面模式不强制依赖数据库和 daemon。
4. **产品层**：UI 只消费统一的作业、进度和诊断模型，不直接依赖任一框架。

可选依赖可用 `pip install -e .[materials]`、`.[dispatch]`、`.[provenance]`
或 `.[automation]` 安装。安装成功不等于部署完成；服务器凭据、调度器模板、
AiiDA profile/database/daemon 和 atomate2 worker 必须另行配置并经过真实集群验证。

迁移顺序：先用 pymatgen 增强本地输入/结果校验，再以 DPDispatcher 后端替换
重复的提交与传输逻辑，随后用 atomate2 表达多阶段工作流；只有确有多服务器
编排和科研数据溯源需求时，再把 AiiDA 放到控制平面。
