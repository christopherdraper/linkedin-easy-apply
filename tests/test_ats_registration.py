"""Generic ATS registration must never cost the applicant a working login.

It used to save the generated password over the stored one right after
clicking submit, before knowing whether registration worked, and even when
the form had no password field (iCIMS's email step, 2026-09-30). A failed or
misread registration then left the bot holding a password the site never
accepted, in place of one it had.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jobapply.accounts as accounts  # noqa: E402

DOMAIN = "jobs.example.com"


def _stored(data_dir):
    return json.loads((data_dir / "ats_accounts.json").read_text())


def _registration_page(still_on_login: bool):
    page = MagicMock()
    page.url = f"https://{DOMAIN}/login" if still_on_login else f"https://{DOMAIN}/apply/step1"
    page.evaluate.return_value = ""
    submit = MagicMock()
    page.query_selector.side_effect = lambda sel: submit if "submit" in sel else None
    return page


def _create(page, profile, password_field: bool, fields: int = 3):
    with (
        patch.object(accounts, "_password_field_visible", return_value=password_field),
        patch.object(accounts, "_fill_registration_form", return_value=fields),
        patch.object(accounts, "_generate_ats_password", return_value="NEW-pass-1!"),
        patch.object(accounts, "_handle_registration_verification", return_value=True),
        patch.object(accounts, "_safe_click"),
        patch.object(accounts, "_dump_form_debug"),
    ):
        return accounts._attempt_account_creation(page, profile)


class TestRegistrationKeepsTheConfirmedPassword:
    def test_unconfirmed_registration_does_not_overwrite_a_working_password(
        self, data_dir, profile
    ):
        accounts._save_ats_account(DOMAIN, profile.email, "OLD-working-1!")
        assert (
            _create(_registration_page(still_on_login=True), profile, password_field=True) is False
        )
        entry = _stored(data_dir)[DOMAIN]
        assert entry["password"] == "OLD-working-1!"
        assert entry["pending_password"] == "NEW-pass-1!"

    def test_confirmed_registration_saves_the_password_and_clears_pending(self, data_dir, profile):
        assert (
            _create(_registration_page(still_on_login=False), profile, password_field=True) is True
        )
        assert _stored(data_dir)[DOMAIN] == {"email": profile.email, "password": "NEW-pass-1!"}

    def test_form_without_a_password_field_saves_nothing(self, data_dir, profile):
        # iCIMS's email-only sign-in step
        assert (
            _create(_registration_page(still_on_login=False), profile, password_field=False) is True
        )
        assert not (data_dir / "ats_accounts.json").exists()


class TestLoginFallsBackToThePendingPassword:
    def test_pending_password_is_tried_and_promoted_when_the_stored_one_fails(
        self, data_dir, profile
    ):
        accounts._save_ats_account(DOMAIN, profile.email, "OLD-stale-1!")
        accounts._save_pending_ats_password(DOMAIN, profile.email, "NEW-real-1!")
        with patch.object(accounts, "_submit_ats_login", side_effect=["rejected", "ok"]) as m:
            assert accounts._attempt_ats_login(MagicMock(), DOMAIN) is True
        assert [c.args[2] for c in m.call_args_list] == ["OLD-stale-1!", "NEW-real-1!"]
        assert _stored(data_dir)[DOMAIN] == {"email": profile.email, "password": "NEW-real-1!"}

    def test_both_rejected_marks_the_domain_and_keeps_both(self, data_dir, profile):
        accounts._save_ats_account(DOMAIN, profile.email, "A-1!")
        accounts._save_pending_ats_password(DOMAIN, profile.email, "B-1!")
        with patch.object(accounts, "_submit_ats_login", return_value="rejected") as m:
            assert accounts._attempt_ats_login(MagicMock(), DOMAIN) is False
        assert m.call_count == 2
        assert DOMAIN in accounts._REJECTED_LOGIN_DOMAINS
        entry = _stored(data_dir)[DOMAIN]
        assert entry["password"] == "A-1!" and entry["pending_password"] == "B-1!"

    def test_only_a_pending_password_is_still_used(self, data_dir, profile):
        accounts._save_pending_ats_password(DOMAIN, profile.email, "ONLY-pending-1!")
        with patch.object(accounts, "_submit_ats_login", return_value="ok") as m:
            assert accounts._attempt_ats_login(MagicMock(), DOMAIN) is True
        assert m.call_args.args[2] == "ONLY-pending-1!"

    def test_an_inconclusive_attempt_does_not_try_the_next_password(self, data_dir, profile):
        accounts._save_ats_account(DOMAIN, profile.email, "A-1!")
        accounts._save_pending_ats_password(DOMAIN, profile.email, "B-1!")
        with patch.object(accounts, "_submit_ats_login", return_value="unknown") as m:
            assert accounts._attempt_ats_login(MagicMock(), DOMAIN) is False
        assert m.call_count == 1
        assert DOMAIN not in accounts._REJECTED_LOGIN_DOMAINS


def test_show_credentials_lists_the_pending_password(data_dir, profile, capsys):
    from jobapply.cli import _show_ats_credentials

    accounts._save_ats_account(DOMAIN, profile.email, "A-1!")
    accounts._save_pending_ats_password(DOMAIN, profile.email, "B-1!")
    _show_ats_credentials()
    out = capsys.readouterr().out
    assert "A-1!" in out and "B-1!" in out


class TestWorkdayWrongCountry:
    """Curtiss-Wright's Bangalore req was listed on LinkedIn as "Indiana, United
    States"; the bot created a Workday account for it (2026-09-29)."""

    CW = (
        "https://curtisswright.wd1.myworkdayjobs.com/en-US/CW_External_Career_Site/job/"
        "India-Bangalore-(ST-660)/Supplier-Quality-Engineer_JR12887-2/apply/autofillWithResume"
    )

    def _status(self, descriptor, profile=None):
        from ats_handlers import workday

        detail = {"jobPostingInfo": {"country": {"descriptor": descriptor}}} if descriptor else {}
        with patch("jobapply.search._workday_job_detail", return_value=detail) as m:
            status = workday._wrong_country_status(self.CW, profile)
        return status, m

    def test_foreign_posting_is_skipped(self):
        status, m = self._status("India")
        assert status == "skipped: job is in India"
        assert m.call_args.args == (
            "curtisswright",
            "wd1",
            "CW_External_Career_Site",
            "/job/India-Bangalore-(ST-660)/Supplier-Quality-Engineer_JR12887-2",
        )

    def test_us_posting_proceeds(self):
        assert self._status("United States of America")[0] is None

    def test_unknown_country_proceeds(self):
        assert self._status(None)[0] is None

    def test_profile_country_is_the_home_country(self, profile):
        profile.country = "India"
        assert self._status("India", profile)[0] is None
        assert self._status("United States of America", profile)[0] is not None


