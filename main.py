"""
Before submitting the assignment, describe here in a few sentences what you would have built next if you spent 2 more hours on this project:

"""

from __future__ import annotations

import argparse
import sys
import textwrap
from typing import Optional

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
    parser.add_argument("-v", "--verbose", action="store_true", help="show the judge's scores and notes each round")
    parser.add_argument("--trace", action="store_true", help="show every model call (tokens, time, cost) after each story")
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
    profile: Optional[ChildProfile] = None
    if args.name or args.age or args.favorites or args.avoid:
        profile = ChildProfile(
            name=args.name or "the child",
            age=args.age or 7,
            pronouns=args.pronouns,
            child_is_hero=bool(args.name),
            favorites=_split(args.favorites),
            avoid=_split(args.avoid),
        )
    controls = StoryControls(
        length=args.length, energy=args.energy, silliness=args.silliness, lesson=args.lesson
    )
    return StoryRequest(text=text, profile=profile, controls=controls)


# --- Output ------------------------------------------------------------------------------------


def wrap(text: str, indent: str = "") -> str:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return "\n\n".join(textwrap.fill(p, WIDTH, initial_indent=indent, subsequent_indent=indent) for p in paragraphs)


def print_round(result: RoundResult) -> None:
    status = "PASSED" if result.passed else "needs work"
    c = result.checks
    print(f"\n  Round {result.round}: {status} | {c.word_count} words, reading grade {c.reading_grade}")
    for name, score in result.verdict.scores().items():
        line = f"    {name:<17} {score.score}/5"
        if score.score < 4 and score.fix:
            line += "  -> " + textwrap.shorten(score.fix, WIDTH, placeholder="...")
        print(line)
    for failure in result.failures:
        if "scored" not in failure:
            print(f"    ! {failure}")


def print_story(session: StorySession, verbose: bool) -> None:
    version = session.current
    best = version.best
    notes = version.notes if version.number > 1 else session.brief.adjustments + session.brief.assumptions
    if verbose:
        for result in version.rounds:
            print_round(result)
        print()
    print("=" * WIDTH)
    print(best.draft.title.upper().center(WIDTH))
    meta = (
        f"{best.checks.word_count} words · ~{best.checks.read_minutes:g} min read aloud · "
        f"{len(version.rounds)} judge round(s) · ${session.total_cost:.4f} so far"
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


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    key = load_api_key()
    status = check_api_key(key)
    if not status.ok:
        print(f"Can't start: {status.message}", file=sys.stderr)
        return 1

    pipeline = StoryPipeline(LLM(key), on_progress=progress_printer)
    text = args.request or input("What kind of story do you want to hear? ").strip() or example_requests

    session: Optional[StorySession] = None
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
