"""DocumentAgent: request -> plan -> execute -> .docx -> response."""
from __future__ import annotations

import base64
import logging
import time
import uuid

from app.config import Settings, get_settings
from app.llm.base import LLMEvent
from app.llm.factory import build_llm
from app.schemas import AgentResponse, DocumentInfo, LLMSummary, PlanSummary
from app.tools.builtin import build_default_registry
from app.tools.knowledge import KnowledgeBase

from .executor import Executor
from .planner import FINAL_TOOL, Planner

log = logging.getLogger("agent")


class DocumentAgent:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.llm = build_llm(self.settings)
        self.registry = build_default_registry()
        self.kb = KnowledgeBase(self.settings.knowledge_base_path)
        self.planner = Planner(self.llm, self.registry, self.settings)
        self.executor = Executor(self.llm, self.registry, self.kb, self.settings)

    async def aclose(self) -> None:
        await self.llm.aclose()

    async def run(self, request: str, *, download_base: str, include_base64: bool = False) -> AgentResponse:
        started = time.monotonic()
        run_id = uuid.uuid4().hex
        out_dir = self.settings.output_dir / run_id
        events: list[LLMEvent] = []

        plan = await self.planner.create_plan(request, events)
        log.info("run %s planned %d tasks: %s", run_id, len(plan.tasks), [t.tool for t in plan.tasks])

        runs, results = await self.executor.run(request, plan, events, out_dir / "document.docx")
        final = next(t for t in plan.tasks if t.tool == FINAL_TOOL)
        path = out_dir / "document.docx"
        if not results[final.id].artifact_path or not path.exists():
            raise RuntimeError("document assembly did not produce a file")

        warnings = [r for r in runs.values() if r.status in ("degraded", "failed")]
        providers_used = list(dict.fromkeys(e.provider for e in events if e.ok))
        n_sections = sum(1 for r in results.values() if r.block)
        message = (
            f"Created '{plan.title}' ({plan.doc_type.replace('_', ' ')}) with {n_sections} sections after executing "
            f"{len(plan.tasks)} self-planned tasks using {', '.join(providers_used) or 'no LLM'}. "
            f"{len(plan.assumptions)} assumption(s) and {len(plan.ambiguities)} ambiguity resolution(s) are documented in the appendix."
        )
        if warnings:
            message += f" {len(warnings)} task(s) used fallback content - review the flagged sections."
        degraded_llm = "mock" in providers_used and len(self.llm.providers) > 1
        if degraded_llm:  # real providers were configured but all failed -> be honest about quality
            message += " WARNING: configured LLM providers were unavailable, so the offline rule-based fallback wrote the content."

        response = AgentResponse(
            run_id=run_id,
            status="completed_with_warnings" if (warnings or degraded_llm) else "completed",
            message=message,
            plan=PlanSummary(**plan.model_dump(include={"title", "doc_type", "audience", "objective", "assumptions", "ambiguities"})),
            tasks=list(runs.values()),
            document=DocumentInfo(
                filename=f"{_slug(plan.title)}.docx",
                download_url=f"{download_base.rstrip('/')}/documents/{run_id}",
                size_bytes=path.stat().st_size,
                base64=base64.b64encode(path.read_bytes()).decode() if include_base64 else None,
            ),
            llm=LLMSummary(
                providers_used=providers_used,
                calls=len(events),
                retries=sum(max(e.attempts - 1, 0) for e in events),
                provider_failures=sum(1 for e in events if not e.ok),
                events=events,
            ),
            total_ms=int((time.monotonic() - started) * 1000),
        )
        (out_dir / "run.json").write_text(response.model_dump_json(indent=2, exclude={"document": {"base64"}}), encoding="utf-8")
        return response


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:60] or "document"
