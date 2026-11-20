"""Streamlit UI for the bedtime story generator. Run with: streamlit run app.py

The UI only displays: every decision is made in the `storyteller` package, which the CLI shares.
Streamlit reruns this script on every click, so the pipeline runs only inside a submit handler, and
its results live in `st.session_state`. Nothing is regenerated while the parent clicks around.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Callable, Optional, TypeVar, get_args

import streamlit as st

from storyteller import config, tracelog
from storyteller.controls import CONTROL_LABELS, QUICK_ACTIONS
from storyteller.llm import LLM, APICallError, KeyStatus, LLMError, check_api_key, load_api_key
from storyteller.models import Category, StoryControls, StoryRequest, StorySession, StoryVersion
from storyteller.pipeline import StoryPipeline

T = TypeVar("T")

EXAMPLES = (
    "A dragon who is afraid of the dark",
    "A girl and her cat build a boat to visit the moon",
    "A very silly pancake who wants to fly",
    "A calm story about a sleepy whale",
)

KEY_PROBLEM_TITLES = {
    "missing": "No API key yet",
    "invalid": "The API key didn't work",
    "no_access": "The API key can't use the story model",
    "rate_limited": "Too many requests right now",
    "network": "Can't reach Anthropic",
    "unknown": "Something went wrong while checking the key",
}

REQUEST_STATUS = {
    "ok": "Used as asked",
    "adjusted": "Gently changed to suit bedtime",
    "out_of_scope": "Not a story request",
}

STEP_ICONS = {
    "analyzer": ":material/search:",
    "writer": ":material/edit:",
    "judge": ":material/gavel:",
    "reviser": ":material/auto_fix_high:",
    "followup": ":material/chat:",
    "done": ":material/check_circle:",
}

STYLE = """<style>
.story { font-family: Georgia, "Iowan Old Style", serif; font-size: 1.15rem; line-height: 1.75;
  padding: 1.5rem 1.75rem; margin: 0.5rem 0 1rem; border-radius: 0.75rem;
  background: rgba(250, 236, 205, 0.28); border: 1px solid rgba(160, 125, 80, 0.25); }
.story p { margin: 0 0 1em; }
.story p:last-child { margin-bottom: 0; }
</style>"""


# --- Small helpers -----------------------------------------------------------------------------


def md(text: str) -> str:
    """Escape user or model text so Streamlit shows it literally instead of as Markdown."""
    return re.sub(r"([\\`*_\[\]<>#|~$])", r"\\\1", text)


def story_html(text: str) -> str:
    """The story as escaped HTML paragraphs in a book-like card (no blank lines, so Markdown leaves it alone).

    The writer sometimes marks sound words as *whoosh*; those become italics instead of showing asterisks.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    body = "".join(
        "<p>" + re.sub(r"\*([^*\n]+)\*", r"<em>\1</em>", html.escape(p)).replace("\n", "<br>") + "</p>"
        for p in paragraphs
    )
    return f'<div class="story">{body}</div>'


def notes_for(session: StorySession, version: StoryVersion) -> list[str]:
    """Notes for the parent: what was changed or chosen for the first story, or for a follow-up."""
    if version.number == 1:
        return session.brief.adjustments + session.brief.assumptions
    return version.notes


def pick_reason(version: StoryVersion) -> str:
    """Why this version shows the draft it does, in plain words."""
    best = version.best
    rounds = len(version.rounds)
    if best.passed:
        if rounds == 1:
            return "The first draft passed every check, so it wasn't revised."
        return f"Round {best.round} passed every check."
    reason = (
        f"No draft passed every check in {rounds} round(s), so this shows round {best.round}: "
        f"it had the highest lowest score ({best.verdict.min_score}/5)."
    )
    if version.stopped_early:
        reason += f" Revising stopped early: {version.stopped_early}."
    return reason


def describe_controls(controls: StoryControls) -> str:
    return " · ".join(
        [f"{controls.length.title()} length"]
        + [f"{name.title()} {getattr(controls, name)} ({CONTROL_LABELS[name][getattr(controls, name)]})"
           for name in ("energy", "silliness", "lesson")]
    )


def short_path(path: Path) -> str:
    return str(path).replace(str(Path.home()), "~", 1)


# --- State -----------------------------------------------------------------------------------


def init_state() -> None:
    state = st.session_state
    if "sessions" not in state:
        state.sessions = []  # every story told in this browser tab, oldest first
    if "current" not in state:
        state.current = None  # id of the story on screen; None shows the request screen


def current_session() -> Optional[StorySession]:
    return next((s for s in st.session_state.sessions if s.id == st.session_state.current), None)


def open_story(session_id: Optional[str]) -> None:
    st.session_state.current = session_id


