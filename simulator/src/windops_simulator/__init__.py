"""The WindOps telemetry simulator (SRS 10.4). Every sample it makes is marked synthetic."""

from windops_simulator.fleet import DEMO_TURBINES, TurbineSpec
from windops_simulator.models import MODEL_VERSION
from windops_simulator.simulator import Sample, Simulator

__all__ = ["DEMO_TURBINES", "MODEL_VERSION", "Sample", "Simulator", "TurbineSpec"]
