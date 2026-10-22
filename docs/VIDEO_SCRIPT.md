# 8-10 minute video script

## 0:00 Live demo (3-4 min)
1. Terminal: `uvicorn app.main:app` → open `/docs`. Say: "One endpoint, POST /agent."
2. **Standard**: run `samples/standard_request.json`. Point at response: `plan.tasks` = the agent's own TODO list, `wave` = parallel groups,
   `status` per task. Download and open the .docx: cover, control table, contents, styled timeline + budget, appendix.
3. **Complex**: run `samples/complex_request.json`. Show `assumptions` and `ambiguities` (small budget vs enterprise quality, next-month launch,
   build-or-buy undecided). Open the docx appendix to show the decisions are documented, not hidden. Show the comparison table and budget
   (note the totals/contingency are computed by code).

## 4:00 What I built (2-3 min) - walk the diagram in README
* Planner (LLM → validated Plan) → Executor (dependency waves, asyncio + semaphore) → Tools → DocumentBuilder.
* Tool design: LLM writes, Python calculates. Show `compute_budget` and `compute_timeline`.
* Planner trust boundary: `normalise()` drops hallucinated tools, breaks cycles, caps size. Executive Summary depends on everything, so it's written last.
* API: FastAPI, Pydantic guardrails, run ids validated, trace persisted to `outputs/<run_id>/run.json`.
* LLM integration: one OpenAI-compatible client covers Groq/Gemini/Ollama.

## 6:30 The improvement: retry & fallback (inside the 'what I built' segment)
* Show `resilient.py`: backoff, self-repair, provider chain, circuit breaker. Why chosen: 10-20 LLM calls/request on free tiers = failures are certain.
* Live proof: `LLM_PROVIDERS=groq,ollama,mock GROQ_API_KEY=bad` → response shows `provider_failures`, run still completes, flagged with a warning.

## 7:30 Debugging insight (1-2 min) - pick the one you actually hit
* **Dead provider multiplies latency.** Symptom: with Ollama off, every task waited through 3 retries plus backoff before falling back.
  Root cause: connection-refused was classified as transient. Fix: `ConnectError` → permanent (skip retries) + circuit breaker.
  Proof: events show `attempts: 1` for the dead provider, and later tasks skip it entirely.
* Alternative I really hit while building: budget totals row orphaned onto the next page. Root cause: Word splits tables across pages freely.
  Fix: `keep_with_next` on rows of short tables.

## 9:00 Tradeoff (1-2 min)
**Autonomous planning vs deterministic workflow.** Fixed templates are predictable but can't handle "include whatever matters".
Free-form LLM plans handle ambiguity but can be invalid. Middle path: let the LLM plan, then validate/repair in code and keep a deterministic
fallback plan. Cost: run-to-run variance. Next: reflection pass and job queue.
