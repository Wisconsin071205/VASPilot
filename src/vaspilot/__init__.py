"""VASPilot: CLI-first, multi-model VASP/HPC agent.

Three-layer boundary:
  1. local CLI/agent layer  - model calls, planning, approval, audit, monitoring
  2. Vlab gateway layer     - non-sensitive server catalog, per-server SSH mux
  3. HPC adapter layer      - Slurm/PBS, file ops, VASP validation/parsing

The agent operates through a named, audited tool registry; shell access
(shell_run / remote_run) is an explicit operator policy — audit-only, never
intercepted. Job submission pauses for human confirmation by default.
"""

__version__ = "1.4.0"
GATEWAY_PROTOCOL_VERSION = "2"
