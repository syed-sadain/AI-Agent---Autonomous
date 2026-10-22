"""Tool registry: each tool declares its schema, runner and a graceful-degradation fallback."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from pydantic import BaseModel

from app.agent.models import Plan, PlanTask, TaskRun, ToolResult
from app.config import Settings
from app.llm.base import LLMEvent
from app.llm.resilient import ResilientLLM

from .knowledge import KnowledgeBase


@dataclass
class ToolContext:
    request: str
    plan: Plan
    llm: ResilientLLM
    events: list[LLMEvent]
    kb: KnowledgeBase
    settings: Settings
    results: dict[str, ToolResult]
    runs: dict[str, TaskRun]
    deps_text: str = ""
    output_path: Path | None = None


ToolFn = Callable[[BaseModel, ToolContext], Awaitable[ToolResult]]
FallbackFn = Callable[[BaseModel, PlanTask], ToolResult]


@dataclass
class ToolSpec:
    name: str
    description: str
    args_doc: str
    args_model: type[BaseModel]
    run: ToolFn
    fallback: FallbackFn | None = None
    produces_block: bool = True


@dataclass
class ToolRegistry:
    _tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools)

    def describe(self) -> str:
        """Rendered into the planner prompt so the LLM chooses tools from the real catalogue."""
        return "\n".join(f"- {t.name}: {t.description} Args: {t.args_doc}" for t in self._tools.values())
