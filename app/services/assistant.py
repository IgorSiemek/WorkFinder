from __future__ import annotations

from dataclasses import dataclass

from app.models import CandidateProfile, Job


@dataclass(slots=True)
class ApplicationAnalysis:
    fit_summary: str
    keywords: list[str]
    strengths: list[str]
    gaps: list[str]
    cv_changes: list[str]
    interview_topics: list[str]


class ApplicationAssistant:
    """Deterministic analysis with the same interface a future LLM adapter can implement."""

    def analyze_job(self, job: Job, profile: CandidateProfile | None) -> ApplicationAnalysis:
        have = {
            item.casefold()
            for item in ((profile.skills + profile.technologies) if profile else [])
        }
        strengths = [skill for skill in job.skills if skill.casefold() in have]
        gaps = [skill for skill in job.skills if skill.casefold() not in have]
        categories = ", ".join(job.categories[:2]).lower()
        return ApplicationAnalysis(
            fit_summary=(
                f"This {job.seniority.lower()} role aligns with your interest in {categories}. "
                f"Its transparent rules-based match score is {job.match_score}/100."
            ),
            keywords=job.skills[:10] + job.categories[:3],
            strengths=strengths or ["Analytical thinking", "Interest in the role"],
            gaps=gaps,
            cv_changes=[
                f"Use a project or achievement that demonstrates {skill}."
                for skill in job.skills[:3]
            ],
            interview_topics=[
                f"Prepare a concise example of {topic.lower()} work."
                for topic in job.categories[:3]
            ],
        )
