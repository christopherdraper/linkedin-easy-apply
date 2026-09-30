"""Workday-owned vision plumbing: a bounded screenshot+act loop used to finish a
single stalled Workday page (safety net) when the deterministic pipeline cannot
advance. Ported from the validated prototype.

Hard rules enforced here: never click Submit, never enter login/account
credentials, decline demographics, never invent SSNs/sensitive identifiers (a
required-unknown date may be estimated for human review). See the system prompt.
"""

import base64
import datetime
import logging

log = logging.getLogger("job_apply")

MODEL = "claude-sonnet-5-5"
W, H = 1360, 900

KEYMAP = {
    "Return": "Enter",
    "Enter": "Enter",
    "Tab": "Tab",
    "Escape": "Escape",
    "Down": "ArrowDown",
    "Up": "ArrowUp",
    "space": " ",
}

ACT_TOOL = {
    "name": "act",
    "description": "Perform one browser action.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "set_field",
                    "select_option",
                    "left_click",
                    "double_click",
                    "type",
                    "key",
                    "scroll",
                    "wait",
                    "screenshot",
                    "done",
                ],
                "description": "set_field = click at coordinate, clear, and type `text`. "
                "select_option = open the dropdown at `coordinate` and pick the option whose "
                "text matches `text` (use this for ALL dropdowns -- it is reliable).",
            },
            "coordinate": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "[x,y] pixel for click/scroll/set_field",
            },
            "text": {
                "type": "string",
                "description": "text to type/set, key name, or a done signal",
            },
            "scroll_direction": {"type": "string", "enum": ["up", "down"]},
            "reason": {"type": "string", "description": "one short phrase why"},
        },
        "required": ["action"],
    },
}


def _shot(page) -> str:
    return base64.standard_b64encode(page.screenshot()).decode()


def _heading(page) -> str:
    try:
        return " ".join(
            page.evaluate(
                "[...document.querySelectorAll('h2,h3')].map(e=>e.innerText.trim())"
                ".filter(Boolean).slice(0,3)"
            )
        )
    except Exception:  # noqa: BLE001
        return ""


def _clickables(page) -> list:
    try:
        return (
            page.evaluate(r"""() => {
          const out=[]; const seen=new Set();
          const sel='input,textarea,button,[role="option"],[role="button"],'
            +'[role="checkbox"],[role="radio"],a[role],'
            +'[data-automation-id="pageFooterNextButton"]';
          document.querySelectorAll(sel).forEach(e=>{
            const r=e.getBoundingClientRect();
            if(r.width<2||r.height<2||r.bottom<0||r.top>window.innerHeight) return;
            let label=(e.getAttribute('aria-label')||e.placeholder||e.value||e.innerText||'')
              .trim().replace(/\s+/g,' ').slice(0,34);
            if(!label && e.tagName==='INPUT') label='('+(e.type||'text')+' input)';
            const k=label+Math.round(r.x)+Math.round(r.y);
            if(seen.has(k)||!label) return; seen.add(k);
            out.push({t:label, x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2)});
          });
          return out.slice(0,45);
        }""")
            or []
        )
    except Exception:  # noqa: BLE001
        return []


def _prune_history(messages, keep_full=2, keep_img=2) -> None:
    """Bound context cost: old screens collapse to their 'Page: <heading>' line
    and drop their screenshot; only the last keep_full/keep_img keep full detail.
    """
    screens = [
        i
        for i, m in enumerate(messages)
        if isinstance(m.get("content"), list)
        and (
            i == 0
            or any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
        )
    ]
    rank = {mi: r for r, mi in enumerate(reversed(screens))}

    def compact(blocks, drop_txt, drop_img):
        for cb in blocks:
            if not isinstance(cb, dict):
                continue
            if cb.get("type") == "image" and drop_img:
                cb.clear()
                cb.update({"type": "text", "text": "[screenshot omitted]"})
            elif (
                cb.get("type") == "text" and drop_txt and "Clickable elements" in cb.get("text", "")
            ):
                cb["text"] = cb["text"].split("\n", 1)[0] + "\n[elements omitted]"

    for mi, m in enumerate(messages):
        r = rank.get(mi)
        if r is None:
            continue
        drop_txt, drop_img = r >= keep_full, r >= keep_img
        if not (drop_txt or drop_img):
            continue
        for b in m["content"]:
            if isinstance(b, dict) and b.get("type") in ("image", "text"):
                compact([b], drop_txt, drop_img)
            elif (
                isinstance(b, dict)
                and b.get("type") == "tool_result"
                and isinstance(b.get("content"), list)
            ):
                compact(b["content"], drop_txt, drop_img)


