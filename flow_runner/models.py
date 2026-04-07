"""Data models for flow definitions and execution state."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class FlowStatus(Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(Enum):
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class FlowStep:
    id: str
    action: str
    params: dict[str, Any]
    output: str | None = None
    audit: str | None = None  # "full" = log everything

    @classmethod
    def from_dict(cls, d: dict) -> FlowStep:
        return cls(
            id=d["id"],
            action=d["action"],
            params=d.get("params", {}),
            output=d.get("output"),
            audit=d.get("audit"),
        )


@dataclass
class FlowDefinition:
    name: str
    version: int
    description: str
    trigger: str
    steps: list[FlowStep]

    @classmethod
    def from_dict(cls, d: dict) -> FlowDefinition:
        return cls(
            name=d["flow"],
            version=d.get("version", 1),
            description=d.get("description", ""),
            trigger=d.get("trigger", "manual"),
            steps=[FlowStep.from_dict(s) for s in d["steps"]],
        )


@dataclass
class StepResult:
    step_id: str
    action: str
    seq: int
    status: StepStatus = StepStatus.RUNNING
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int = 0
    input_hash: str = ""
    output_summary: str = ""
    output_data: Any = None
    error: str | None = None
    tokens: int | None = None
    cost: float | None = None
    llm_response: dict[str, Any] | None = None  # Full provider response

    def compute_input_hash(self, params: dict) -> str:
        raw = json.dumps(params, sort_keys=True, default=str)
        self.input_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return self.input_hash


@dataclass
class FlowRun:
    flow_name: str
    agent_id: str
    trigger: str
    status: FlowStatus = FlowStatus.RUNNING
    started_at: datetime = field(default_factory=datetime.now)
    ended_at: datetime | None = None
    duration_ms: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    steps: list[StepResult] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    db_id: str | None = None  # SurrealDB record ID after insert
    flow_db_id: str | None = None  # flow record ID
