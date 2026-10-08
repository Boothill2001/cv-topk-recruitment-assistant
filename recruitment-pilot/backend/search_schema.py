"""Contracts for one group assessment; scores are deliberately absent."""
from typing import Literal
from pydantic import Field
from pydantic import ConfigDict, computed_field, model_validator
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

class PublicQuote(Strict):
    paragraph_id: str
    quote: str = Field(min_length=1,max_length=500)

class PublicCriterion(Strict):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    criterion_id: str = Field(alias='c')
    status: Literal['MET','PARTIAL','NOT_MET','UNKNOWN'] = Field(alias='s')
    explanation: str = Field(alias='why',min_length=1,max_length=140)
    quotes: list[PublicQuote] = Field(alias='q',max_length=8)
    questions: list[str] = Field(default_factory=list,max_length=3)

    @computed_field
    @property
    def evidence_refs(self) -> list[str]:
        # References are mechanically derived, never a second AI interpretation.
        return list(dict.fromkeys(q.paragraph_id for q in self.quotes))

class PublicCandidate(Strict):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    candidate_id: str = Field(alias='id')
    assessments: list[PublicCriterion] = Field(alias='a')

class PublicJudgeBatch(Strict):
    items: list[PublicCandidate] = Field(min_length=1,max_length=20)

class PublicEvidenceChoice(Strict):
    model_config = ConfigDict(extra='forbid', populate_by_name=True, json_schema_extra={
        'allOf':[{'if':{'properties':{'s':{'const':'UNKNOWN'}}},
                  'then':{'properties':{'q':{'maxItems':0}}},
                  'else':{'properties':{'q':{'minItems':1}}}}]})
    criterion_id: str = Field(alias='c')
    status: Literal['MET','PARTIAL','NOT_MET','UNKNOWN'] = Field(alias='s')
    explanation: str = Field(alias='why',min_length=1,max_length=140)
    evidence_refs: list[str] = Field(alias='q',max_length=8)
    questions: list[str] = Field(default_factory=list,max_length=3)

    @model_validator(mode='after')
    def supported_status(self):
        if (self.status=='UNKNOWN') != (not self.evidence_refs):
            raise ValueError('Missing evidence IDs requires s=UNKNOWN and q=[]. Other statuses require direct evidence IDs.')
        return self

class PublicEvidenceCandidate(Strict):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    candidate_id: str = Field(alias='id')
    assessments: list[PublicEvidenceChoice] = Field(alias='a')

class PublicEvidenceBatch(Strict):
    items: list[PublicEvidenceCandidate] = Field(min_length=1,max_length=20)
