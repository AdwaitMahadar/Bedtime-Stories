"""Turns settings (age, controls, category) into concrete instruction text.

The same text goes to the writer, the reviser and the judge, so the judge scores the story
against exactly what the writer was asked to do.
"""

from __future__ import annotations

from . import config
from .models import StoryControls

# --- Age ---------------------------------------------------------------------------------

AGE_GUIDANCE = {
    "5-6": (
        "Ages 5-6: short sentences (mostly under 12 words), concrete everyday words, one clear problem, "
        "lots of sounds and repetition (a refrain the child can join in on is great). Feelings named simply."
    ),
    "7-8": (
        "Ages 7-8: mix short and medium sentences, some descriptive language and a few interesting words "
        "whose meaning is clear from context. One main problem with a small twist. Characters can show "
        "mixed feelings."
    ),
    "9-10": (
        "Ages 9-10: richer vocabulary and varied sentences, a light subplot or a clever twist is welcome, "
        "humor can be more verbal (wordplay, irony). Still warm, safe and age-appropriate."
    ),
}

GRADE_RANGE = {"5-6": (0.0, 2.5), "7-8": (1.5, 4.0), "9-10": (3.0, 6.0)}  # Flesch-Kincaid


def age_band(age: int) -> str:
    if age <= 6:
        return "5-6"
    if age <= 8:
        return "7-8"
    return "9-10"


# --- Controls ------------------------------------------------------------------------------

ENERGY = {
    1: (
        "Energy 1/5 (very sleepy): almost no tension. A slow, dreamy, gentle journey; nothing urgent ever happens. "
        "The main character still does the small, gentle thing that settles the story (a hum, a kind word, a found path)."
    ),
    2: "Energy 2/5 (calm): low stakes and gentle pacing. A small puzzle or worry, no chases, no peril.",
    3: "Energy 3/5 (balanced): a clear problem with some mild excitement, resolved without real danger.",
    4: "Energy 4/5 (lively): an adventure with movement and a few exciting moments. Tension stays mild and brief.",
    5: "Energy 5/5 (adventurous): a bold quest with exciting set pieces. Never frightening; the heroes are never truly in danger.",
}

SILLINESS = {
    1: "Silliness 1/5 (gentle): warm and sincere; humor only as soft smiles.",
    2: "Silliness 2/5 (light): one or two gentle funny moments.",
    3: "Silliness 3/5 (playful): regular playful moments, a funny character trait or a silly sound word.",
    4: "Silliness 4/5 (funny): a running joke, playful sound words and at least one laugh-out-loud moment.",
    5: "Silliness 5/5 (very silly): joyfully absurd situations, wordplay and silly sounds throughout, still kind (no mean jokes).",
}

LESSON = {
    1: "Lesson 1/5 (just for fun): no moral at all. Don't add one.",
    2: (
        "Lesson 2/5 (light): a gentle takeaway shown only through what happens. No character or narrator "
        "states or explains it (no lines like 'being brave means...' or 'that's what friends do')."
    ),
    3: (
        "Lesson 3/5 (balanced): a clear takeaway shown through the characters' choices. At most one short, "
        "natural line of dialogue hints at it; the narrator never explains it."
    ),
    4: "Lesson 4/5 (clear): the takeaway is visible and briefly put into words by a character, without preaching.",
    5: "Lesson 5/5 (explicit): a clear moral the child will remember, stated warmly near the end. Never preachy or scolding.",
}

LENGTH_BEATS = {"short": 3, "medium": 4, "long": 5}


def length_target(controls: StoryControls) -> int:
    return config.LENGTH_TARGETS[controls.length]


def length_bounds(controls: StoryControls) -> tuple[int, int]:
    target = length_target(controls)
    tolerance = config.LENGTH_TOLERANCE
    return round(target * (1 - tolerance)), round(target * (1 + tolerance))


def length_plan(controls: StoryControls) -> str:
    """A word budget per part, so the writer can pace the story instead of guessing at the end."""
    target = length_target(controls)
    low, high = length_bounds(controls)
    beats = LENGTH_BEATS[controls.length]
    closing = round(target * 0.15 / 10) * 10
    per_beat = round((target - closing) / beats / 10) * 10
    return (
        f"Total: {low}-{high} words (target {target}). This is measured in code, and a story outside the "
        f"range is sent back. Budget: about {per_beat} words for each of the {beats} beats, then about "
        f"{closing} words for the calm closing. First drafts tend to run long, so aim for the target, not the maximum."
    )


def describe_controls(controls: StoryControls) -> str:
    low, high = length_bounds(controls)
    return "\n".join(
        [
            f"- Length: {controls.length}, about {length_target(controls)} words (must be {low}-{high}).",
            f"- {ENERGY[controls.energy]}",
            f"- {SILLINESS[controls.silliness]}",
            f"- {LESSON[controls.lesson]}",
            "- Always: the final part slows down and settles toward sleep, whatever the Energy level.",
        ]
    )


# --- Follow-up quick actions ---------------------------------------------------------------------

LENGTH_ORDER = ["short", "medium", "long"]

QUICK_ACTIONS = ("Shorter", "Longer", "Funnier", "Calmer", "Simpler words")


def apply_quick_action(action: str, controls: StoryControls) -> tuple[StoryControls, str]:
    """Return the updated controls and the revision instruction for a quick action."""
    index = LENGTH_ORDER.index(controls.length)
    if action == "Shorter":
        new = controls.model_copy(update={"length": LENGTH_ORDER[max(index - 1, 0)]})
        return new, "Make the story shorter. Keep the best moments and the calm ending."
    if action == "Longer":
        new = controls.model_copy(update={"length": LENGTH_ORDER[min(index + 1, 2)]})
        return new, "Make the story longer by deepening the middle with one more small moment, not by padding."
    if action == "Funnier":
        new = controls.model_copy(update={"silliness": min(controls.silliness + 1, 5)})
        return new, "Make the story funnier, while keeping the plot and the calm ending."
    if action == "Calmer":
        new = controls.model_copy(update={"energy": max(controls.energy - 1, 1)})
        return new, "Make the story calmer: lower the stakes, soften exciting moments, slow the pacing."
    if action == "Simpler words":
        return controls, "Use simpler words and shorter sentences throughout, keeping the same story."
    raise ValueError(f"Unknown quick action: {action}")


def describe_control_changes(old: StoryControls, new: StoryControls) -> list[str]:
    """Turn control changes into explicit revision instructions."""
    changes = []
    if old.length != new.length:
        low, high = length_bounds(new)
        changes.append(f"Change the length from {old.length} to {new.length}: {low}-{high} words.")
    for name, table in (("energy", ENERGY), ("silliness", SILLINESS), ("lesson", LESSON)):
        before, after = getattr(old, name), getattr(new, name)
        if before != after:
            changes.append(f"Change {name} from {before}/5 to {after}/5. Target: {table[after]}")
    return changes
