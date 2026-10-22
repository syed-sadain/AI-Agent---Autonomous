"""Planner: the LLM designs its own TODO list; code validates and repairs it before execution."""
from __future__ import annotations

import logging

from app.config import Settings
from app.llm.base import AllProvidersFailedError, LLMEvent
from app.llm.resilient import ResilientLLM
from app.tools.registry import ToolRegistry

from . import prompts
from .models import Ambiguity, Plan, PlanTask

log = logging.getLogger("agent.planner")
FINAL_TOOL = "assemble_document"


class Planner:
    def __init__(self, llm: ResilientLLM, registry: ToolRegistry, settings: Settings) -> None:
        self.llm, self.registry, self.settings = llm, registry, settings

    async def create_plan(self, request: str, events: list[LLMEvent]) -> Plan:
        try:
            plan = await self.llm.complete_json(
                purpose="plan",
                system=prompts.planner_system(self.registry.describe(), self.settings.max_tasks),
                user=prompts.planner_user(request),
                model_cls=Plan,
                events=events,
                temperature=0.2,
            )
        except AllProvidersFailedError as exc:
            log.error("planning failed on every provider, using deterministic plan: %s", exc)
            return self.fallback_plan(request)
        normalised = self.normalise(plan)
        if not any(self.registry.get(t.tool).produces_block for t in normalised.tasks if self.registry.get(t.tool)):
            log.warning("LLM plan produced no content tasks, using deterministic plan")
            return self.fallback_plan(request)
        return normalised

    # ── validation / repair ────────────────────────────────────────────────
    def normalise(self, plan: Plan) -> Plan:
        """Never execute an LLM plan blindly: drop unknown tools, fix ids/deps, break cycles, cap size."""
        tasks: list[PlanTask] = []
        seen: set[str] = set()
        for t in plan.tasks:
            if t.tool == FINAL_TOOL or t.tool not in self.registry:
                if t.tool != FINAL_TOOL:
                    log.warning("dropping task %s: unknown tool %r", t.id, t.tool)
                continue
            tid = t.id if t.id not in seen else f"{t.id}_{len(seen)}"
            seen.add(tid)
            tasks.append(t.model_copy(update={"id": tid}))
        tasks = tasks[: self.settings.max_tasks - 1]

        ids = {t.id for t in tasks}
        for t in tasks:
            t.depends_on = [d for d in dict.fromkeys(t.depends_on) if d in ids and d != t.id]
        self._break_cycles(tasks)

        tasks.append(
            PlanTask(id="final", title="Assemble Word document", tool=FINAL_TOOL, args={}, depends_on=[t.id for t in tasks])
        )
        return plan.model_copy(update={"tasks": tasks})

    @staticmethod
    def _break_cycles(tasks: list[PlanTask]) -> None:
        remaining = {t.id: set(t.depends_on) for t in tasks}
        while remaining:
            ready = [i for i, d in remaining.items() if not d]
            if not ready:  # cycle: drop dependencies of the stuck tasks, keep going
                log.warning("dependency cycle among %s; clearing deps", sorted(remaining))
                for t in tasks:
                    if t.id in remaining:
                        t.depends_on = []
                return
            for i in ready:
                remaining.pop(i)
            for d in remaining.values():
                d.difference_update(ready)

    # ── deterministic safety net ───────────────────────────────────────────
    def fallback_plan(self, request: str) -> Plan:
        title = " ".join(request.split()[:10]).rstrip(".,;:") or "Business Document"
        sections = [
            ("Background", "Describe the context and why this work is needed."),
            ("Proposed Approach", "Explain the recommended approach and key activities."),
            ("Next Steps", "List concrete next steps with owners."),
        ]
        tasks = [PlanTask(id="t1", title="Executive Summary", tool="write_section",
                          args={"heading": "Executive Summary", "instructions": "Summarise purpose, approach and outcome."},
                          depends_on=["t2", "t3", "t4"])]
        for n, (h, ins) in enumerate(sections, 2):
            tasks.append(PlanTask(id=f"t{n}", title=h, tool="write_section", args={"heading": h, "instructions": ins}))
        return self.normalise(Plan(
            title=title, doc_type="business_report", objective=request[:200],
            assumptions=["Planning model was unavailable, so a generic report structure was used."],
            ambiguities=[Ambiguity(issue="Automatic planning failed", decision="Used a deterministic 4-section outline.")],
            tasks=tasks,
        ))
