"""Contracts for one group assessment; scores are deliberately absent."""
from typing import Literal
from pydantic import Field
from pydantic import ConfigDict
from .models import Strict, Evidence

class SearchRequest(Strict):
    config_version: int = Field(ge=1)
    candidate_limit: int = Field(default=20,ge=1,le=20)

class CriterionAssessment(Strict):
    criterion_id: str
    status: Literal['MET','PARTIAL','NOT_MET','UNKNOWN']
    explanation: str = Field(min_length=1,max_length=500)
    evidence_refs: list[str] = Field(max_length=3)
    questions: list[str] = Field(max_length=3)

class CandidateAssessment(Strict):
    candidate_id: str
    assessments: list[CriterionAssessment]
    strengths: list[str] = Field(max_length=5)
    gaps: list[str] = Field(max_length=5)
    recommendation: str = Field(max_length=600)

class BatchAssessment(Strict):
    items: list[CandidateAssessment] = Field(min_length=1,max_length=20)

class JudgeCriterion(Strict):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    criterion_id: str = Field(alias='c')
    status: Literal['MET','PARTIAL','NOT_MET','UNKNOWN'] = Field(alias='s')
    evidence_refs: list[str] = Field(alias='e',max_length=3)
    explanation: str = Field(alias='why',min_length=1,max_length=140)

class JudgeCandidate(Strict):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    candidate_id: str = Field(alias='id')
    assessments: list[JudgeCriterion] = Field(alias='a')

class JudgeBatch(Strict):
    items: list[JudgeCandidate] = Field(min_length=1,max_length=20)