def add_story(session: StorySession) -> None:
    st.session_state.sessions.append(session)
    st.session_state.current = session.id


# --- API key ---------------------------------------------------------------------------------


def ensure_llm() -> bool:
    """Check the key once per browser tab. Without a working key, show the blocking screen."""
    if "llm" in st.session_state:
        return True
    key = load_api_key()
    status = check_api_key(key)
    if status.ok:
        st.session_state.llm = LLM(key)
        return True
    key_screen(status)
    return False


def key_screen(status: KeyStatus) -> None:
    st.title("🌙 Bedtime Stories")
    st.subheader(KEY_PROBLEM_TITLES.get(status.kind or "unknown", KEY_PROBLEM_TITLES["unknown"]))
    st.error(status.message, icon=":material/key:")
    if status.kind in ("missing", "invalid", "no_access"):
        st.markdown(
            "1. In the project folder, run `cp .env.example .env`\n"
            "2. Open `.env` and paste your Anthropic API key after `ANTHROPIC_API_KEY=`\n"
            "3. Save the file and click **Retry**. No restart needed."
        )
        st.caption(
            "If ANTHROPIC_API_KEY is also set in your shell, that one is used instead: fix or unset it, "
            "then restart the app. The app never asks for, stores or logs your key."
        )
    st.button("Retry", type="primary", icon=":material/refresh:")


# --- Running the pipeline ----------------------------------------------------------------------


def run_with_progress(label: str, task: Callable[[StoryPipeline], T]) -> Optional[T]:
    """Run a pipeline task with live progress. On failure, show a friendly message and return None."""
    with st.status(label, expanded=True) as status:

        def on_progress(step: str, detail: str) -> None:
            status.write(f"{STEP_ICONS.get(step, ':material/more_horiz:')} {detail}")

        try:
            result = task(StoryPipeline(st.session_state.llm, on_progress=on_progress))
        except LLMError as error:
            status.update(label="Something went wrong", state="error", expanded=False)
            if isinstance(error, APICallError) and error.kind in ("invalid", "no_access"):
                del st.session_state["llm"]  # the next click shows the key screen
            st.error(f"{error} Nothing was lost: you can try again.", icon=":material/error:")
            return None
        status.update(label="Done", state="complete", expanded=False)
    return result


# --- Controls --------------------------------------------------------------------------------


def controls_editor(current: StoryControls, key: str, with_story_type: bool = True) -> StoryControls:
    """Widgets for the story controls, prefilled from `current`. Returns the chosen values."""
    minutes = {name: round(words / config.READ_ALOUD_WPM) for name, words in config.LENGTH_TARGETS.items()}
    length = st.segmented_control(
        "Length", list(minutes), default=current.length, required=True, key=f"{key}_length",
        format_func=lambda name: f"{name.title()} · ~{minutes[name]} min",
    )
    values = {}
    for name, help_text in (
        ("energy", "How much happens: 1 is a sleepy drift, 5 is a bold (never scary) quest."),
        ("silliness", "1 is warm and gentle, 5 is joyfully absurd."),
        ("lesson", "1 is just for fun, 5 is a clear moral stated at the end."),
    ):
        labels = CONTROL_LABELS[name]
        values[name] = st.select_slider(
            name.title(), options=list(labels), value=getattr(current, name), key=f"{key}_{name}",
            format_func=lambda v, labels=labels: f"{v} · {labels[v]}", help=help_text,
        )
    update = {"length": length, **values}
    if with_story_type:
        update["story_type"] = st.selectbox(
            "Story type", [None, *get_args(Category)], key=f"{key}_type",
            index=[None, *get_args(Category)].index(current.story_type),
            format_func=lambda c: "Let the storyteller choose" if c is None else c.title(),
        )
    return current.model_copy(update=update)


# --- Screens ---------------------------------------------------------------------------------


def sidebar() -> None:
    sessions: list[StorySession] = st.session_state.sessions
    with st.sidebar:
        st.title("🌙 Bedtime Stories")
        st.button(
            "New story", icon=":material/add:", width="stretch", on_click=open_story, args=(None,),
            type="primary" if st.session_state.current is None else "secondary",
        )
        if not sessions:
            return
        st.subheader("Tonight's stories")
        for session in reversed(sessions):
            count = len(session.versions)
            label = session.current.best.draft.title + (f" ({count} versions)" if count > 1 else "")
            st.button(
                label, key=f"open_{session.id}", width="stretch", on_click=open_story, args=(session.id,),
                type="primary" if session.id == st.session_state.current else "tertiary",
            )
        st.caption(
            f"Kept until you close this tab. Cost so far: ${sum(s.total_cost for s in sessions):.4f}"
        )


def set_request(text: str) -> None:
    st.session_state.request_text = text


