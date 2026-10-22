from datetime import date

from app.agent.models import Plan, PlanTask
from app.agent.planner import Planner
from app.config import Settings
from app.tools.builtin import BudgetItem, Phase, build_default_registry, compute_budget, compute_timeline, next_monday


def test_budget_math_is_exact():
    rows, total = compute_budget(
        [BudgetItem(item="a", quantity=2, unit_cost=100), BudgetItem(item="b", quantity=1, unit_cost=50),
         BudgetItem(item="c", quantity=10, unit_cost=5)], "USD", 10)
    assert total == 330 and rows[-3][-1] == "USD 300" and rows[-1][-1] == "USD 330"


def test_timeline_dates_are_sequential_mon_to_fri():
    rows = compute_timeline([Phase(name="A", duration_weeks=2), Phase(name="B", duration_weeks=1)], date(2026, 1, 5))
    assert rows[0][1:3] == ["05 Jan 2026", "16 Jan 2026"]  # Mon -> Fri two weeks later
    assert rows[1][1] == "19 Jan 2026"


def test_next_monday_is_always_a_future_monday():
    d = next_monday(date(2026, 10, 22))
    assert d.weekday() == 0 and d > date(2026, 10, 22)


def make_planner():
    s = Settings(llm_providers="mock", max_tasks=6)
    return Planner(llm=None, registry=build_default_registry(), settings=s)  # type: ignore[arg-type]


def test_normalise_repairs_a_bad_llm_plan():
    plan = Plan(title="t", objective="o", tasks=[
        PlanTask(id="1", title="a", tool="write_section", depends_on=["2", "ghost"]),
        PlanTask(id="2", title="b", tool="write_section", depends_on=["1"]),            # cycle
        PlanTask(id="2", title="dup id", tool="build_table"),                            # duplicate id
        PlanTask(id="9", title="hallucinated", tool="send_email"),                       # unknown tool
        PlanTask(id="x", title="premature end", tool="assemble_document"),               # LLM-placed final
    ])
    fixed = make_planner().normalise(plan)
    tools = [t.tool for t in fixed.tasks]
    assert "send_email" not in tools and tools.count("assemble_document") == 1 and tools[-1] == "assemble_document"
    assert len({t.id for t in fixed.tasks}) == len(fixed.tasks)
    assert all(d in {t.id for t in fixed.tasks} for t in fixed.tasks for d in t.depends_on)
    assert fixed.tasks[0].depends_on == [] or "ghost" not in fixed.tasks[0].depends_on


def test_plan_is_capped_to_max_tasks():
    plan = Plan(title="t", objective="o", tasks=[PlanTask(id=str(i), title="s", tool="write_section") for i in range(30)])
    assert len(make_planner().normalise(plan).tasks) == 6


def test_fallback_plan_is_executable():
    plan = make_planner().fallback_plan("Write something about our onboarding process please")
    assert plan.tasks[-1].tool == "assemble_document" and len(plan.tasks) >= 4
