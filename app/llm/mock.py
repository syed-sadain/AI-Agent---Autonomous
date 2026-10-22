"""Offline, deterministic 'LLM'.

Why it exists: (1) tests and CI need zero network, (2) the demo still works if every free-tier quota is
exhausted (last link of the fallback chain), (3) it documents the exact JSON contract each prompt expects.
It is rule-based, so content is intentionally generic; real models (Groq/Gemini/Ollama) give real quality.
"""
from __future__ import annotations

import json
import re

from .base import PermanentLLMError


def _field(text: str, tag: str, default: str = "") -> str:
    m = re.search(rf"^{tag}: (.*)$", text, re.MULTILINE)
    return m.group(1).strip() if m else default


def _context(text: str) -> str:
    return text.split("CONTEXT FROM PRIOR STEPS:", 1)[-1].strip()


def _topic(request: str) -> str:
    m = (re.search(r"\b(?:about|regarding)\b\s+(.+)", request, re.IGNORECASE)
         or re.search(r"\b(?:for|on)\b\s+(.+)", request, re.IGNORECASE))
    topic = (m.group(1) if m else request).strip()
    topic = re.split(r"[.;,]| with | in \d| that | which ", topic)[0].strip()
    return (topic[:70] or "the initiative").rstrip(" .,")


def _doc_type(r: str) -> str:
    r = r.lower()
    rules = [
        ("meeting_minutes", ["meeting", "minutes"]),
        ("sop", ["sop", "standard operating", "procedure"]),
        ("technical_design", ["technical design", "architecture", "design doc", "system design"]),
        ("product_spec", ["product spec", "prd", "specification"]),
        ("proposal", ["proposal", "pitch"]),
        ("project_plan", ["project plan", "roadmap", " plan "]),
        ("business_report", ["report", "analysis", "board", "executive"]),
    ]
    padded = f" {r} "
    for dt, kws in rules:
        if any(k in padded for k in kws):
            return dt
    return "business_report"


# ─────────────────────────────── planning ───────────────────────────────
def _analyse(r: str, doc_type: str) -> tuple[list[str], list[dict]]:
    low = r.lower()
    assumptions: list[str] = []
    amb: list[dict] = []
    small = bool(re.search(r"small|limited|tight|low budget|cheap|lean", low))
    premium = bool(re.search(r"enterprise|premium|best[- ]in[- ]class|world[- ]class", low))
    rushed = bool(re.search(r"next month|asap|urgent|quickly|this week|immediately", low))
    undecided = bool(re.search(r"not sure|undecided|either|build or buy|buy or build|should we", low))

    if small and premium:
        amb.append({"issue": "Budget is described as small, but enterprise-grade quality is expected.",
                    "decision": "Adopted a buy-first approach with phased scope; enterprise features are limited to security, SLAs and audit logging in phase 1."})
    if rushed:
        amb.append({"issue": "A very short launch window conflicts with the breadth of scope requested.",
                    "decision": "Planned a pilot-first launch inside the window and scheduled the full rollout afterwards."})
    if undecided:
        amb.append({"issue": "The request leaves the key decision open (for example build versus buy).",
                    "decision": "Compared the options side by side and recommended the lower-risk one, flagged for confirmation."})
    if re.search(r"whatever|what you think|anything (that )?matters|include what", low):
        amb.append({"issue": "Document contents were left to the agent's judgement.",
                    "decision": "Chose a structure suited to the stated audience and omitted low-value detail."})
    if not re.search(r"\d[\d,]*\s?(k|m|usd|inr|eur|dollars|lakh|rupees|\$|€|₹)|[$€₹]\s?\d", low):
        assumptions.append("No budget figure was given; costs are indicative estimates with a 10% contingency.")
    if not re.search(r"\b(20\d\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b", low):
        assumptions.append("No start date was given; schedules start on the next Monday.")
    team = re.search(r"team of (\d+)|(\d+)[- ](?:person|people|member)", low)
    if team:
        assumptions.append(f"Resourcing assumes a team of {team.group(1) or team.group(2)} people.")
    if doc_type == "meeting_minutes":
        assumptions.append("Attendees, decisions and action items are placeholders because no transcript was provided.")
    return assumptions, amb


def _audience(r: str) -> str:
    low = r.lower()
    if "board" in low:
        return "Board of Directors and executive leadership"
    if "client" in low or "customer" in low and "proposal" in low:
        return "Client stakeholders"
    if "engineer" in low or "developer" in low:
        return "Engineering team"
    return "Management and project stakeholders"


