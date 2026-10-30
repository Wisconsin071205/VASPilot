"""Compare recorded submit attempts with a read-only observation of today's path."""

from __future__ import annotations

import re
import shlex


def _options_queue(tokens: list[str], scheduler: str) -> str:
    queue = ""
    for index, token in enumerate(tokens):
        if token in ({"-q"} if scheduler == "pbs" else {"-p", "--partition"}):
            if index + 1 < len(tokens):
                queue = tokens[index + 1]
        elif scheduler == "slurm" and token.startswith("--partition="):
            queue = token.split("=", 1)[1]
    return queue


def _words(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def analyze_submission(attempt: dict | None, current: dict) -> dict:
    """Recommendations have a scope and confidence; missing history stays missing."""
    attempt = attempt or {}
    execution = attempt.get("execution") or {}
    findings, repairs = [], []

    def finding(code: str, scope: str, confidence: str, message: str) -> None:
        findings.append({"code": code, "scope": scope, "confidence": confidence,
                         "message": message})

    def repair(target: str, action: str) -> None:
        repairs.append({"target": target, "action": action,
                        "requires_cluster_admin": False})

    gaps = [key for key in ("command", "gateway_version", "returncode")
            if key not in execution] if attempt else []
    if gaps:
        finding("historical_execution_incomplete", "historical", "unknown",
                "This old record lacks some execution facts. Current probe values must "
                "not be presented as the command or version used by that old attempt.")
    result = {"ok": True, "attempt": attempt, "current": current,
              "historical_execution_gaps": gaps, "findings": findings,
              "repair_plan": repairs, "can_auto_resubmit": False,
              "verification": "Reconcile any unresolved attempt first. A new submission "
              "needs human approval; verify the returned job ID and scheduler state. "
              "A read-only inspection does not prove a future job will succeed."}
    if not current.get("available"):
        unsupported = current.get("reason") == "inspection_unsupported"
        finding("current_path_unverified", "current", "unknown",
                "The deployed command has not been verified. A historical queue error "
                "alone cannot establish that another attempt will fail.")
        repair("application_gateway" if unsupported else "connection_diagnostics",
               "Update the application gateway to support read-only inspection, then "
               "inspect again. Do not change cluster defaults on this evidence."
               if unsupported else "Resolve the failed read-only probe and inspect again.")
        return result

    scheduler = str(current.get("scheduler") or "")
    tokens = _words(str(current.get("command") or ""))
    executable = "qsub" if scheduler == "pbs" else "sbatch"
    position = next((i for i, token in enumerate(tokens)
                     if token.rsplit("/", 1)[-1] == executable), None)
    argv = tokens[position:] if position is not None else []
    selected = _options_queue(argv, scheduler)
    prefix = current.get("directive_prefix") if scheduler == "pbs" else "#SBATCH"
    if not selected:
        for line in (current.get("script_directives") or []) if prefix else []:
            match = re.match(re.escape(prefix) + r"\s+(.+)", line)
            if match:
                selected = _options_queue(_words(match.group(1)), scheduler) or selected
    result["current_explicit_queue"] = selected or None
    prefix_unverified = scheduler == "pbs" and prefix is None
    prefix_mismatch = (scheduler == "pbs" and prefix is not None and prefix != "#PBS"
                       and any(line.startswith("#PBS ") for line in current.get("script_directives") or []))
    if prefix_unverified or prefix_mismatch:
        finding("directive_prefix_not_matched", "current", "unknown" if prefix_unverified else "observed",
                "The qsub directive prefix has not been verified against this script. "
                "A literal #PBS queue line is not proof that qsub reads it.")
        repair("submission_environment", "Inspect PBS_DPREFIX and align it with the "
               "approved script's directive prefix; do not change cluster default queues.")

    queues = {}
    raw_queues = str(current.get("queue_list") or "")
    queue_list_known = "queue_list" in current and not current.get("queue_list_truncated")
    if scheduler == "pbs":
        queue_list_known = queue_list_known and raw_queues.lstrip().startswith("Queue")
        for line in raw_queues.splitlines():
            fields = line.split()
            if len(fields) >= 5 and fields[3] in {"yes", "no"} and fields[4] in {"yes", "no"}:
                queues[fields[0]] = fields[3:5] == ["yes", "yes"]
    else:
        for line in raw_queues.splitlines():
            fields = line.split("|")
            if len(fields) >= 2:
                queues[fields[0].rstrip("*")] = fields[1].strip() == "up"

    known_bad_operand = scheduler == "pbs" and "--" in argv[1:]
    if known_bad_operand:
        torque61 = bool(re.search(r"\b6\.1(?:\.|\b)", str(current.get("scheduler_version"))))
        finding("pbs_double_dash_operand", "current", "observed" if torque61 else "inferred",
                "The deployed qsub command uses -- before the script. Torque 6.1's "
                "script scanner can skip the script, including its queue and resources.")
        repair("application_gateway", "Change the PBS adapter to pass ./<script> without "
               "--, then deploy that gateway. Keep script approval and resource checks; "
               "changing the server default queue does not fix a skipped script.")
    elif scheduler == "pbs" and argv and any(token.startswith("./") for token in argv[1:]):
        finding("pbs_path_operand_present", "current", "observed",
                "The currently deployed command passes ./<script> without the known "
                "Torque -- issue. Old failures do not establish that this path will fail.")

    explicit_valid = (queues.get(selected, False)
                      if selected and queue_list_known and "@" not in selected else None)
    result["current_explicit_queue_enabled"] = explicit_valid
    default = current.get("default_queue")
    default_valid = default in queues if queue_list_known and default is not None else None
    result["current_default_queue_exists"] = default_valid
    probe_errors = {key: value for key, value in current.items() if key.endswith("_error")}
    if probe_errors:
        finding("current_probe_incomplete", "current", "unknown",
                "Some read-only checks failed. Resolve those missing observations "
                "before using them to prescribe a repair or approve another attempt.")
        repair("read_only_diagnostics", "Resolve the reported probe errors and inspect again.")
    if selected and explicit_valid is True:
        finding("explicit_queue_available", "current", "observed",
                "The script/command selects a queue that is enabled and started. "
                "The default queue is not used when this explicit selection is honored.")
        if default_valid is False:
            finding("invalid_default_not_a_prerequisite", "current", "observed",
                    "The server default queue is missing, but correcting that global "
                    "setting is not a prerequisite for this explicit-queue submission.")
    elif selected and explicit_valid is False:
        repair("job_script", "Select an enabled, started queue supported by the job's "
               "resources, update the script's queue directive, and obtain fresh approval.")
    elif (not selected and not prefix_unverified and not prefix_mismatch
          and current.get("script_directives_complete") and default_valid is False):
        repair("job_script", "Choose a valid queue explicitly in the script and obtain "
               "fresh approval. A global default-queue change requires an administrator "
               "and is unnecessary for a single explicitly configured job.")
    elif selected and explicit_valid is None:
        finding("queue_not_verified", "current", "unknown",
                "Queue availability or its destination server was not fully verified. "
                "Inspect that queue before prescribing a configuration change.")
        repair("queue_diagnostics", "Verify the selected queue and its destination "
               "server with a complete read-only queue query, then diagnose again.")
    elif not selected and not current.get("script_directives_complete"):
        finding("script_header_incomplete", "current", "unknown",
                "The available header does not establish all script directives.")
        repair("script_inspection", "Read the complete directive header before "
               "deciding whether a queue directive is missing.")
    if not repairs:
        repair("verification", "No current configuration change is established by this "
               "inspection. Reconcile old attempts and use a newly approved attempt to "
               "check whether an error remains; do not promise success or certain failure.")
    if scheduler == "pbs":
        result["known_compatibility"] = {
            "issue": "torque_6_1_double_dash_script_scanner",
            "application_fix": "qsub ./<script> instead of qsub -- <script>",
            "fixed_gateway_release": "1.5.2",
            "source": "https://github.com/adaptivecomputing/torque/blob/"
            "1d115c5454b46ac0b4a6eb800665c7012bce6639/src/cmds/qsub_functions.c#L242",
            "historical_cause_requires": "the command/version actually recorded for that attempt",
        }
    return result
