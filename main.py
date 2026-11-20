"""
Before submitting the assignment, describe here in a few sentences what you would have built next if you spent 2 more hours on this project:

The judge is the part I trust least: it catches clear problems reliably (all 20 planted problems in the judge
check), but the same line can be flagged in one review and missed in the next. So first I'd run two judge calls
per review in the app and act on the union of their must-fix lists, then measure the extra time against the
problems it catches. Second, a settings screen: the pipeline already handles a child profile (name, age,
favourite things, an avoid-list) through the CLI, but the app has no screen for it yet, and saved defaults
would make it feel personal. Third, I'd speed up the first story by running the writer while the request check
runs, keeping the draft only if the request turns out to be fine as asked. Finally, I'd grow the held-out set
and score a sample of stories by hand, to check the judge against a person on the subtle cases (stated morals,
small continuity slips) where it is weakest.

This file is the CLI. The Streamlit app is app.py, and both use the same pipeline in storyteller/.
"""

from __future__ import annotations

import argparse
import sys
import textwrap

from storyteller import tracelog
from storyteller.controls import QUICK_ACTIONS
from storyteller.llm import LLM, LLMError, check_api_key, load_api_key
from storyteller.models import ChildProfile, RoundResult, StoryControls, StoryRequest, StorySession
from storyteller.pipeline import StoryPipeline

example_requests = "A story about a girl named Alice and her best friend Bob, who happens to be a cat."

WIDTH = 88


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tell a judged, revised bedtime story for ages 5-10.")
    parser.add_argument("--request", help="the story request (otherwise you'll be asked)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show each review: must-fix problems and scores")
    parser.add_argument(
        "--trace", action="store_true", help="show every model call (tokens, time, cost) after each story"
    )
    child = parser.add_argument_group("child profile (optional)")
    child.add_argument("--name", help="child's name; makes them the hero")
    child.add_argument("--age", type=int, choices=range(5, 11), metavar="5-10", help="default 7")
    child.add_argument("--pronouns", choices=["she", "he", "they", "unspecified"], default="unspecified")
    child.add_argument("--favorites", default="", help="comma-separated, e.g. 'dinosaurs, pancakes'")
    child.add_argument("--avoid", default="", help="comma-separated things to leave out, e.g. 'spiders, the dark'")
    controls = parser.add_argument_group("story controls")
    controls.add_argument("--length", choices=["short", "medium", "long"], default="medium")
    controls.add_argument("--energy", type=int, default=2, choices=range(1, 6), metavar="1-5")
    controls.add_argument("--silliness", type=int, default=3, choices=range(1, 6), metavar="1-5")
    controls.add_argument("--lesson", type=int, default=2, choices=range(1, 6), metavar="1-5")
    return parser


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def build_request(args: argparse.Namespace, text: str) -> StoryRequest:
    profile: ChildProfile | None = None
    if args.name or args.age or args.favorites or args.avoid:
        profile = ChildProfile(
            name=args.name or "the child",
            age=args.age or 7,
            pronouns=args.pronouns,
            child_is_hero=bool(args.name),
            favorites=_split(args.favorites),
            avoid=_split(args.avoid),
        )
    controls = StoryControls(length=args.length, energy=args.energy, silliness=args.silliness, lesson=args.lesson)
    return StoryRequest(text=text, profile=profile, controls=controls)


# --- Output ------------------------------------------------------------------------------------


def wrap(text: str, indent: str = "") -> str:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return "\n\n".join(textwrap.fill(p, WIDTH, initial_indent=indent, subsequent_indent=indent) for p in paragraphs)


def print_round(result: RoundResult) -> None:
    status = "PASSED" if result.passed else "sent back"
    c = result.checks
    print(f"\n  Draft {result.round}: {status} | {c.word_count} words, reading grade {c.reading_grade}")
    for issue in result.verdict.issues:
        print(f'    ! {issue.kind}: "{textwrap.shorten(issue.quote, 60, placeholder="...")}"')
        print(f"      fix: {textwrap.shorten(issue.fix, WIDTH - 11, placeholder='...')}")
    scores = "  ".join(f"{name} {score.score}" for name, score in result.verdict.scores().items())
    print(f"    scores: {scores}")
    for failure in result.failures + result.soft_failures:
        if failure.split(":")[0] not in {i.kind for i in result.verdict.issues}:
            print(f"    ! {textwrap.shorten(failure, WIDTH - 6, placeholder='...')}")


def print_story(session: StorySession, verbose: bool) -> None:
    version = session.current
    best = version.best
    notes = best.draft.notes
    if verbose:
        for result in version.rounds:
            print_round(result)
        print()
    print("=" * WIDTH)
    print(best.draft.title.upper().center(WIDTH))
    meta = (
        f"{best.checks.word_count} words · ~{best.checks.read_minutes:g} min read aloud · "
        f"{len(version.rounds)} review(s) · ${session.total_cost:.4f} so far"
    )
    print(meta.center(WIDTH))
    print("=" * WIDTH)
    if version.stopped_early:
        print(wrap(f"Note: I stopped revising early ({version.stopped_early}), so this is the best draft so far."))
    for note in notes:
        print(wrap(f"Note: {note}"))
    if notes:
        print()
    print(wrap(best.draft.text))
    print("=" * WIDTH)


def report_run(session: StorySession, show_trace: bool) -> None:
    """Save the trace log after each run; with --trace, also print the call timeline."""
    path = tracelog.save(session)
    if show_trace:
        print("\nModel calls so far in this session:")
        print(tracelog.timeline(session.trace))
        print(f"Trace log: {path}" if path else "Trace log: couldn't be written")


def progress_printer(step: str, detail: str) -> None:
    print(f"  · {detail}", flush=True)


# --- Main loop ---------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    key = load_api_key()
    status = check_api_key(key)
    if not status.ok:
        print(f"Can't start: {status.message}", file=sys.stderr)
        return 1

    pipeline = StoryPipeline(LLM(key), on_progress=progress_printer)
    text = args.request or input("What kind of story do you want to hear? ").strip() or example_requests

    session: StorySession | None = None
    while session is None:
        try:
            session = pipeline.create(build_request(args, text))
        except LLMError as error:
            print(f"\nSorry: {error}", file=sys.stderr)
            return 1
        if session.brief.status == "out_of_scope":
            report_run(session, args.trace)
            print(f"\n{session.brief.redirect_message}\n")
            text = input("What kind of story do you want to hear? ").strip() or example_requests
            session = None

    print_story(session, args.verbose)
    report_run(session, args.trace)

    actions = ", ".join(a.lower() for a in QUICK_ACTIONS)
    while True:
        print(f"\nWant any changes? Type them, use a quick action ({actions}), or press Enter to finish.")
        reply = input("> ").strip()
        if not reply:
            print("Sweet dreams!")
            return 0
        quick = next((a for a in QUICK_ACTIONS if a.lower() == reply.lower()), None)
        try:
            outcome = pipeline.follow_up(session, message=None if quick else reply, quick_action=quick)
        except LLMError as error:
            print(f"Sorry: {error}", file=sys.stderr)
            continue
        if outcome.kind == "redirect":
            report_run(session, args.trace)
            print(outcome.message)
            continue
        session = outcome.session
        print_story(session, args.verbose)
        report_run(session, args.trace)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nGoodnight!")
        sys.exit(130)
