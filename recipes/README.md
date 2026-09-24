# Legacy workflow recipes

The JSON files in this directory describe future or migrated multi-stage
workflow designs. They are not loaded by the current `WorkflowEngine`.

The implemented product contract is the single-stage VASP job submission
documented in [`spec/workflows.md`](../spec/workflows.md). Each relax, static,
DOS, band, NEB, or convergence calculation must currently be prepared and
approved as its own plan; no recipe is silently expanded into additional jobs.
