"""Built-in tools. Tools mix LLM creativity with deterministic code where correctness matters
(budget arithmetic, calendar dates), which is a deliberate hallucination-control choice."""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.agent import prompts
from app.agent.models import Block, PlanTask, ToolResult
from app.documents.builder import DocumentBuilder

from .registry import ToolContext, ToolRegistry, ToolSpec

TableKind = Literal["risks", "kpis", "requirements", "raci", "action_items", "comparison", "generic"]


def _short(text: str, n: int = 450) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _summarise(block: Block) -> str:
    if block.kind == "table":
        head = " | ".join(block.columns)
        return _short(f"Table '{block.heading}' [{head}]: " + "; ".join(" / ".join(r) for r in block.rows[:4]))
    return _short(" ".join(block.paragraphs) + " " + " ".join(block.bullets))


# ───────────────────────────── research_context ─────────────────────────────
class ResearchArgs(BaseModel):
    topic: str
    focus: str = ""


async def research_context(args: ResearchArgs, ctx: ToolContext) -> ToolResult:
    hits = ctx.kb.search(f"{args.topic} {args.focus} {ctx.request}", k=2)
    if not hits:
        return ToolResult(summary="No internal benchmark data matched; reason qualitatively and label estimates.")
    facts = [f for h in hits for f in h["facts"]]
    return ToolResult(
        summary="Reference facts:\n" + "\n".join(f"- {f}" for f in facts),
        sources=[h["title"] for h in hits],
    )


def research_fallback(args: ResearchArgs, task: PlanTask) -> ToolResult:
    return ToolResult(summary="(research unavailable)", degraded=True)


# ───────────────────────────── write_section ─────────────────────────────
class SectionArgs(BaseModel):
    heading: str
    instructions: str = "Write a clear, useful section for this document."
    style: Literal["prose", "bullets", "mixed"] = "mixed"


