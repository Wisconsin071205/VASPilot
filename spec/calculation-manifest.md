# CalculationManifest 说明

一次 VASP 计算从准备到专家复核的完整可溯源记录。v0.3 新写入对应
[`calculation-manifest-v3.schema.json`](calculation-manifest-v3.schema.json)；
`calculation-manifest.schema.json` 与 v2 Schema 仅用于兼容读取和迁移。

## 设计要点

- **脱敏**：不存密码、TOTP、私钥、POTCAR 全文、个人路径；服务器用目录内名称，
  POTCAR 只存整文件哈希与 TITEL，INCAR 只摘录白名单参数。
- **可溯源**：结构哈希、输入哈希、结果文件哈希、计划哈希（plan_sha256）、
  工具链版本与 git 提交号全部落盘，任何一环都可回溯。
- **审计内嵌**：`modifications` 数组记录每次输入变更（时间/字段/前后值/理由/作者），
  与工具的三级修复策略（L1 检测 / L2 审批补丁 / L3 白名单自动）一一对应。
- **状态分离**：`execution.status`（调度/程序）与 `results.scientific_status`
  （科学收敛）严格分离；REVIEWED 才代表结论被接受。
- **版本化**：`schema_version` 与解析器版本同步；解析器升级时提供 migration 说明，
  旧 manifest 不静默失效。

## 字段速查

| 组 | 关键字段 | 作用 |
|---|---|---|
| workflow | name/version/stage/plan_sha256 | 工作流阶段与计划确定性校验 |
| structure | source_file/sha256/formula/n_atoms/lattice_hash | 结构来源与指纹 |
| inputs | incar/kpoints/potcar 哈希 + 白名单参数 | 输入指纹（POTCAR 只存哈希与 TITEL） |
| environment | server/job_id/queue_events | 调度环境（脱敏） |
| software | toolchain_version/git_commit/parser | 工具链与解析器指纹 |
| execution | status/时间/退出码 | 程序级状态 |
| results | scientific_status/energy/ionic/electronic/errors/files | 科学结论与证据 |
| modifications | time/file/field/before/after/reason/author | 全程变更审计 |
| review | reviewed/reviewer/conclusion/notes | 专家复核 |
| agents | agent_id/role/model_backend/task_id | 专业 Agent 委派记录 |
| capability_invocations | ID/版本/实现哈希/输入输出哈希 | 确定性能力溯源 |
| data_sources/transfers/figures | 来源、传输和 PlotBundle | 本地处理完整溯源 |

## 生命周期

1. workflow_prepare 生成 `plan.json` 并记录 plan_sha256；
2. 提交后由 vasp_parse 在 FINISHED 时生成 manifest（或对既有目录离线生成）；
3. 每次修改（含审批后的 L2 补丁）追加 modifications；
4. 专家复核后置 reviewed=true 并写 conclusion；
5. manifest 与原始 JSON 证据（gateway 的 vasp-inspect 输出等）一起归档，
   供后续训练/评测重新生成标签（遵守脱敏规则）。
