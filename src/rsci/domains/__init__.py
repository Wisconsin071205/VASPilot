"""Explicit scientific domains; general projects never enable VASP implicitly."""

from ..core.errors import ValidationError


def domain_for(kind: str):
    if kind == "vasp":
        from .vasp import VaspDomain
        return VaspDomain()
    if kind == "general":
        from .general import GeneralDomain
        return GeneralDomain()
    raise ValidationError(f"unsupported project domain: {kind!r}")


def plan_domain(plan: dict):
    # All pre-domain plans were VASP. Never rehash/reinterpret old approvals.
    return domain_for(str(plan.get("domain") or "vasp"))