def _autofill_resume(page, resume_path) -> bool:
    """Upload ``resume_path`` to the first EMPTY file-upload widget on the page,
    searching every frame.

    Workday nests its required resume/CV widget inside an iframe, and the vision
    act loop has no file-upload action -- left alone it thrashes on the "field is
    required" error until its budget drains. Called each loop iteration so the
    widget is satisfied the moment the page presents it. Idempotent: a widget
    that already shows an uploaded file (filename text, upload item, or a
    non-empty input) is skipped, so re-renders never double-upload (which
    corrupts Workday's upload state).
    """
    if not resume_path:
        return False
    for fr in page.frames:
        try:
            needs = fr.evaluate(r"""() => {
              const inp = document.querySelector("input[type='file']");
              if (!inp) return false;
              const body = document.body ? (document.body.innerText || '') : '';
              const done = /successfully uploaded/i.test(body)
                || !!document.querySelector("[data-automation-id='file-upload-item']")
                || (inp.files && inp.files.length > 0);
              return !done;
            }""")
        except Exception:  # noqa: BLE001
            continue
        if not needs:
            continue
        try:
            fin = fr.query_selector("input[type='file']")
            if fin:
                fin.set_input_files(resume_path)
                page.wait_for_timeout(2500)
                log.info("   Workday vision: uploaded resume to required file widget")
                return True
        except Exception as e:  # noqa: BLE001
            log.debug("resume autofill err: %s", str(e)[:80])
    return False


def _select_dropdown_option(page, coord, want) -> bool:
    """Open the Workday dropdown at ``coord`` and click the option ELEMENT whose
    text matches ``want`` (exact match preferred, else substring), searching every
    frame. Clicking the real option element -- rather than a vision-estimated
    coordinate -- is what makes selection reliable: coordinate clicks on Workday's
    overlay options frequently miss and send the model into a reopen loop. Falls
    back to type-to-filter + Down + Enter when no option element matches.
    """
    if coord:
        page.mouse.click(coord[0], coord[1])
        page.wait_for_timeout(500)
    want_l = (want or "").strip().lower()
    if not want_l:
        return False
    exact = partial = None
    for fr in page.frames:
        try:
            opts = fr.query_selector_all("[role='option']")
        except Exception:  # noqa: BLE001
            continue
        for o in opts:
            try:
                t = (o.inner_text() or "").strip().lower()
            except Exception:  # noqa: BLE001
                continue
            if not t:
                continue
            if t == want_l and exact is None:
                exact = o
            elif partial is None and want_l in t:
                partial = o
        if exact is not None:
            break
    target = exact or partial
    if target is not None:
        try:
            target.scroll_into_view_if_needed(timeout=1000)
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            target.click()
            return True
        except Exception:  # noqa: BLE001
            pass
    try:
        page.keyboard.type(want, delay=25)
        page.wait_for_timeout(400)
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
        return True
    except Exception:  # noqa: BLE001
        return False


def _do_action(page, inp) -> None:
    a = inp.get("action")
    coord = inp.get("coordinate")
    if a == "set_field" and coord:
        page.mouse.click(coord[0], coord[1])
        page.wait_for_timeout(150)
        page.keyboard.press("Control+a")
        page.wait_for_timeout(60)
        page.keyboard.press("Delete")
        page.wait_for_timeout(60)
        page.keyboard.type(inp.get("text", ""), delay=25)
    elif a == "select_option":
        _select_dropdown_option(page, coord, inp.get("text", ""))
    elif a == "left_click" and coord:
        page.mouse.click(coord[0], coord[1])
    elif a == "double_click" and coord:
        page.mouse.dblclick(coord[0], coord[1])
    elif a == "type":
        page.keyboard.type(inp.get("text", ""), delay=25)
    elif a == "key":
        for pt in inp.get("text", "").split("+"):
            page.keyboard.press(KEYMAP.get(pt.strip(), pt.strip()))
    elif a == "scroll":
        x, y = coord or [W // 2, H // 2]
        page.mouse.move(x, y)
        page.mouse.wheel(0, 360 if inp.get("scroll_direction", "down") == "down" else -360)
    elif a == "wait":
        page.wait_for_timeout(1200)
    page.wait_for_timeout(700)


TODAY = datetime.date.today().strftime("%m/%d/%Y")


def _screen(page):
    hd = _heading(page)
    els = _clickables(page)
    text = f"Page: {hd}\nClickable elements (text @ x,y):\n" + "\n".join(
        f"- {e['t']} @ {e['x']},{e['y']}" for e in els
    )
    return [
        {"type": "text", "text": text},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": _shot(page)},
        },
    ]


