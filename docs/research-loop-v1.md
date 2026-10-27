# 科研辅助闭环 v1

在「项目文件」选择通用科研项目，然后使用「文献 / 数据 / 报告」。VASP 项目继续使用原来的专业功能。

## 文献与模型

1. 导入 PDF（每份不超过 50 MiB、300 页）。原件按 SHA-256 去重，文本、扫描及混合 PDF 都进入同一文献库。
2. 第一次使用点击「安装 OCR 组件」。独立 Python 3.12、Docling、RapidOCR 和 ONNX Runtime CPU 环境位于实际配置目录的 `research-runtime/`；需联网和至少 8 GiB 可用空间，不修改系统 Python。依赖版本及下载哈希固定在 `ocr-requirements.lock`。安装失败保留日志与缓存，可恢复同一任务。
3. 离线机器可导入同平台的可信组件 ZIP。先在联网机器运行 `python scripts/package_ocr.py --config-dir <配置目录> --output <组件.zip>`，在目标机器使用「导入本机 OCR 组件包」或 `rsci research install-ocr <项目> --offline-bundle <组件.zip>`。
4. 点击解析。后台按页保留检查点，同一配置目录最多一个 OCR 执行者。关闭窗口后继续处理，再次打开会恢复未完成任务。原件、提取证据与笔记在项目目录；模型和页面缓存留在配置目录。
5. 选择 1–5 篇论文提问，或选择 2–5 篇进行比较。每篇最多发送 5 个相关片段，每片段最多 2000 字，界面和任务结果显示模型、文献及实际发送量。问答模式不开放工具，也不发送完整对话、配置或整个项目。
6. 在任务中心查看结果。引用可跳到物理页码并高亮；比较表和术语解释可直接修订，再保存人工修改记录。原始结果和证据不会被替换。

OCR 可能误读，尤其是公式、单位和图表；无法提取的页面会标注需要核对。DOI 元数据、原文出处和科学结论正确性是不同检查。引用通过仅说明该片段来自指定文献。模型调用中断后不会自动重发，需要人工重试，可能再次产生费用。

文献请求使用专用的非流式、无工具、无自动重试入口；HTTP 错误或响应不完整都保留为待处理状态。DOI 当前明确显示「未核验」，不会把本地引用匹配当作 DOI 注册信息核验。源码安装还提供 `rsci[literature]` 和 `rsci[analysis]` 可选依赖组；桌面解析仍使用独立组件环境。

## 数据与审批

导入 UTF-8 CSV/TSV，选择「数据」进行检查。第一版上限为 16 MiB、100000 行、200 列。检查缺失、重复、混合类型、常量列、非有限值、IQR 异常候选；指定标签或划分列后检查分布和精确重复样本交叉。检查不会改写数据，也不意味着排除了全部数据泄漏。

可生成描述统计、Pearson/Spearman 相关分析、单自变量线性回归脚本。选择列和缺失值处理方式后，审阅脚本，再通过既有审批流程运行。数值模板固定 `scipy==1.15.3`；源码版需在分析 Python 中安装该版本，EXE 可使用已安装 OCR 组件的独立 Python。不会自动安装分析依赖或重跑任务。

模板与计划绑定脚本、输入、参数及解释器。输入改变后需要新计划和新审批。回归输出样本量、斜率、截距、R²、标准误、p 值及 95% 区间；假设和局限随结果保存。

## 报告与复现

在「报告」选择已完成的文献结果、数据检查和分析计划。生成 Markdown、HTML 和 ZIP；ZIP 包含选定分析的输入快照、脚本、依赖文件、运行摘要、结果、引用片段及 SHA-256 清单。原始 PDF 默认不包含，可勾选。配置、审批令牌和执行凭据不进入导出。

HTML 可离线打开并展开出处。人工修改或模型解释不被包装成程序计算结果。数据或文献版本改变后，旧报告标注需要重新核对。

`rsci research verify <复现包.zip>` 只检查完整性，不运行代码；哈希校验不是作者身份或科学正确性的认证。重新执行需要审阅脚本、准备依赖并重新审批。

## CLI 与接口

```text
rsci --config-dir <目录> research import <项目> <论文.pdf>
rsci research documents <项目>
rsci research parse <项目> <文献ID>
rsci research ask <项目> --document <文献ID> --question "研究方法是什么？"
rsci research compare <项目> --document <ID1> --document <ID2>
rsci research glossary <项目> --document <文献ID>
rsci research profile <项目> data/sample.csv
rsci research template <项目> data/sample.csv --template regression --column x --column y --missing error
rsci research-task list --project <项目>
rsci research-task get <任务ID>
rsci research-task resume <任务ID>
rsci research-task cancel <任务ID>
rsci research report <项目> --question "研究总结" --note <笔记ID> --profile-task <检查ID> --plan <计划ID>
rsci research export <项目> <报告ID> --format zip
rsci research verify <复现包.zip>
```

UI 和 CLI 共用 `document.*`、`literature.*`、`research.*`、`report.*`、`research_task.*` 服务。通用 CLI 入口为 `research call <action> --json <对象>`。PDF 导入为经过令牌认证的流式 `POST /api/document/import?project=...&title=...`，其余 JSON 接口仍使用 `X-RSCI-Token`。页面和报告下载也是认证请求。

配置优先使用显式 `--config-dir`，否则遵循 `RSCI_HOME` 和既有默认目录。旧项目在文件锁内增补稳定 ID；有活动科研任务的项目不能重命名或删除。损坏记录保留，不会自动覆盖或猜测归属。

## 验收与开发

`scripts/verify_research_loop.py --config-dir <隔离目录> --model-home <现有模型配置目录>` 使用合成论文验证真实 OCR、关闭 UI 后后台继续、重启恢复、纯证据模型请求、审批分析及复现包校验。`--exe <rsci.exe>` 可改为通过打包入口启动 UI 和文献 worker。测试数据无真实论文内容或个人数据；密钥只通过本机配置读取，不写日志或参数。

常规回归使用 `pytest`，扩展使用 `npm test` 与 `npm run compile`。源码和 Windows 包通过 `scripts/build_submission.py`、`scripts/verify_submission.py` 构建及验证。OCR 包独立交付；尚未安装组件时，普通聊天、项目和现有工作流仍可使用。

`verify_research_loop.py --exe <rsci.exe> --restricted-path` 将测试进程的 PATH 限制到 Windows 系统目录。`scripts/verify_research_recovery.py --config-dir <同一验收目录> --exe <rsci.exe>` 使用双栏、表格、旋转、扫描和空白页，在第一个真实检查点后终止 worker，再并发启动两个恢复进程，验证已有检查点不被覆盖。

本轮在 Windows 实机验证了联网准备组件、离线组件迁移、中断安装恢复、真实模型、关闭 UI 后解析及分析继续执行、复现包哈希校验，以及上述 OCR 崩溃恢复。HTTP 抓取测试覆盖 Chat/Responses 两种协议，确认没有发送未选论文或项目私有笔记，并确认 503 响应只发送一次请求。界面完成独立审查及引用跳转复测。

未完成的环境验收：无预装 Python 的全新 Windows 虚拟机，以及 macOS/Linux 原生运行。限制 PATH 的 Windows 实机测试不能替代干净系统验收。当前随附 OCR 离线包仅适用于 Windows x64，不能跨平台使用；真实科研论文的 OCR 与结论忠实性仍需按领域抽样人工复核。