OUTLINES: dict[str, list[tuple[str, str, dict]]] = {
    "proposal": [
        ("write_section", "Background and Problem Statement", {}),
        ("write_section", "Proposed Solution", {}),
        ("write_section", "Scope of Work", {"style": "bullets"}),
        ("build_timeline", "Delivery Timeline", {}),
        ("build_budget", "Investment", {}),
        ("build_table", "Risks and Mitigations", {"kind": "risks"}),
        ("build_table", "Success Metrics", {"kind": "kpis"}),
        ("write_section", "Next Steps", {}),
    ],
    "project_plan": [
        ("write_section", "Project Overview and Objectives", {}),
        ("write_section", "Scope and Deliverables", {"style": "bullets"}),
        ("build_timeline", "Work Plan and Milestones", {}),
        ("build_budget", "Resourcing and Budget", {}),
        ("build_table", "Roles and Responsibilities", {"kind": "raci"}),
        ("build_table", "Risks and Mitigations", {"kind": "risks"}),
        ("write_section", "Governance and Communication", {}),
    ],
    "meeting_minutes": [
        ("write_section", "Meeting Overview", {}),
        ("write_section", "Agenda and Discussion", {}),
        ("write_section", "Decisions Made", {"style": "bullets"}),
        ("build_table", "Action Items", {"kind": "action_items"}),
        ("write_section", "Next Meeting", {}),
    ],
    "sop": [
        ("write_section", "Purpose and Scope", {}),
        ("build_table", "Roles and Responsibilities", {"kind": "raci"}),
        ("write_section", "Procedure", {"style": "bullets"}),
        ("write_section", "Exceptions and Escalation", {}),
        ("build_table", "Quality Checks", {"kind": "kpis"}),
        ("write_section", "Review and Revision", {}),
    ],
    "technical_design": [
        ("write_section", "Context and Goals", {}),
        ("build_table", "Requirements", {"kind": "requirements"}),
        ("write_section", "Architecture Overview", {}),
        ("write_section", "Data and Interfaces", {}),
        ("write_section", "Security and Reliability", {}),
        ("build_timeline", "Delivery Plan", {}),
        ("build_table", "Risks and Open Questions", {"kind": "risks"}),
    ],
    "product_spec": [
        ("write_section", "Product Vision and Problem", {}),
        ("write_section", "Users and Use Cases", {}),
        ("build_table", "Functional Requirements", {"kind": "requirements"}),
        ("write_section", "Non-Functional Requirements", {"style": "bullets"}),
        ("build_timeline", "Release Plan", {}),
        ("build_table", "Success Metrics", {"kind": "kpis"}),
        ("build_table", "Risks", {"kind": "risks"}),
    ],
    "business_report": [
        ("write_section", "Background and Context", {}),
        ("write_section", "Analysis of the Current Situation", {}),
        ("build_table", "Options Considered", {"kind": "comparison"}),
        ("write_section", "Recommendation", {}),
        ("build_table", "Risks and Mitigations", {"kind": "risks"}),
        ("write_section", "Implementation Roadmap", {}),
    ],
}