class TestActivationLink:
    """Curtiss-Wright's Workday emails a "Verify your candidate account" link,
    not a code; the new account was refused at sign-in until it was opened."""

    DOMAIN = "curtisswright.wd1.myworkdayjobs.com"
    BODY = (
        "Click this link to confirm your email address and complete setup for your candidate "
        "account https://curtisswright.wd1.myworkdayjobs.com/CW_External_Career_Site/activate/"
        "7dcm9tvn/?redirect=%2Fen-US%2Fjob&amp;source=LinkedIn The link will expire after 24 hours."
    )

    def test_link_for_the_site_is_extracted_and_unescaped(self):
        link = accounts._extract_activation_link(self.BODY, self.DOMAIN)
        assert link == (
            "https://curtisswright.wd1.myworkdayjobs.com/CW_External_Career_Site/activate/"
            "7dcm9tvn/?redirect=%2Fen-US%2Fjob&source=LinkedIn"
        )

    def test_another_sites_link_is_ignored(self):
        assert accounts._extract_activation_link(self.BODY, "graco.wd501.myworkdayjobs.com") is None

    def test_verification_without_a_code_opens_the_link(self, profile):
        profile.gmail_app_password = "app-pw"
        page = MagicMock()
        page.url = f"https://{self.DOMAIN}/en-US/CW_External_Career_Site/login"
        page.evaluate.return_value = "we sent a verification email. verify your account."
        with (
            patch.object(accounts, "_fetch_verification_code_from_gmail", return_value=None),
            patch.object(
                accounts, "_fetch_activation_link_from_gmail", return_value="https://x/activate/1"
            ) as m_link,
        ):
            assert accounts._handle_registration_verification(page, profile) is True
        assert m_link.call_args.args[2] == self.DOMAIN
        page.goto.assert_called_once()
        assert page.goto.call_args.args[0] == "https://x/activate/1"


class TestICIMSSharedSignIn:
    URL = "https://login.icims.com/u/login/identifier?state=abc"

    def _page(self):
        page = MagicMock()
        page.url = self.URL
        fields = {"input[name='username']": MagicMock(), "input[name='password']": MagicMock()}
        page.query_selector.side_effect = lambda sel: fields.get(sel, MagicMock())
        return page, fields

    def test_without_a_stored_password_it_does_not_try(self, data_dir):
        from ats_handlers.icims import ICIMSHandler

        page, fields = self._page()
        assert ICIMSHandler().resolve_login_wall(page, {}) is False
        fields["input[name='password']"].fill.assert_not_called()

    def test_with_a_stored_password_it_signs_in_in_two_steps(self, data_dir):
        from ats_handlers.icims import ICIMSHandler

        accounts._save_ats_account("login.icims.com", "e@x.com", "icims-pw-1!")
        page, fields = self._page()

        def leave(*_a, **_k):
            if fields["input[name='password']"].fill.called:
                page.url = "https://jobs-x.icims.com/jobs/1/apply"

        with patch("jobapply.forms._safe_click", side_effect=leave):
            assert ICIMSHandler().resolve_login_wall(page, {}) is True
        fields["input[name='username']"].fill.assert_called_once_with("e@x.com")
        fields["input[name='password']"].fill.assert_called_once_with("icims-pw-1!")

    def test_other_pages_are_left_to_the_generic_logic(self, data_dir):
        from ats_handlers.icims import ICIMSHandler

        page, _ = self._page()
        page.url = "https://jobs-x.icims.com/jobs/1/login"
        assert ICIMSHandler().resolve_login_wall(page, {}) is False
