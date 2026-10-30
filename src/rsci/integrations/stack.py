"""Honest capability discovery for the optional automation stack.

The core application remains usable without these heavy dependencies.  This
module is deliberately import-safe so the UI can explain which layer is
available without pretending that installing a package also configures a
cluster, database, daemon, or workflow worker.
"""

from __future__ import annotations

from importlib.util import find_spec


_LAYERS = (
    ("pymatgen", "pymatgen", "VASP 输入输出与结构解析"),
    ("custodian", "custodian", "VASP 错误监控、修复与重启"),
    ("atomate2", "atomate2", "材料计算工作流定义"),
    ("dpdispatcher", "dpdispatcher", "远程机器、调度器与任务分发"),
    ("aiida", "aiida", "多服务器编排、持久化与数据溯源"),
)


def automation_stack_status() -> dict:
    layers = []
    for name, module, purpose in _LAYERS:
        installed = find_spec(module) is not None
        layers.append({"name": name, "installed": installed,
                       "configured": False, "purpose": purpose})
    return {
        "ok": True,
        "layers": layers,
        "ready_for_material_workflows": all(
            row["installed"] for row in layers[:3]),
        "ready_for_dispatch": layers[3]["installed"],
        "ready_for_provenance": layers[4]["installed"],
        "note": ("installed 仅表示 Python 包可用；服务器、调度器、AiiDA "
                 "profile/daemon 仍需单独配置。"),
    }
