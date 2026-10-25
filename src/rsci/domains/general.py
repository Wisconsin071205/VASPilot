"""General research files and evidence, without a material-science verdict."""
from __future__ import annotations

from pathlib import Path


class GeneralDomain:
    kind = "general"
    title = "通用科研"

    def validate_project(self, directory: Path, metadata: dict) -> dict:
        return {"ok": True, "domain": self.kind, "errors": [], "warnings": [],
                "ready_for_single_stage": False,
                "note": "通用项目不要求材料计算输入；执行脚本前需生成并审批分析计划。"}

    def conclusion(self, execution_state: str, analysis: dict) -> dict:
        return {"status": "completed" if execution_state == "COMPLETED" else "failed",
                "scientific_converged": None, "scientific_status": "not_assessed",
                "scientific_note": "脚本执行与结果回收不代表科学结论已获验证。"}
