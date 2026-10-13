import pytest

from backend.voice.normalize import spoken_form


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        ("It is 10:03 right now.", "It is ten oh three right now."),
        ("Meeting at 3:30 PM on 10/13.", "Meeting at three thirty P M on October thirteenth."),
        ("Starts at 3pm, ends 5 pm.", "Starts at three P M, ends five P M."),
        ("Back at 9 a.m.", "Back at nine A M."),
        ("Call at 12:00.", "Call at twelve o'clock."),
        (
            "Costs $5.50 or $1,200.",
            "Costs five dollars and fifty cents or one thousand, two hundred dollars.",
        ),
        ("About 45% done, 2-4 hours.", "About forty five percent done, two to four hours."),
        ("The 13th of October 2026.", "The thirteenth of October twenty twenty six."),
        ("You have 3 emails & 12 events.", "You have three emails and twelve events."),
    ],
)
def test_spoken_form(text: str, spoken: str) -> None:
    assert spoken_form(text) == spoken
