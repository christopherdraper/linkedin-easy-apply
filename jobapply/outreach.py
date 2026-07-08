"""Hiring manager outreach: extract the job poster from a LinkedIn job page,
draft a short personalized message with Claude, and send it."""

import logging
from typing import Dict, Optional

from jobapply import stats
from jobapply.ai import _AI_AVAILABLE, _get_ai_client
from jobapply.browser import _playwright_context, _stealth_playwright
from jobapply.profile import ApplicantProfile
from jobapply.safety import _sanitize_description

log = logging.getLogger(__name__)


def _extract_hiring_manager(page) -> Optional[Dict[str, str]]:
    """
    Extract the job poster / hiring manager info from a LinkedIn job detail page.

    Returns a dict with keys: name, headline, profile_url, compose_url
    or None if no hiring team section is found.
    """
    return page.evaluate(
        """() => {
        const body = document.body ? document.body.innerText : '';
        if (!body.includes('Meet the hiring team') && !body.includes('Job poster'))
            return null;

        const msgLink = document.querySelector(
            'a[href*="messaging/compose"][href*="JOB_DETAILS"]'
        );
        if (!msgLink) return null;

        let card = msgLink;
        for (let i = 0; i < 10 && card; i++) {
            const text = card.innerText || '';
            if (text.includes('Job poster') || text.includes('Meet the hiring team'))
                break;
            card = card.parentElement;
        }
        if (!card) return null;

        const profileLink = card.querySelector('a[href*="/in/"]');
        const cardText = card.innerText;
        const lines = cardText.split('\\n')
            .map(function(l) { return l.trim(); })
            .filter(function(l) { return l.length > 0; });

        var name = '';
        var headline = '';
        for (var i = 0; i < lines.length; i++) {
            var l = lines[i];
            if (l.charAt(0) === '\\u2022' || l === 'Job poster'
                || l === 'Message' || l === 'Connect'
                || l.includes('Meet the hiring team')) continue;
            if (!name) { name = l; continue; }
            if (!headline && l !== name) { headline = l; break; }
        }

        return {
            name: name,
            headline: headline,
            profile_url: profileLink ? profileLink.href.split('?')[0] : null,
            compose_url: msgLink.href,
        };
    }"""
    )


def _ai_draft_hiring_message(
    job: Dict, profile: "ApplicantProfile", poster_name: str
) -> Optional[str]:
    """Draft a short personalized message to the hiring manager using Claude."""
    if not _AI_AVAILABLE:
        return None
    try:
        client = _get_ai_client()
        description = _sanitize_description(job.get("description", ""))[:2000]
        prompt = f"""Write a short LinkedIn message (3 sentences) from a job applicant to the hiring manager / job poster.

Applicant: {profile.full_name}, {profile.current_title or "engineer"} with {profile.years_experience or "several"} years of experience.
Key skills: {", ".join(profile.specializations[:4]) if profile.specializations else "infrastructure engineering"}

Job: {job.get("title")} at {job.get("company")}
Poster name: {poster_name}
Job description snippet: {description[:800]}

Guidelines:
- Start with "Hey <first name>," — that's the only greeting
- Sentence 1: Say you applied for the role (use the exact title)
- Sentence 2: ONE specific detail — a tool overlap, a similar problem you solved, or a concrete fact from your background that connects to the job. Not a summary of your whole career.
- Sentence 3: A brief, natural closer — a question about the team/stack, or saying you'd love to chat. Keep it casual.
- Do NOT start sentences with "I've spent the last N years". Do NOT list multiple skills. Pick ONE thing.
- 3 sentences. Under 250 characters after the greeting.
- Sound like a person dashing off a quick note, not crafting a pitch.
- NEVER use: "excited", "passionate", "eager", "thrilled", "aligns", "resonates", "looking forward", "opportunity", "I believe", "sounds like", "exactly what", "wheelhouse", "culture", "I'd love to bring"
- NEVER use em dashes (—) or double dashes (--). Use commas, periods, or rewrite the sentence instead.
- NO subject line, NO sign-off"""

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        stats.add_ai_tokens(response.usage)
        msg = response.content[0].text.strip()
        # Strip em dashes — they scream "AI-written"
        msg = msg.replace(" — ", ", ").replace(" -- ", ", ").replace("—", ", ").replace("--", ", ")
        # Strip any AI preamble, self-corrections, or extra lines
        lines = msg.split("\n")
        clean = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("*") or stripped.startswith("---"):
                break  # AI started self-correcting
            if stripped:
                clean.append(stripped)
        msg = " ".join(clean) if clean else msg
        # Strip stale greeting prefixes
        for prefix in ("Subject:", "Hi,", "Hello,"):
            if msg.startswith(prefix):
                msg = msg[len(prefix) :].strip()
        return msg
    except Exception as e:
        log.warning(f"   AI hiring message draft failed: {e}")
        return None


