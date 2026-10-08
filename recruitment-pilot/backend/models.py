from typing import Literal
from pydantic import BaseModel, Field, ConfigDict

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid',allow_inf_nan=False)

class Evidence(Strict):
    source_id: str
    quote: str = Field(min_length=2, max_length=700)

class Signal(Strict):
    topic: Literal['salary','location','remote','availability','scope','motivation','other']
    summary: str
    evidence: list[Evidence] = Field(min_length=1)

class Fact(Strict):
    id: str
    field: Literal['skills','domains','responsibilities','achievements','titles','company_context','years_experience','management_years','education','languages']
    value: str
    numeric_value: float | None = None
    polarity: Literal['POSITIVE','NEGATIVE'] = 'POSITIVE'
    evidence: list[Evidence] = Field(min_length=1)

class Predicate(Strict):
    field: Literal['skills','domains','responsibilities','achievements','titles','company_context','years_experience','management_years','education','languages']
    operator: Literal['equals','any_of','at_least','gte','lte']
    values: list[str] = Field(default_factory=list)
    number: float | None = None
    weight: float = Field(default=1,gt=0,le=100)

class Rule(Strict):
    mode: Literal['ALL','ANY'] = 'ALL'
    predicates: list[Predicate] = Field(min_length=1,max_length=30)

class RetrievalPolicy(Strict):
    gate: Literal['ALL_MET','NO_CONFIRMED_FAILURE','REVIEW_ONLY'] = 'ALL_MET'
    threshold_enabled: bool = True
    threshold: float = Field(default=85,ge=0,le=100)
    threshold_operator: Literal['>','>='] = '>'
    weights: dict[Literal['MUST','NICE','OPTIONAL'],float] = Field(default_factory=lambda:{'MUST':3,'NICE':1,'OPTIONAL':.5})
    alpha: float = Field(default=.1,ge=0,le=1)
    top_k: int = Field(default=10,ge=1,le=10000)
    calibrated: bool = False

class Profile(Strict):
    summary: str
    title: str
    skills: list[str]
    domains: list[str]
    experience_summary: str
    years_experience: float | None
    signals: list[Signal]
    unknowns: list[str]
    contradictions: list[str]
    facts: list[Fact] = Field(default_factory=list)

class Criterion(Strict):
    id: str
    name: str = Field(min_length=1)
    type: Literal['MUST','NICE','OPTIONAL']
    enabled: bool = True
    description: str
    rule: Rule | None = None

class Strategy(Strict):
    exa_query: str = Field(default='',max_length=1500)
    id: str
    name: str
    description: str
    enabled: bool = True
    rule: Rule | None = None

class CriteriaDraft(Strict):
    criteria: list[Criterion] = Field(min_length=1)
    review_notes: list[str]

class PublicMust(Strict):
    criterion_id: str
    query: str = Field(min_length=3,max_length=1000)

class CriteriaConfig(CriteriaDraft):
    scouting_guidance: dict | None = None
    public_musts: list[PublicMust] = Field(default_factory=list)
    strategies: list[Strategy] = Field(default_factory=list)
    policy: RetrievalPolicy = Field(default_factory=RetrievalPolicy)

class StrategyPlan(Strict):
    public_musts: list[PublicMust] = Field(default_factory=list)
    strategies: list[Strategy] = Field(min_length=2, max_length=20)

class Assessment(Strict):
    criterion_id: str
    status: Literal['MET','PARTIAL','NOT_MET','UNKNOWN']
    evidence: list[Evidence]
    explanation: str

class Evaluation(Strict):
    assessments: list[Assessment]
    strategy_ids: list[str]
    strengths: list[str]
    gaps: list[str]

class Recruitability(Strict):
    conclusion: Literal['SIGNAL','CONDITIONAL','BARRIER','UNKNOWN']
    summary: str
    evidence: list[Evidence]
    attractions: list[Signal]
    barriers: list[Signal]
    hypotheses: list[str]
    questions: list[str] = Field(min_length=1)

class ComparisonItem(Strict):
    candidate_id: str
    rank: int = Field(ge=1)
    recommendation: str
    technical_evidence: list[Evidence]
    strengths: list[str]
    gaps: list[str]
    recruitability: Recruitability

class Comparison(Strict):
    items: list[ComparisonItem]
    overall_summary: str

class ConfigUpdate(Strict):
    version: int
    config: CriteriaConfig

class MatchRequest(Strict):
    config_version: int

class ReportRequest(Strict):
    selected_ids: list[str] = Field(min_length=1, max_length=20)
    limit: Literal[5,10,20] = 10
    threshold: float = Field(default=85, ge=0, le=100)

class FeedbackRequest(Strict):
    run_id: str
    candidate_id: str
    decision: Literal['GOOD','NOT_GOOD','UNCERTAIN']
    note: str = Field(max_length=5000)


class ExaQuery(Strict):
    strategy_id: str
    exa_query: str = Field(min_length=3,max_length=1500)
class ExaQueries(Strict):
    public_musts: list[PublicMust] = Field(default_factory=list)
    queries: list[ExaQuery] = Field(min_length=1,max_length=20)
