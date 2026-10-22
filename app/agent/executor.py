"""Executor: runs the plan in dependency waves, in parallel within a wave, with per-task fallbacks."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from pathlib import Path

from pydantic import ValidationError

from app.config import Settings
from app.llm.base import LLMEvent
from app.llm.resilient import ResilientLLM
from app.tools.knowledge import KnowledgeBase
from app.tools.registry import ToolContext, ToolRegistry

from .models import Plan, PlanTask, TaskRun, ToolResult

log = logging.getLogger("agent.executor")


class Executor:
    def __init__(self, llm: ResilientLLM, registry: ToolRegistry, kb: KnowledgeBase, settings: Settings) -> None:
        self.llm, self.registry, self.kb, self.settings = llm, registry, kb, settings

    async def run(
        self, request: str, plan: Plan, events: list[LLMEvent], output_path: Path
    ) -> tuple[dict[str, TaskRun], dict[str, ToolResult]]:
        runs = {t.id: TaskRun(id=t.id, title=t.title, tool=t.tool, depends_on=t.depends_on) for t in plan.tasks}
        results: dict[str, ToolResult] = {}
        base = ToolContext(
            request=request, plan=plan, llm=self.llm, events=events, kb=self.kb,
            settings=self.settings, results=results, runs=runs, output_path=output_path,
        )
        sem = asyncio.Semaphore(self.settings.max_concurrency)
        by_id = {t.id: t for t in plan.tasks}

        for wave, ids in enumerate(self._waves(plan.tasks)):
            await asyncio.gather(*(self._run_task(by_id[i], wave, base, sem) for i in ids))
        return runs, results

    @staticmethod
    def _waves(tasks: list[PlanTask]) -> list[list[str]]:
        remaining = {t.id: set(t.depends_on) for t in tasks}
        waves: list[list[str]] = []
        while remaining:
            ready = [i for i, d in remaining.items() if not d]
            if not ready:
                raise RuntimeError(f"unresolvable dependencies: {sorted(remaining)}")
            waves.append(ready)
            for i in ready:
                remaining.pop(i)
            for d in remaining.values():
                d.difference_update(ready)
        return waves

    async def _run_task(self, task: PlanTask, wave: int, base: ToolContext, sem: asyncio.Semaphore) -> None:
        run = base.runs[task.id]
        run.wave, run.status = wave, "running"
        spec = self.registry.get(task.tool)
        assert spec is not None  # guaranteed by Planner.normalise
        started = time.monotonic()

        raw = dict(task.args)
        if "heading" in spec.args_model.model_fields:
            raw.setdefault("heading", task.title)
        deps = "\n".join(
            f"[{d}: {base.runs[d].title}] {base.results[d].summary}" for d in task.depends_on if d in base.results
        )
        ctx = replace(base, deps_text=deps)

        async with sem:
            args = None
            try:
                args = spec.args_model.model_validate(raw)
                result = await spec.run(args, ctx)
                run.status = "degraded" if result.degraded else "completed"
            except Exception as exc:  # noqa: BLE001 - one bad task must not sink the run
                log.warning("task %s (%s) failed: %s", task.id, task.tool, exc)
                run.error = str(exc)[:300]
                if spec.fallback is None:
                    run.status = "failed"
                    run.duration_ms = int((time.monotonic() - started) * 1000)
                    raise
                if args is None:  # invalid LLM-supplied args: salvage with safe defaults
                    try:
                        args = spec.args_model.model_validate({"heading": raw.get("heading", task.title)})
                    except ValidationError:
                        args = spec.args_model.model_construct(**raw)
                result = spec.fallback(args, task)
                run.status = "degraded"

        base.results[task.id] = result
        run.duration_ms = int((time.monotonic() - started) * 1000)
