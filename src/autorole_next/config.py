from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class SearchFilter(BaseModel):
	platforms: list[str] = Field(default_factory=lambda: ["linkedin", "indeed"])
	keywords: list[str] = Field(default_factory=list)
	location: str = ""
	seniority: list[str] = Field(default_factory=list)
	domain: list[str] = Field(default_factory=list)
	exclude: list[str] = Field(default_factory=list)


class ScoringWeights(BaseModel):
	technical_skills: float = 0.30
	experience_depth: float = 0.25
	seniority_alignment: float = 0.20
	domain_relevance: float = 0.15
	culture_fit: float = 0.10

	def normalised(self) -> dict[str, float]:
		data = self.model_dump()
		total = sum(data.values())
		return {k: v / total for k, v in data.items()}


class ScoringConfig(BaseModel):
	strategy: Literal["heuristic", "llm"] = "llm"
	llm_max_jd_chars: int = 6000
	llm_max_resume_chars: int = 8000


class TailoringConfig(BaseModel):
	max_attempts: int = 2
	degree_4_enabled: bool = True
	pass_threshold: float = 0.70
	degree_1_threshold: float = 0.70
	degree_2_threshold: float = 0.55
	degree_3_threshold: float = 0.40


class LLMConfig(BaseModel):
	provider: Literal["openai", "anthropic", "ollama"] = "ollama"
	model: str = "gpt-4o"
	ollama_model: str = "gpt-oss:120b-cloud"
	# ollama_model: str = "qwen2.5-coder:3b"
	ollama_base_url: str = "http://127.0.0.1:11434"
	temperature: float = 0.2
	max_retries: int = 3
	timeout_seconds: float = 300.0


class RendererConfig(BaseModel):
	engine: Literal["pandoc", "weasyprint"] = "weasyprint"
	pandoc_path: str = "pandoc"
	template: str = ""
	font_size_pt: float = 9.5
	line_height: float = 1.18
	page_margin_in: float = 0.4


class RetentionConfig(BaseModel):
	max_age_days: int = 365
	auto_prune: bool = False


class AppConfig(BaseSettings):
	base_dir: str = "~/.autorole"
	resume_dir: str = "~/.autorole/resumes"
	db_path: str = "~/.autorole/pipeline.db"
	master_resume: str = "~/.autorole/resumes/master.md"

	search: SearchFilter = Field(default_factory=SearchFilter)
	scoring: ScoringConfig = Field(default_factory=ScoringConfig)
	scoring_weights: ScoringWeights = Field(default_factory=ScoringWeights)
	tailoring: TailoringConfig = Field(default_factory=TailoringConfig)
	llm: LLMConfig = Field(default_factory=LLMConfig)
	renderer: RendererConfig = Field(default_factory=RendererConfig)
	retention: RetentionConfig = Field(default_factory=RetentionConfig)

	model_config = {
		"env_prefix": "AR_",
		"env_nested_delimiter": "__",
	}