class SectionContent(BaseModel):
    paragraphs: list[str] = Field(default_factory=list)
    bullets: list[str] = Field(default_factory=list)
    callout: str | None = None

    @field_validator("paragraphs", "bullets", mode="before")
    @classmethod
    def _as_str_list(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return [str(x) for x in v]

    @model_validator(mode="after")
    def _non_empty(self) -> "SectionContent":
        if not (self.paragraphs or self.bullets):
            raise ValueError("section has no content")
        return self


async def write_section(args: SectionArgs, ctx: ToolContext) -> ToolResult:
    user = prompts.tool_user(
        ctx, tool_fields={"HEADING": args.heading, "INSTRUCTIONS": args.instructions, "STYLE": args.style}
    )
    c = await ctx.llm.complete_json(
        purpose="section", system=prompts.SECTION_SYSTEM, user=user, model_cls=SectionContent, events=ctx.events
    )
    block = Block(kind="section", heading=args.heading, paragraphs=c.paragraphs, bullets=c.bullets, callout=c.callout)
    return ToolResult(block=block, summary=_summarise(block))


def section_fallback(args: SectionArgs, task: PlanTask) -> ToolResult:
    block = Block(
        kind="section",
        heading=args.heading,
        paragraphs=[
            "[Automatic drafting was unavailable for this section: every LLM provider failed.] "
            f"Intended content: {args.instructions}"
        ],
    )
    return ToolResult(block=block, summary=_summarise(block), degraded=True)


# ───────────────────────────── build_table ─────────────────────────────
class TableArgs(BaseModel):
    heading: str
    kind: TableKind = "generic"
    instructions: str = "Build a useful table for this document."
    columns: list[str] = Field(default_factory=list)


class TableContent(BaseModel):
    columns: list[str] = Field(min_length=2)
    rows: list[list[Any]] = Field(min_length=1)
    caption: str | None = None

    @model_validator(mode="after")
    def _normalise(self) -> "TableContent":
        n = len(self.columns)
        self.columns = [str(c) for c in self.columns]
        fixed = []
        for row in self.rows:
            cells = [str(c) for c in (row if isinstance(row, list) else [row])]
            fixed.append((cells + [""] * n)[:n])  # pad / truncate ragged rows from sloppy models
        self.rows = fixed
        return self


async def build_table(args: TableArgs, ctx: ToolContext) -> ToolResult:
    fields = {"HEADING": args.heading, "KIND": args.kind, "INSTRUCTIONS": args.instructions}
    if args.columns:
        fields["COLUMNS"] = ", ".join(args.columns)
    t = await ctx.llm.complete_json(
        purpose="table", system=prompts.TABLE_SYSTEM, user=prompts.tool_user(ctx, tool_fields=fields),
        model_cls=TableContent, events=ctx.events,
    )
    block = Block(kind="table", heading=args.heading, columns=t.columns, rows=t.rows, caption=t.caption)
    return ToolResult(block=block, summary=_summarise(block))


def table_fallback(args: TableArgs, task: PlanTask) -> ToolResult:
    block = Block(
        kind="table", heading=args.heading, columns=["Item", "Details"],
        rows=[["Not generated", "Automatic drafting was unavailable (all LLM providers failed)."]],
    )
    return ToolResult(block=block, summary=_summarise(block), degraded=True)


# ───────────────────────────── build_budget ─────────────────────────────
class BudgetArgs(BaseModel):
    heading: str = "Budget Estimate"
    instructions: str = "Estimate the main cost items."
    currency: str = "USD"
    contingency_pct: float = Field(default=10.0, ge=0, le=50)
    cap: float | None = Field(default=None, description="Optional hard budget cap")


class BudgetItem(BaseModel):
    item: str
    category: str = "General"
    quantity: float = Field(default=1, gt=0)
    unit_cost: float = Field(ge=0)

    @field_validator("quantity", "unit_cost", mode="before")
    @classmethod
    def _num(cls, v: Any) -> Any:
        if isinstance(v, str):
            return float(re.sub(r"[^\d.\-]", "", v) or 0)
        return v


class BudgetContent(BaseModel):
    items: list[BudgetItem] = Field(min_length=3)
    note: str | None = None


def _money(cur: str, x: float) -> str:
    return f"{cur} {x:,.0f}"


def compute_budget(items: list[BudgetItem], currency: str, contingency_pct: float) -> tuple[list[list[str]], float]:
    """Deterministic arithmetic: never trust an LLM to add numbers."""
    rows, subtotal = [], 0.0
    for it in items:
        line = it.quantity * it.unit_cost
        subtotal += line
        qty = f"{it.quantity:g}"
        rows.append([it.category, it.item, qty, _money(currency, it.unit_cost), _money(currency, line)])
    contingency = subtotal * contingency_pct / 100
    total = subtotal + contingency
    rows.append(["", "Subtotal", "", "", _money(currency, subtotal)])
    rows.append(["", f"Contingency ({contingency_pct:g}%)", "", "", _money(currency, contingency)])
    rows.append(["", "TOTAL", "", "", _money(currency, total)])
    return rows, total


async def build_budget(args: BudgetArgs, ctx: ToolContext) -> ToolResult:
    fields = {"HEADING": args.heading, "INSTRUCTIONS": args.instructions, "CURRENCY": args.currency}
    if args.cap:
        fields["BUDGET_CAP"] = f"{args.cap:g}"
    b = await ctx.llm.complete_json(
        purpose="budget", system=prompts.BUDGET_SYSTEM, user=prompts.tool_user(ctx, tool_fields=fields),
        model_cls=BudgetContent, events=ctx.events,
    )
    rows, total = compute_budget(b.items, args.currency, args.contingency_pct)
    notes = [b.note] if b.note else []
    if args.cap and total > args.cap:
        notes.append(
            f"Estimated total ({_money(args.currency, total)}) exceeds the stated cap of "
            f"{_money(args.currency, args.cap)}; scope reduction or phasing is recommended."
        )
    block = Block(
        kind="table", heading=args.heading,
        columns=["Category", "Item", "Qty", "Unit cost", "Line total"],
        rows=rows, bold_last_rows=3, intro=" ".join(notes) or None,
        caption="Estimates are indicative and exclude taxes.",
    )
    return ToolResult(block=block, summary=f"Budget total {_money(args.currency, total)} across {len(b.items)} line items.")


def budget_fallback(args: BudgetArgs, task: PlanTask) -> ToolResult:
    return table_fallback(TableArgs(heading=args.heading), task)


# ───────────────────────────── build_timeline ─────────────────────────────
class TimelineArgs(BaseModel):
    heading: str = "Delivery Timeline"
    instructions: str = "Lay out the delivery phases."
    start_date: date | None = None
    total_weeks: int | None = Field(default=None, ge=1, le=104)


class Phase(BaseModel):
    name: str
    duration_weeks: int = Field(ge=1, le=52)
    deliverable: str = ""
    owner: str = "Project team"

    @field_validator("duration_weeks", mode="before")
    @classmethod
    def _int(cls, v: Any) -> Any:
        if isinstance(v, str):
            m = re.search(r"\d+", v)
            return int(m.group()) if m else 1
        return max(1, round(v)) if isinstance(v, float) else v


class TimelineContent(BaseModel):
    phases: list[Phase] = Field(min_length=2)
    note: str | None = None


def next_monday(today: date | None = None) -> date:
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def compute_timeline(phases: list[Phase], start: date) -> list[list[str]]:
    """Deterministic calendar maths (Mon-Fri weeks, sequential phases)."""
    rows, cursor = [], start
    for i, p in enumerate(phases, 1):
        end = cursor + timedelta(weeks=p.duration_weeks) - timedelta(days=3)  # Friday of last week
        rows.append([
            f"{i}. {p.name}", cursor.strftime("%d %b %Y"), end.strftime("%d %b %Y"),
            f"{p.duration_weeks} wk", p.deliverable, p.owner,
        ])
        cursor += timedelta(weeks=p.duration_weeks)
    return rows


async def build_timeline(args: TimelineArgs, ctx: ToolContext) -> ToolResult:
    fields = {"HEADING": args.heading, "INSTRUCTIONS": args.instructions}
    if args.total_weeks:
        fields["TOTAL_WEEKS"] = str(args.total_weeks)
    t = await ctx.llm.complete_json(
        purpose="timeline", system=prompts.TIMELINE_SYSTEM, user=prompts.tool_user(ctx, tool_fields=fields),
        model_cls=TimelineContent, events=ctx.events,
    )
    start = args.start_date or next_monday()
    rows = compute_timeline(t.phases, start)
    total = sum(p.duration_weeks for p in t.phases)
    notes = [t.note] if t.note else []
    if args.total_weeks and total != args.total_weeks:
        notes.append(f"Note: phases total {total} weeks versus the requested {args.total_weeks}.")
    block = Block(
        kind="table", heading=args.heading,
        columns=["Phase", "Start", "End", "Duration", "Deliverable", "Owner"],
        rows=rows, intro=" ".join(notes) or None,
        caption=f"Total duration: {total} weeks, starting {start.strftime('%d %b %Y')}.",
    )
    return ToolResult(block=block, summary=f"Timeline of {len(t.phases)} phases, {total} weeks from {start.isoformat()}.")


def timeline_fallback(args: TimelineArgs, task: PlanTask) -> ToolResult:
    return table_fallback(TableArgs(heading=args.heading), task)


# ───────────────────────────── assemble_document ─────────────────────────────
class AssembleArgs(BaseModel):
    pass


async def assemble_document(args: AssembleArgs, ctx: ToolContext) -> ToolResult:
    assert ctx.output_path is not None, "output_path must be set by the executor"
    blocks = [
        ctx.results[t.id].block
        for t in ctx.plan.tasks
        if t.id in ctx.results and ctx.results[t.id].block is not None
    ]
    notes = [f"Task '{r.title}' used fallback content ({r.error or 'LLM unavailable'})."
             for r in ctx.runs.values() if r.status == "degraded"]
    sources = sorted({s for r in ctx.results.values() for s in r.sources})
    DocumentBuilder().build(ctx.plan, [b for b in blocks if b], ctx.output_path, notes=notes, sources=sources)
    return ToolResult(summary=f"Document with {len(blocks)} sections written.", artifact_path=str(ctx.output_path))


# ───────────────────────────── registry factory ─────────────────────────────
def build_default_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(ToolSpec(
        "research_context", "Look up reference facts/benchmarks (mock knowledge base) to ground later sections.",
        '{"topic": str, "focus": str}', ResearchArgs, research_context, research_fallback, produces_block=False))
    r.register(ToolSpec(
        "write_section", "Write one prose/bullet section of the document.",
        '{"heading": str, "instructions": str, "style": "prose|bullets|mixed"}',
        SectionArgs, write_section, section_fallback))
    r.register(ToolSpec(
        "build_table", "Build a table (risks, KPIs, requirements, RACI, action items, comparison).",
        '{"heading": str, "kind": "risks|kpis|requirements|raci|action_items|comparison|generic", "instructions": str}',
        TableArgs, build_table, table_fallback))
    r.register(ToolSpec(
        "build_budget", "Estimate costs; the LLM proposes line items, code computes totals and contingency.",
        '{"heading": str, "currency": "USD|INR|EUR|...", "contingency_pct": number, "cap": number|null, "instructions": str}',
        BudgetArgs, build_budget, budget_fallback))
    r.register(ToolSpec(
        "build_timeline", "Create a phased schedule; the LLM proposes phases, code computes calendar dates.",
        '{"heading": str, "total_weeks": int|null, "start_date": "YYYY-MM-DD"|null, "instructions": str}',
        TimelineArgs, build_timeline, timeline_fallback))
    r.register(ToolSpec(
        "assemble_document", "FINAL step: render all produced sections into the .docx file.",
        "{}", AssembleArgs, assemble_document, None, produces_block=False))
    return r
