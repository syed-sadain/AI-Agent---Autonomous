"""Public API contracts."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.agent.models import Ambiguity, TaskRun
from app.llm.base import LLMEvent


class AgentRequest(BaseModel):
    request: str = Field(..., min_length=15, max_length=4000, examples=["Create a project plan for launching a mobile app in 12 weeks"])
    include_base64: bool = Field(False, description="Embed the .docx in the JSON response as base64")

    @field_validator("request")
    @classmethod
    def _guardrail(cls, v: str) -> str:
        v = " ".join(v.split())  # collapse newlines/tabs so prompts stay well-formed
        if len(v.split()) < 4:
            raise ValueError("Please describe the document you need in at least 4 words.")
        if not any(ch.isalpha() for ch in v):
            raise ValueError("Request must contain text.")
        return v


class PlanSummary(BaseModel):
    title: str
    doc_type: str
    audience: str
    objective: str
    assumptions: list[str]
    ambiguities: list[Ambiguity]


class DocumentInfo(BaseModel):
    filename: str
    download_url: str
    size_bytes: int
    base64: str | None = None


class LLMSummary(BaseModel):
    providers_used: list[str]
    calls: int
    retries: int
    provider_failures: int
    events: list[LLMEvent]


class AgentResponse(BaseModel):
    run_id: str
    status: Literal["completed", "completed_with_warnings"]
    message: str
    plan: PlanSummary
    tasks: list[TaskRun]
    document: DocumentInfo
    llm: LLMSummary
    total_ms: int
