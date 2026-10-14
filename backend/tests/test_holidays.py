from datetime import date

from backend.tools.calendar import local_holidays


def test_local_holidays_include_observances_and_state_days() -> None:
    oct_ = {h["name"]: h for h in local_holidays(date(2026, 10, 1), date(2026, 10, 31), "CA")}
    assert oct_["Columbus Day, also observed as Indigenous Peoples' Day"]["date"] == "2026-10-12"
    assert "Halloween" in oct_
    march = [h["name"] for h in local_holidays(date(2027, 3, 1), date(2027, 3, 31), "CA")]
    assert any("Chavez" in n for n in march)
