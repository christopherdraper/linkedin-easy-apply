"""Candidate-specific scoring guidance comes from the profile, never the
shared prompt: a rule about one applicant's SRE title was being applied to a
mechanical engineer's scores."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobapply.content import _SCORING_RULES, ai_score_job  # noqa: E402
from jobapply.forms import _determine_radio_answer  # noqa: E402

SCORE_JSON = '{"score": 0.8, "reasoning": "ok", "matched_skills": [], "deal_breakers": []}'


def _prompt(ai_client, job, profile):
    with ai_client(SCORE_JSON) as client:
        ai_score_job(job, profile)
    return client.return_value.messages.create.call_args.kwargs["messages"][0]["content"]


def test_shared_rules_name_no_applicant():
    assert "SRE" not in _SCORING_RULES
    assert "backend software" not in _SCORING_RULES.lower()


def test_profile_notes_reach_the_prompt(ai_client, job, profile):
    profile.scoring_notes = ["Open to backend Python roles despite an SRE title."]
    prompt = _prompt(ai_client, job, profile)
    assert "Notes about this candidate" in prompt
    assert "- Open to backend Python roles despite an SRE title." in prompt


def test_no_notes_adds_no_block(ai_client, job, profile):
    profile.scoring_notes = []
    assert "Notes about this candidate" not in _prompt(ai_client, job, profile)


def test_experience_question_is_answered_from_the_profile(profile, ai_client):
    """ "Worked in a DevOps environment?" used to be a fixed "yes"; now the AI,
    which reads the applicant's profile, decides."""
    profile.screening_answers = {}
    with ai_client("No") as client:
        answer = _determine_radio_answer("Have you worked in a DevOps environment?", profile)
    assert client.return_value.messages.create.called
    assert answer == "no"