def _mock_plan(request: str) -> dict:
    low = request.lower()
    doc_type = _doc_type(request)
    topic = _topic(request)
    currency = "INR" if re.search(r"\binr\b|₹|rupee|lakh", low) else "EUR" if re.search(r"\beur\b|€|euro", low) else "USD"
    weeks = re.search(r"(\d+)[ -]?week", low)
    total_weeks = int(weeks.group(1)) if weeks else None
    assumptions, ambiguities = _analyse(request, doc_type)

    outline = list(OUTLINES[doc_type])
    if doc_type == "business_report":  # add finance/schedule only when the request signals them
        if re.search(r"budget|cost|price|cheap|spend", low):
            outline.insert(4, ("build_budget", "Indicative Budget", {}))
        if re.search(r"month|week|launch|deadline|timeline|by ", low):
            outline.append(("build_timeline", "Delivery Timeline", {}))
    if doc_type == "business_report" and not re.search(r"build.*buy|buy.*build|option|compare|vs\b|versus", low):
        outline = [o for o in outline if o[1] != "Options Considered"]

    tasks = [{"id": "t1", "title": "Gather reference facts", "tool": "research_context",
              "args": {"topic": topic, "focus": doc_type.replace("_", " ")}, "depends_on": []}]
    content_ids = []
    for n, (tool, heading, extra) in enumerate(outline, start=3):
        args = {"heading": heading, "instructions": f"Cover '{heading}' for: {topic}.", **extra}
        if tool == "build_budget":
            args.update(currency=currency, contingency_pct=10)
        if tool == "build_timeline" and total_weeks:
            args["total_weeks"] = total_weeks
        tasks.append({"id": f"t{n}", "title": heading, "tool": tool, "args": args, "depends_on": ["t1"]})
        content_ids.append(f"t{n}")
    tasks.insert(1, {"id": "t2", "title": "Executive Summary", "tool": "write_section",
                     "args": {"heading": "Executive Summary", "instructions": f"Summarise purpose, key findings and recommendation for {topic}."},
                     "depends_on": ["t1", *content_ids]})
    title_core = topic[:1].upper() + topic[1:]
    labels = {"proposal": "Proposal", "project_plan": "Project Plan", "meeting_minutes": "Meeting Minutes", "sop": "SOP",
              "technical_design": "Technical Design", "product_spec": "Product Specification", "business_report": "Business Report"}
    return {
        "title": f"{labels[doc_type]}: {title_core}",
        "doc_type": doc_type,
        "audience": _audience(request),
        "objective": f"Deliver a clear {labels[doc_type].lower()} for {topic}, ready for stakeholder review.",
        "assumptions": assumptions,
        "ambiguities": ambiguities,
        "tasks": tasks,
    }


# ─────────────────────────────── sections ───────────────────────────────
def _mock_section(text: str) -> dict:
    heading, request, doc_type = _field(text, "HEADING"), _field(text, "REQUEST"), _field(text, "DOC_TYPE")
    objective, audience, style = _field(text, "OBJECTIVE"), _field(text, "AUDIENCE"), _field(text, "STYLE", "mixed")
    ctx = _context(text)
    topic, h = _topic(request), heading.lower()
    facts = [l[2:].strip() for l in ctx.splitlines() if l.startswith("- ")]
    covered = re.findall(r"^\[[^\]]*?: ([^\]]+)\]", ctx, re.MULTILINE)
    buy = bool(re.search(r"build.*buy|buy.*build", request.lower()))

    if "executive summary" in h:
        paras = [
            f"This document presents a {doc_type.replace('_', ' ')} for {topic}, prepared for {audience.lower()}. {objective}",
            "It covers " + (", ".join(c.lower() for c in covered[:6]) if covered else "the context, approach, and next steps")
            + ", and records every assumption the author made so reviewers can challenge them.",
        ]
        bullets = ["Approach is phased to reduce delivery risk and keep early commitments small.",
                   "Costs and dates are indicative and are computed transparently in this document.",
                   "Open points are listed in the appendix for confirmation."]
        callout = ("Recommended path: start with a buy-first pilot, measure results, then decide on deeper investment."
                   if buy else "Recommended path: approve the phased plan and confirm the assumptions listed in the appendix.")
        return {"paragraphs": paras, "bullets": bullets, "callout": callout}

    if any(k in h for k in ("background", "context", "overview", "vision", "purpose", "problem")):
        paras = [
            f"{topic[:1].upper() + topic[1:]} has been identified as a priority. The organisation needs a clear, shared understanding of the problem, the people affected, and what success looks like.",
            "Current ways of working are largely manual and inconsistent, which creates avoidable cost, delay, and risk. Addressing this now creates a foundation for future improvements.",
        ]
        return {"paragraphs": paras, "bullets": facts[:2] if style != "prose" else [], "callout": None}

    if any(k in h for k in ("recommendation", "solution", "approach", "architecture", "procedure")):
        paras = [
            f"The recommended approach for {topic} is incremental: deliver a small, valuable first release, learn from real usage, then expand.",
            "Each stage has an owner, an observable completion signal, and a go/no-go checkpoint so that stakeholders stay in control of scope and spend.",
        ]
        bullets = (["Buy or configure proven components first; build custom only where it creates differentiation."] if buy else
                   ["Start with the smallest end-to-end slice that proves value.",
                    "Define one accountable owner per step and one measurable outcome per milestone.",
                    "Review results at each checkpoint before committing further budget."])
        bullets += facts[:1]
        return {"paragraphs": paras, "bullets": bullets, "callout": None}

    if any(k in h for k in ("scope", "deliverable", "decision", "non-functional")):
        return {"paragraphs": [f"The following items are in scope for {topic}."],
                "bullets": ["Discovery, requirements and success criteria agreed with stakeholders",
                            "Design and implementation of the agreed first release",
                            "Testing, acceptance and handover with documentation",
                            "Out of scope: items not listed here, handled through change control"], "callout": None}

    if any(k in h for k in ("next", "governance", "review", "roadmap", "escalation", "exceptions", "analysis", "agenda", "discussion", "users")):
        paras = [f"For {topic}, work proceeds in short cycles with a regular status review and a single escalation path for blockers and scope changes.",
                 "Progress, risks and decisions are recorded in one place and shared with stakeholders after every review."]
        return {"paragraphs": paras, "bullets": facts[:2] if style != "prose" else [],
                "callout": "Confirm the open assumptions in the appendix before work begins."}

    return {"paragraphs": [f"This section covers {heading.lower()} for {topic}. {_field(text, 'INSTRUCTIONS')}"],
            "bullets": facts[:2], "callout": None}


