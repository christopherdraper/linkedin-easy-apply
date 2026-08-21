"""Tests for LinkedIn Easy Apply new-markup modal detection.

LinkedIn migrated the Easy Apply modal to hashed-class markup: the container is
now `[data-testid='dialog']` / `[data-testid='dialog-content']` and the footer
Next/Review/Submit buttons expose only their visible text. These tests pin the
stable-hook unions so a future edit cannot silently drop either the new hooks
(breaking LinkedIn) or the legacy hooks (breaking external ATS / old LinkedIn).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobapply.easy_apply import (  # noqa: E402
    _NEXT_BTN_SEL,
    _REVIEW_BTN_SEL,
    _SUBMIT_BTN_SEL,
    _get_modal_text,
)
from jobapply.forms import (  # noqa: E402
    _MODAL_SEL,
    _get_form_container,
    _pick_radiogroup_option,
)


def _yes_no_profile():
    profile = MagicMock()
    profile.screening_answers = {}
    profile.authorized_to_work = True
    profile.requires_sponsorship = False
    return profile


def test_pick_radiogroup_authorized_to_work_yes():
    opts = [{"id": "a", "text": "Yes"}, {"id": "b", "text": "No"}]
    rid, txt = _pick_radiogroup_option(
        "are you legally authorized to work in the united states?", opts, _yes_no_profile()
    )
    assert (rid, txt) == ("a", "yes")


def test_pick_radiogroup_sponsorship_no():
    opts = [{"id": "y", "text": "Yes"}, {"id": "n", "text": "No"}]
    rid, txt = _pick_radiogroup_option(
        "will you now or in the future require sponsorship for an employment visa?",
        opts,
        _yes_no_profile(),
    )
    assert (rid, txt) == ("n", "no")


def test_modal_selector_covers_new_and_legacy_markup():
    # New hashed-class markup hooks
    assert "[data-testid='dialog-content']" in _MODAL_SEL
    assert "[data-testid='dialog']" in _MODAL_SEL
    # Legacy hooks must remain (external ATS + old LinkedIn depend on them)
    assert ".artdeco-modal" in _MODAL_SEL
    assert ".jobs-easy-apply-modal" in _MODAL_SEL
    assert "[role='dialog']" in _MODAL_SEL


def test_button_selectors_union_text_and_aria():
    # Text hooks are the only stable hook in the new markup
    assert "button:has-text('Submit application')" in _SUBMIT_BTN_SEL
    assert "button:has-text('Review')" in _REVIEW_BTN_SEL
    assert "button:has-text('Next')" in _NEXT_BTN_SEL
    # Legacy aria-label hooks retained
    assert "aria-label='Submit application'" in _SUBMIT_BTN_SEL
    assert "aria-label='Review your application'" in _REVIEW_BTN_SEL
    assert "aria-label='Continue to next step'" in _NEXT_BTN_SEL


def test_get_form_container_uses_union_and_returns_modal():
    page = MagicMock()
    sentinel = MagicMock()
    page.query_selector.return_value = sentinel
    assert _get_form_container(page) is sentinel
    page.query_selector.assert_called_once_with(_MODAL_SEL)


def test_get_form_container_falls_back_to_page():
    page = MagicMock()
    page.query_selector.return_value = None
    assert _get_form_container(page) is page


def test_get_modal_text_reads_new_markup_modal():
    page = MagicMock()
    modal = MagicMock()
    modal.inner_text.return_value = "Apply to Turing\nMobile phone number*"
    page.query_selector.return_value = modal
    assert "Mobile phone" in _get_modal_text(page)
    page.query_selector.assert_called_once_with(_MODAL_SEL)
