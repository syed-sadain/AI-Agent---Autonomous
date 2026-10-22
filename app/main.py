"""FastAPI entrypoint.  Run:  uvicorn app.main:app --reload"""
from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.agent.orchestrator import DocumentAgent
from app.config import get_settings
from app.schemas import AgentRequest, AgentResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("api")
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")  # blocks path traversal


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.agent = DocumentAgent(get_settings())
    log.info("providers: %s", [p.name for p in app.state.agent.llm.providers])
    yield
    await app.state.agent.aclose()


app = FastAPI(
    title="Autonomous AI Agent",
    version="1.0.0",
    description="POST a natural-language request; the agent plans its own TODO list, executes it with tools, and returns a Word document.",
    lifespan=lifespan,
)


@app.get("/health")
async def health(request: Request):
    agent: DocumentAgent = request.app.state.agent
    return {"status": "ok", "providers": [p.name for p in agent.llm.providers], "tools": agent.registry.names()}


@app.post("/agent", response_model=AgentResponse)
async def run_agent(body: AgentRequest, request: Request) -> AgentResponse:
    agent: DocumentAgent = request.app.state.agent
    try:
        return await agent.run(body.request, download_base=str(request.base_url), include_base64=body.include_base64)
    except Exception as exc:  # noqa: BLE001
        log.exception("agent run failed")
        raise HTTPException(status_code=500, detail=f"Agent failed: {exc}") from exc


@app.get("/documents/{run_id}", name="download_document")
async def download_document(run_id: str):
    if not _RUN_ID.match(run_id):
        raise HTTPException(status_code=400, detail="Invalid run id")
    path = get_settings().output_dir / run_id / "document.docx"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Document not found")
    return FileResponse(
        path, filename=f"{run_id[:8]}.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    if not _RUN_ID.match(run_id):
        raise HTTPException(status_code=400, detail="Invalid run id")
    path = get_settings().output_dir / run_id / "run.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))