# ─────────────────────────────── tables ───────────────────────────────
def _mock_table(text: str) -> dict:
    kind, heading, request = _field(text, "KIND", "generic"), _field(text, "HEADING"), _field(text, "REQUEST")
    topic = _topic(request)
    buy = bool(re.search(r"build.*buy|buy.*build", request.lower()))
    t = {
        "risks": (["Risk", "Likelihood", "Impact", "Mitigation"], [
            ["Scope grows beyond the agreed first release", "High", "High", "Written must-have list and formal change control"],
            ["Key people are unavailable at critical points", "Medium", "High", "Name deputies and book review slots early"],
            [f"Quality of results for {topic} is below expectations", "Medium", "High", "Pilot with a small group and measure before scaling"],
            ["Costs exceed estimates", "Medium", "Medium", "Hold 10% contingency; review spend at each milestone"],
            ["Data, security or compliance gaps found late", "Low", "High", "Run a security and privacy check during design"]]),
        "kpis": (["Metric", "Target", "How measured", "Frequency"], [
            ["Delivery against plan", "≥ 90% of milestones on time", "Milestone tracker", "Weekly"],
            ["Stakeholder satisfaction", "≥ 4 / 5", "Short survey", "Monthly"],
            ["Defects found after release", "< 5 critical", "Issue tracker", "Weekly"],
            ["Budget variance", "within ±10%", "Finance report", "Monthly"]]),
        "requirements": (["ID", "Requirement", "Priority", "Notes"], [
            ["R1", f"The solution must support the core workflow for {topic}", "Must", "Defines the first release"],
            ["R2", "Access is role-based and auditable", "Must", "Security baseline"],
            ["R3", "Key actions are logged and reportable", "Should", "Supports review"],
            ["R4", "The solution integrates with existing systems through documented interfaces", "Should", "Avoids rework"],
            ["R5", "Users can export their data", "Could", "Nice to have"]]),
        "raci": (["Activity", "Responsible", "Accountable", "Consulted", "Informed"], [
            ["Define scope and success criteria", "Project lead", "Sponsor", "Stakeholders", "Team"],
            ["Design and build", "Delivery team", "Project lead", "Subject experts", "Sponsor"],
            ["Testing and acceptance", "QA / reviewers", "Project lead", "Users", "Sponsor"],
            ["Approve release", "Sponsor", "Sponsor", "Project lead", "Stakeholders"]]),
        "action_items": (["Action", "Owner", "Due", "Status"], [
            ["Circulate these minutes for review", "Meeting chair", "+2 days", "Open"],
            [f"Confirm scope and owners for {topic}", "Project lead", "+1 week", "Open"],
            ["Share updated budget and timeline", "Finance partner", "+1 week", "Open"],
            ["Book follow-up review", "Coordinator", "+3 days", "Open"]]),
        "comparison": ((["Criterion", "Build in-house", "Buy / configure a product", "Advantage"] if buy else
                        ["Criterion", "Option A: Current approach", "Option B: Proposed approach", "Advantage"]), [
            ["Time to first value", "Months", "Weeks", "Option B"],
            ["Upfront cost", "High", "Low to medium", "Option B"],
            ["Customisation", "Full control", "Limited to product features", "Option A"],
            ["Ongoing effort", "Team maintains everything", "Vendor maintains platform", "Option B"],
            ["Delivery risk", "Higher", "Lower", "Option B"]]),
        "generic": (["Item", "Details", "Owner"], [
            [heading or "Item 1", f"Key detail for {topic}", "Project lead"],
            ["Item 2", "Supporting detail", "Delivery team"],
            ["Item 3", "Follow-up detail", "Sponsor"]]),
    }
    cols, rows = t.get(kind, t["generic"])
    return {"columns": cols, "rows": rows, "caption": None}