def _run_act_loop(
    page, client, system_text, user_text, max_actions, forbid_submit=True, resume_path=None
):
    """Shared bounded act loop. Returns True if the model signalled 'done'.

    ``resume_path`` (when set) is uploaded to any empty Workday file-upload
    widget each iteration, so the model never has to -- it has no file action.
    """
    from jobapply import stats

    sys_cached = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
    _autofill_resume(page, resume_path)
    messages = [{"role": "user", "content": [{"type": "text", "text": user_text}] + _screen(page)}]
    recent_sigs = []
    text_only_turns = 0
    for _i in range(max_actions):
        # Sonnet 5.5 rejects forced tool use ("any"/"tool"), so the tool is
        # offered and a text-only reply is nudged back to it below.
        r = client.messages.create(
            model=MODEL,
            max_tokens=700,
            system=sys_cached,
            tools=[ACT_TOOL],
            tool_choice={"type": "auto"},
            messages=messages,
        )
        try:
            stats.add_ai_tokens(r.usage, r.model)
        except Exception:  # noqa: BLE001, S110
            pass
        messages.append({"role": "assistant", "content": r.content})
        tus = [b for b in r.content if getattr(b, "type", None) == "tool_use"]
        if not tus:
            text_only_turns += 1
            if text_only_turns >= 2:
                return False
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Respond by calling the act tool with your next action.",
                        }
                    ],
                }
            )
            continue
        text_only_turns = 0
        tu = tus[0]
        inp = tu.input
        act = inp.get("action")
        log.info(
            "   Workday vision [%d] :: %s %s %s | %s",
            _i,
            act,
            inp.get("coordinate") or "",
            (inp.get("text") or "")[:30],
            (inp.get("reason") or "")[:40],
        )
        if (
            act == "done"
            or "REACHED_REVIEW" in (inp.get("text") or "")
            or "LOGIN_WALL" in (inp.get("text") or "")
        ):
            return True
        # Anti-loop guard: a stuck model repeats the same action -- either three in
        # a row, or in a short cycle (open->select->open->select on a dropdown that
        # will not accept a click). Count identical signatures in a sliding window
        # (not just the immediate predecessor) so cyclic loops are caught too;
        # refuse the offending action and nudge toward a different strategy.
        sig = (
            act,
            tuple(inp.get("coordinate")) if inp.get("coordinate") else None,
            (inp.get("text") or "")[:20],
        )
        if recent_sigs.count(sig) >= 2:
            log.info("   Workday vision: loop detected -- nudging off repeated action")
            nudge = {
                "type": "text",
                "text": (
                    "You have repeated this action several times with no effect -- it is "
                    "stuck. Do NOT repeat it. If you were setting a DROPDOWN, use the "
                    "keyboard instead: click the field ONCE, type the value, then issue "
                    "key 'Down' and key 'Enter' -- never click the option row. Otherwise "
                    "the field may already be set or is optional: click 'Save and "
                    "Continue' to advance, or act on a DIFFERENT field."
                ),
            }
            results = [
                {"type": "tool_result", "tool_use_id": tu.id, "content": [nudge] + _screen(page)}
            ]
            for extra in tus[1:]:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": extra.id,
                        "content": "Ignored -- one action per turn.",
                    }
                )
            messages.append({"role": "user", "content": results})
            _prune_history(messages)
            recent_sigs = []
            continue
        recent_sigs.append(sig)
        if len(recent_sigs) > 6:
            recent_sigs.pop(0)
        # Submit guard: never let the model click a submit/save-and-continue control.
        if (
            forbid_submit
            and act in ("left_click", "double_click")
            and _coord_is_submit(page, inp.get("coordinate"))
        ):
            log.info("   Workday vision: refused a Submit-region click")
        else:
            try:
                _do_action(page, inp)
            except Exception as e:  # noqa: BLE001
                log.debug("vision act err: %s", str(e)[:80])
        _autofill_resume(page, resume_path)
        cont = _screen(page)
        results = [{"type": "tool_result", "tool_use_id": tu.id, "content": cont}]
        for extra in tus[1:]:
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": extra.id,
                    "content": "Ignored -- one action per turn.",
                }
            )
        messages.append({"role": "user", "content": results})
        _prune_history(messages)
    return False


def _coord_is_submit(page, coord) -> bool:
    """True if the element at coord is a Submit button (never click it).

    Fails CLOSED: if we cannot determine what's under the coordinate (e.g. the
    page.evaluate call raises), we refuse the click rather than allow it.
    """
    if not coord:
        return False
    try:
        return bool(
            page.evaluate(
                "([x,y]) => { const el = document.elementFromPoint(x,y); "
                "return !!el && /\\bsubmit\\b/i.test((el.closest('button,[role=button]')?.innerText)||''); }",
                coord,
            )
        )
    except Exception:  # noqa: BLE001
        return True


