"""Tests for screening question matching and answer validation."""

from job_search_apply import (
    _determine_radio_answer,
    _match_screening_answer,
)


class TestMatchScreeningAnswer:
    def test_exact_substring_match(self):
        answers = {"salary": "150000", "devops": "9"}
        assert _match_screening_answer("desired salary", answers) == "150000"

    def test_exact_match_key_in_label(self):
        answers = {"kubernetes": "5"}
        assert _match_screening_answer("years of experience with kubernetes", answers) == "5"

    def test_fuzzy_word_match(self):
        answers = {"years of experience with kubernetes": "5"}
        label = "how many years of work experience do you have with kubernetes?"
        assert _match_screening_answer(label, answers) == "5"

    def test_no_match(self):
        answers = {"salary": "150000"}
        assert _match_screening_answer("favorite color", answers) is None

    def test_empty_label(self):
        answers = {"salary": "150000"}
        assert _match_screening_answer("", answers) is None

    def test_empty_answers(self):
        assert _match_screening_answer("salary", {}) is None

    def test_single_word_key_substring(self):
        # "go" is a substring of "golang" so it matches via exact substring
        answers = {"go": "2"}
        assert _match_screening_answer("how many years of golang", answers) == "2"
        assert _match_screening_answer("go experience", answers) == "2"
        # But "go" is NOT a substring of "years of experience"
        assert _match_screening_answer("years of experience", answers) is None

    def test_case_insensitive_matching(self):
        answers = {"Kubernetes": "5"}
        # Keys are matched with .lower()
        assert _match_screening_answer("kubernetes experience", answers) == "5"


class TestDetermineRadioAnswer:
    def test_authorized_to_work(self, profile):
        assert _determine_radio_answer("are you legally authorized to work?", profile) == "yes"

    def test_requires_sponsorship(self, profile):
        assert _determine_radio_answer("do you require visa sponsorship?", profile) == "no"

    def test_sponsorship_needed(self, profile):
        profile.requires_sponsorship = True
        assert _determine_radio_answer("will you need sponsorship?", profile) == "yes"

    def test_not_authorized(self, profile):
        profile.authorized_to_work = False
        assert _determine_radio_answer("are you eligible to work in the US?", profile) == "no"

    def test_willing_to_patterns(self, profile):
        assert _determine_radio_answer("are you willing to relocate?", profile) == "yes"
        assert _determine_radio_answer("are you comfortable with on-call?", profile) == "yes"
        assert _determine_radio_answer("do you agree to a background check?", profile) == "yes"

    def test_screening_answer_override(self, profile):
        profile.screening_answers["background check"] = "No"
        assert _determine_radio_answer("background check required?", profile) == "no"

    def test_unknown_defaults_to_yes(self, profile):
        # Unknown questions with no AI should default to "yes"
        assert _determine_radio_answer("some completely unknown question?", profile) == "yes"