def _send_hiring_manager_message(  # noqa: C901
    page,
    compose_url: str,
    message: str,
    poster_name: str,
    job_title: str = "",
    company: str = "",
) -> str:
    """
    Navigate to the LinkedIn messaging compose URL and send a message.
    Returns 'sent', 'failed', or an error description.
    """
    try:
        page.goto(compose_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)

        # Find the message textbox
        textbox = page.query_selector('div.msg-form__contenteditable[role="textbox"]')
        if not textbox:
            # Fallback: any contenteditable with message-related label
            textbox = page.query_selector('[role="textbox"][aria-label*="message" i]')
        if not textbox:
            log.warning("   Could not find message compose textbox")
            return "failed: no textbox found"

        # Check for subject line (InMail) and fill if present
        subject_input = page.query_selector('input.msg-form__subject, input[name="subject"]')
        if subject_input:
            subject_input.click()
            subj = f"Re: {job_title}" if job_title else "Re: your job posting"
            subject_input.fill(subj)
            page.wait_for_timeout(300)

        # Type the message
        textbox.click()
        page.wait_for_timeout(300)
        textbox.type(message, delay=20)
        page.wait_for_timeout(500)

        # Find and click the send button
        send_btn = page.query_selector("button.msg-form__send-btn")
        if not send_btn:
            send_btn = page.query_selector('button[type="submit"]:has(svg)')
        if not send_btn:
            log.warning("   Could not find send button")
            return "failed: no send button"

        send_btn.click()
        page.wait_for_timeout(2000)

        log.info(f"   ✉️  Message sent to {poster_name}")
        return "sent"
    except Exception as exc:
        log.warning(f"   Message send failed: {exc}")
        return f"failed: {exc}"


def _message_hiring_manager_after_apply(
    job: Dict,
    profile: "ApplicantProfile",
    proxy: Optional[str] = None,
) -> tuple:
    """
    After a successful application, check the job page for a hiring manager
    and send a short personalized message.
    Returns (status, message_text, poster_name) or (None, None, None) if skipped.
    """
    if not profile.message_hiring_manager:
        return None, None, None

    try:
        import playwright  # noqa: F401
    except ImportError:
        return None, None, None

    with _stealth_playwright() as p:
        browser, context, page, owns_browser = _playwright_context(p, proxy)
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)

            poster = _extract_hiring_manager(page)
            if not poster or not poster.get("compose_url"):
                log.info("   ℹ️  No hiring manager found for %s", job.get("title"))
                return None, None, None

            log.info(f"   👤 Found hiring manager: {poster['name']} — {poster.get('headline', '')}")

            message = _ai_draft_hiring_message(job, profile, poster["name"])
            if not message:
                log.warning("   Could not draft message — skipping")
                return None, None, None

            log.info(f"   💬 Sending: {message[:80]}...")
            status = _send_hiring_manager_message(
                page,
                poster["compose_url"],
                message,
                poster["name"],
                job_title=job.get("title", ""),
                company=job.get("company", ""),
            )
            return status, message, poster["name"]
        finally:
            page.close()
            if owns_browser:
                browser.close()