_PAGE_SYSTEM = (
    """You are completing a Workday job application, advancing PAGE BY PAGE to the
final Review page. You get a screenshot and a list of clickable elements with coordinates.
Issue ONE action via `act`.

On each page, fill every required (*) field accurately using the applicant data, then click
"Save and Continue" (bottom-right) to advance to the next page, and repeat -- keep going
through every page until you reach Review. Use set_field for text.

DROPDOWNS -- read carefully, these cause the most trouble:
- To set ANY dropdown (Country, State, Degree, Field of Study, gender, ethnicity, etc.) use
  the `select_option` action: coordinate = the dropdown field, text = the option you want.
  It opens the field and clicks the matching option reliably. PREFER select_option over
  clicking an option row yourself -- coordinate clicks on the option overlay often miss and
  send you into a reopen loop.
- If select_option does not stick, the field may have a SEARCH BOX: click it, TYPE the value,
  then key 'Down' and key 'Enter'. NEVER use the scroll action on a dropdown -- its options
  are a fixed overlay that page-scrolling does not move, so scrolling loops forever. After two
  tries, move on -- a human reviews at the end.
- Country/State are given in the applicant data above (the applicant is in the United States).
- Hierarchical "How Did You Hear About Us" style dropdowns: drill into a '>' category, then
  TYPE to filter and Down + Enter to pick a concrete leaf.
FILE UPLOADS (resume/CV): these are handled for you automatically -- the resume is attached
to any required upload widget before you see the page. Do NOT try to click an upload button,
open a file dialog, or "skip" the resume. If an "Upload a file is required" error persists for
a moment, just wait one turn and it clears; then continue with Save and Continue.
GENERAL: never issue more than TWO scroll actions in a row -- if scrolling is not clearly
helping, do something else (type, key, or move to another field).
Do not re-fix acceptable autofill. If a value truly will not select after ~3 tries, move on
-- a human reviews everything at the Review page.

Voluntary Disclosures / Self-Identify: set the demographic fields (gender, ethnicity/race,
Hispanic/Latino, veteran, disability) from the applicant's "Self-identification" data shown in
the applicant info, using select_option. If a field has NO matching value in that data, choose
the decline option ("Prefer not to say" / "I don't wish to answer"). NEVER guess a value that
is neither provided nor a decline option, and NEVER infer gender/race/ethnicity from the name.
Tick any required consent/terms checkbox. Prefer real dates in the applicant data or resume;
never invent an SSN or other sensitive identifier (leave those blank). Only if a REQUIRED date
field is unknown and blocks progress, enter a plausible estimate -- every field is
human-reviewed and corrected at the Review page before anything is submitted. Add at most 2-3
skills.

HARD RULES: NEVER click "Submit". NEVER create an account or type a password. If you reach
a page whose heading is "Review", respond 'done' with text 'REACHED_REVIEW'. If you land on
a Sign In / Create Account page, respond 'done' with text 'LOGIN_WALL'. Today: """
    + TODAY
)


def vision_complete_page(page, profile, *, client=None, max_actions=120) -> bool:
    """Drive the Workday form forward, page by page, to the Review page (never
    Submit) via one continuous vision loop with memory. Generous action budget so a
    heavy page (work history + education + skills) finishes in one pass rather than
    losing context across short passes."""
    if client is None:
        from jobapply.ai import _get_ai_client

        client = _get_ai_client()
    try:
        from job_search_apply import _profile_summary

        summary = _profile_summary(profile)
    except Exception:  # noqa: BLE001
        summary = ""
    resume_path = ""
    try:
        from pathlib import Path

        rp = getattr(profile, "resume_path", "") or ""
        if rp:
            resolved = Path(rp).expanduser()
            if resolved.exists():
                resume_path = str(resolved)
    except Exception:  # noqa: BLE001
        resume_path = ""
    eeo = ""
    try:
        si = getattr(profile, "self_identification", {}) or {}
        if si:
            eeo = "\nSelf-identification (use for EEO / voluntary self-ID fields):\n" + "\n".join(
                f"- {k.replace('_', ' ')}: {v}" for k, v in si.items()
            )
    except Exception:  # noqa: BLE001
        eeo = ""
    screening = ""
    try:
        sa = getattr(profile, "screening_answers", {}) or {}
        pairs = [(k, v) for k, v in sa.items() if str(v).strip()]
        if pairs:
            screening = (
                "\nScreening answers (authoritative -- when a form question matches one of "
                "these, use the stated answer verbatim; do not infer your own):\n"
                + "\n".join(f"- {k}: {v}" for k, v in pairs)
            )
    except Exception:  # noqa: BLE001
        screening = ""
    user = (
        "Complete this Workday application page by page, clicking Save and Continue to "
        "advance, until you reach the Review page. Do NOT submit. "
        f"Applicant:\n{summary}{screening}{eeo}\nCurrent screen:"
    )
    log.info("   Workday vision: driving form to Review (budget %d actions)", max_actions)
    return _run_act_loop(page, client, _PAGE_SYSTEM, user, max_actions, resume_path=resume_path)