# ─────────────────────────────── budget / timeline ───────────────────────────────
def _mock_budget(text: str) -> dict:
    cur, request = _field(text, "CURRENCY", "USD"), _field(text, "REQUEST").lower()
    scale = {"INR": 80, "EUR": 0.92}.get(cur, 1) * (0.5 if re.search(r"small|limited|tight|lean|cheap", request) else 1)
    base = [
        ("Discovery & planning", "Services", 40, 70), ("Design / configuration", "Services", 80, 70),
        ("Implementation", "Services", 160, 75), ("Software licences / tooling", "Tools", 12, 300),
        ("Testing & quality assurance", "Services", 50, 60), ("Training & change management", "Services", 30, 60),
        ("Project management", "Services", 60, 80),
    ]
    items = [{"item": n, "category": c, "quantity": q, "unit_cost": round(u * scale, 2)} for n, c, q, u in base]
    return {"items": items, "note": "Unit costs are indicative day/hour rates and monthly licence fees; tune them with real quotes."}


def _allocate(total: int, weights: list[int]) -> list[int]:
    s = sum(weights)
    alloc = [max(1, round(w / s * total)) for w in weights]
    while sum(alloc) != total:
        i = alloc.index(max(alloc)) if sum(alloc) > total else weights.index(max(weights))
        alloc[i] += -1 if sum(alloc) > total else 1
    return alloc


def _mock_timeline(text: str) -> dict:
    request = _field(text, "REQUEST").lower()
    total = _field(text, "TOTAL_WEEKS")
    if total:
        weeks = int(total)
    elif (m := re.search(r"(\d+)[ -]?week", request)):
        weeks = int(m.group(1))
    elif re.search(r"next month|a month|30 days", request):
        weeks = 5
    else:
        weeks = 10
    if weeks >= 8:
        names, w = ["Discovery & planning", "Design", "Build / implementation", "Testing & QA", "Launch", "Handover & review"], [1, 2, 4, 3, 1, 1]
        deliv = ["Agreed scope & plan", "Approved design", "Working release", "Test report", "Go-live", "Lessons learned"]
    elif weeks >= 5:
        names, w = ["Plan & configure", "Build / pilot", "Test", "Launch", "Review"], [1, 2, 1, 1, 1]
        deliv = ["Scope & setup", "Pilot release", "Test sign-off", "Go-live", "Review notes"]
    else:
        names, w = ["Plan & design", "Build & test", "Launch"], [1, 2, 1]
        deliv = ["Agreed plan", "Tested pilot", "Go-live"]
    if weeks < len(w):
        w = w[:weeks]; names = names[:weeks]; deliv = deliv[:weeks]
    owners = ["Project lead", "Design lead", "Delivery team", "QA lead", "Project lead", "Sponsor"]
    alloc = _allocate(weeks, w)
    phases = [{"name": n, "duration_weeks": a, "deliverable": d, "owner": owners[i % len(owners)]}
              for i, (n, a, d) in enumerate(zip(names, alloc, deliv))]
    return {"phases": phases, "note": None}


# ─────────────────────────────── provider ───────────────────────────────
class MockProvider:
    name = "mock"
    always_available = True

    async def chat(self, messages, *, purpose, json_mode=True, temperature=0.3) -> str:
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user" and "REQUEST:" in m["content"])
        if purpose == "plan":
            data = _mock_plan(user.split("REQUEST:", 1)[1].strip())
        elif purpose == "section":
            data = _mock_section(user)
        elif purpose == "table":
            data = _mock_table(user)
        elif purpose == "budget":
            data = _mock_budget(user)
        elif purpose == "timeline":
            data = _mock_timeline(user)
        else:
            raise PermanentLLMError(f"mock: unknown purpose {purpose!r}")
        return json.dumps(data)

    async def aclose(self) -> None:  # nothing to close
        return None
