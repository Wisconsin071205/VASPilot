"""VASPilot: CLI-first, multi-model VASP/HPC agent.

Three-layer boundary:
  1. local CLI/agent layer  - model calls, planning, approval, audit, monitoring
  2. Vlab gateway layer     - non-sensitive server catalog, per-server SSH mux
  3. HPC adapter layer      - Slurm/PBS, file ops, VASP validation/parsing

No layer ever exposes an arbitrary remote shell to a model.
"""

__version__ = "1.0.0"
GATEWAY_PROTOCOL_VERSION = "1"
