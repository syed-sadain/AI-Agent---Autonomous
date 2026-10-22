"""Domain models shared by planner, executor, tools and the document builder."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class Ambiguity(BaseModel):
    """A gap/conflict the agent noticed in the request and how it decided to proceed."""

    issue: str
    decision: str = "Proceeded with a reasonable default; please confirm."


class PlanTask(BaseModel):
    id: str
    title: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str:
        return str(v)

    @field_validator("depends_on", mode="before")
    @classmethod
    def _deps_to_str(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            v = [v]
        return [str(x) for x in v]


class Plan(BaseModel):
    """The agent's own execution plan (its TODO list)."""

    title: str
    doc_type: str = "business_report"
    audience: str = "Business stakeholders"
    objective: str
    assumptions: list[str] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    tasks: list[PlanTask] = Field(min_length=1)

    @field_validator("ambiguities", mode="before")
    @classmethod
    def _coerce_ambiguities(cls, v: Any) -> Any:
        if not isinstance(v, list):
            return []
        return [{"issue": x} if isinstance(x, str) else x for x in v]

    @field_validator("assumptions", mode="before")
    @classmethod
    def _coerce_assumptions(cls, v: Any) -> Any:
        return [str(x) for x in v] if isinstance(v, list) else []


class Block(BaseModel):
    """A renderable document unit produced by a tool."""

    kind: Literal["section", "table"]
    heading: str
    paragraphs: list[str] = Field(default_factory=list)
    bullets: list[str] = Field(default_factory=list)
    callout: str | None = None
    intro: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    caption: str | None = None
    bold_last_rows: int = 0


class ToolResult(BaseModel):
    block: Block | None = None
    summary: str = ""
    sources: list[str] = Field(default_factory=list)
    degraded: bool = False
    artifact_path: str | None = None


TaskStatus = Literal["pending", "running", "completed", "degraded", "failed"]


class TaskRun(BaseModel):
    """Execution record for one planned task (exposed in the API response)."""

    id: str
    title: str
    tool: str
    depends_on: list[str] = Field(default_factory=list)
    wave: int = 0
    status: TaskStatus = "pending"
    duration_ms: int = 0
    error: str | None = None