def request_screen() -> None:
    st.title("What's tonight's story about?")
    st.caption("Every story is reviewed against a checklist and revised before you see it.")

    examples = st.container(horizontal=True)
    for example in EXAMPLES:
        examples.button(example, on_click=set_request, args=(example,), icon=":material/lightbulb:")

    with st.form("request_form"):
        text = st.text_area(
            "Story idea", key="request_text", height=110,
            placeholder="A name, an animal, a place, a worry to soothe... anything works, even just 'a story'.",
        )
        with st.expander("Story settings"):
            controls = controls_editor(StoryControls(), "new")
        submitted = st.form_submit_button("Tell me a story", type="primary", icon=":material/auto_stories:")

    if not submitted:
        return
    if not text.strip():
        st.warning("Type a story idea, or pick one of the examples above.")
        return
    request = StoryRequest(text=text.strip(), controls=controls)
    session = run_with_progress("Writing your story...", lambda pipeline: pipeline.create(request))
    if session is None:
        return
    tracelog.save(session)
    if session.brief.status == "out_of_scope":
        st.info(session.brief.redirect_message, icon=":material/auto_stories:")
        return
    add_story(session)
    st.rerun()


def story_screen(session: StorySession) -> None:
    versions = session.versions
    version = versions[-1]
    if len(versions) > 1:
        number = st.segmented_control(
            "Version", [v.number for v in versions], default=version.number, required=True,
            key=f"version_{session.id}_{len(versions)}",  # a new version resets the choice to the latest
            format_func=lambda n: f"v{n}",
        )
        version = versions[number - 1]
        origin = "Your first request" if version.number == 1 else f"Changed: {md(version.origin)}"
        st.caption(origin)

    show_story(session, version)
    if version is versions[-1]:
        follow_up_box(session, version)
    else:
        st.info(f"You're reading v{version.number}. Changes always start from the latest version, v{len(versions)}.")
    how_it_was_made(session, version)


def show_story(session: StorySession, version: StoryVersion) -> None:
    best = version.best
    st.header(md(best.draft.title), anchor=False)
    row = st.container(horizontal=True, vertical_alignment="center")
    row.caption(f"{best.checks.word_count} words · about {best.checks.read_minutes:g} min read aloud")
    if best.passed:
        row.badge("Passed review", icon=":material/check:", color="green")
    else:
        row.badge("Best effort", icon=":material/info:", color="orange", help=pick_reason(version))

    notes = notes_for(session, version)
    if notes:
        st.info("\n".join(f"- {md(note)}" for note in notes), icon=":material/edit_note:")
    if version.stopped_early:
        st.warning(f"I stopped revising early ({version.stopped_early}), so this is the best draft so far.")
    st.markdown(story_html(best.draft.text), unsafe_allow_html=True)
    st.download_button(
        "Download", f"{best.draft.title}\n\n{best.draft.text}\n", icon=":material/download:",
        file_name=re.sub(r"[^a-z0-9]+", "-", best.draft.title.lower()).strip("-") + ".txt",
        key=f"download_{session.id}_{version.number}", on_click="ignore",
    )


def follow_up_box(session: StorySession, version: StoryVersion) -> None:
    st.subheader("Change this story", anchor=False)
    controls = version.controls
    at_limit = {
        "Shorter": controls.length == "short",
        "Longer": controls.length == "long",
        "Funnier": controls.silliness == 5,
        "Calmer": controls.energy == 1,
    }
    actions = st.container(horizontal=True)
    clicked = None
    for action in QUICK_ACTIONS:
        if actions.button(action, key=f"quick_{session.id}_{action}", disabled=at_limit.get(action, False)):
            clicked = action

    with st.form(f"followup_{session.id}", clear_on_submit=True):
        message = st.text_input(
            "Describe a change",
            placeholder="e.g. give the dragon a little sister, or: a new story about pirates",
        )
        with st.expander("Change story settings"):
            new_controls = controls_editor(controls, f"edit_{session.id}_{version.number}", with_story_type=False)
        submitted = st.form_submit_button("Update story", type="primary", icon=":material/auto_fix_high:")

    if clicked:
        apply_follow_up(session, quick_action=clicked)
    elif submitted:
        changed = new_controls if new_controls != controls else None
        if not message.strip() and changed is None:
            st.warning("Type a change, or adjust a setting first.")
            return
        apply_follow_up(session, message=message.strip() or None, controls=changed)


def apply_follow_up(session: StorySession, **change) -> None:
    outcome = run_with_progress("Updating the story...", lambda pipeline: pipeline.follow_up(session, **change))
    if outcome is None:
        return
    tracelog.save(outcome.session)
    if outcome.kind == "redirect":
        st.info(outcome.message, icon=":material/auto_stories:")
        return
    if outcome.kind == "new":
        add_story(outcome.session)
    st.rerun()


