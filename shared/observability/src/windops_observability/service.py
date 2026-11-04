"""Identify the running service in every log, trace and metric."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceInfo:
    name: str
    environment: str
    version: str
