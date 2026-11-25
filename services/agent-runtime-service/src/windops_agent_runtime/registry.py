"""The workflows this runtime can run, by name. A run names one, and pins its version."""

from windops_agent_runtime.decision import Decision
from windops_agent_runtime.investigation import Investigation
from windops_agent_runtime.workflows import ModelCheck, Rehearsal, Workflow

REGISTRY: dict[str, Workflow] = {
    workflow.name: workflow for workflow in (Rehearsal(), ModelCheck(), Investigation(), Decision())
}