# --- "How this story was made" -----------------------------------------------------------------


def how_it_was_made(session: StorySession, version: StoryVersion) -> None:
    with st.expander("How this story was made", icon=":material/insights:"):
        plan, review, calls = st.tabs(["Plan", "Review rounds", "Model calls"])
        with plan:
            show_plan(session, version)
        with review:
            show_rounds(version)
        with calls:
            show_calls(session)


def show_plan(session: StorySession, version: StoryVersion) -> None:
    brief = session.brief
    lines = [
        f"**Request:** {md(session.request.text)}",
        f"**Request check:** {REQUEST_STATUS[brief.status]}",
        f"**Story type:** {brief.category} · **Tone:** {md(brief.tone)}",
        f"**Premise:** {md(brief.premise)}",
        "**Characters:**\n" + "\n".join(f"- **{md(c.name)}**: {md(c.description)}" for c in brief.characters),
        f"**Settings for this version:** {describe_controls(version.controls)}",
    ]
    if brief.must_include:
        lines.insert(4, "**Must include:** " + ", ".join(md(item) for item in brief.must_include))
    st.markdown("\n\n".join(lines))
    if version.instruction:
        st.markdown("**Change applied in this version:**\n\n" + md(version.instruction))


def mark(ok: bool) -> str:
    return ":green[✓]" if ok else ":red[✗]"


def score_badge(score: int) -> str:
    color = "green" if score >= config.PASS_MIN_SCORE else "orange" if score == 3 else "red"
    return f":{color}-badge[{score}/5]"


def show_rounds(version: StoryVersion) -> None:
    st.markdown(f"**Shown:** {pick_reason(version)}")
    st.caption(
        f"A draft passes when every criterion scores at least {config.PASS_MIN_SCORE}/5, the length is "
        "in range, nothing from the avoid-list appears, and the reading level isn't above the child's age."
    )
    for index, result in enumerate(version.rounds):
        status = "passed" if result.passed else "sent back"
        shown = " · shown" if index == version.best_round else ""
        st.markdown(f"**Round {result.round}: {status}{shown}**")
        checks = result.checks
        avoid = f"avoid-list: {', '.join(checks.avoid_hits)}" if checks.avoid_hits else "avoid-list clear"
        st.markdown(" · ".join([
            f"{mark(checks.length_ok)} {checks.word_count} words (allowed {checks.length_min}–{checks.length_max})",
            f"{mark(checks.reading_grade <= checks.grade_max)} reading grade {checks.reading_grade:g} "
            f"(up to {checks.grade_max:g} for this age)",
            f"{mark(not checks.avoid_hits)} {avoid}",
        ]))
        lines = []
        for name, score in result.verdict.scores().items():
            line = f"- {score_badge(score.score)} **{name.replace('_', ' ')}**: {md(score.evidence)}"
            if score.fix:
                line += f"  \n  *Fix:* {md(score.fix)}"
            lines.append(line)
        st.markdown("\n".join(lines))
        st.caption(f"Reviewer's summary: {md(result.verdict.summary)}")


def show_calls(session: StorySession) -> None:
    trace = session.trace
    tokens = sum(e.input_tokens + e.output_tokens for e in trace)
    columns = st.columns(4)
    columns[0].metric("Model calls", len(trace))
    columns[1].metric("Tokens", f"{tokens:,}")
    columns[2].metric("Model time", f"{sum(e.latency_ms for e in trace) / 1000:.0f} s")
    columns[3].metric("Cost", f"${session.total_cost:.4f}")
    rows = [
        {
            "Step": e.step, "Try": e.attempt, "OK": e.ok, "Tokens in": e.input_tokens,
            "Tokens out": e.output_tokens, "Seconds": e.latency_ms / 1000, "Cost": e.cost_usd,
            "Note": e.error or "",
        }
        for e in trace
    ]
    st.dataframe(
        rows, hide_index=True, width="stretch",
        column_config={
            "Seconds": st.column_config.NumberColumn(format="%.1f"),
            "Cost": st.column_config.NumberColumn(format="$%.5f"),
        },
    )
    st.caption(
        f"Every call in this story so far, all to {config.MODEL}. The same metadata, never the story or "
        f"your child's details, is saved to {short_path(config.LOG_DIR)}."
    )


# --- Entry point -----------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Bedtime Stories", page_icon="🌙")
    st.markdown(STYLE, unsafe_allow_html=True)
    init_state()
    if not ensure_llm():
        return
    sidebar()
    session = current_session()
    if session is None:
        request_screen()
    else:
        story_screen(session)


if __name__ == "__main__":
    main()
