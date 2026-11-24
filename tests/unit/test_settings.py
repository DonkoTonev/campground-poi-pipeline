import pytest

from poi.settings import Settings, load_region


def test_default_region_loads() -> None:
    region = load_region("sierra")
    south, west, north, east = region.bbox
    assert south < north and west < east
    assert west < 0, "North American longitudes are negative"
    assert "yose" in region.nps_park_codes


def test_unknown_region_fails_loudly() -> None:
    with pytest.raises(KeyError):
        load_region("atlantis")


def test_user_agent_requires_contact_url() -> None:
    with pytest.raises(RuntimeError):
        _ = Settings(contact_url=None, _env_file=None).user_agent  # type: ignore[call-arg]
    ua = Settings(contact_url="https://example.org/me", _env_file=None).user_agent  # type: ignore[call-arg]
    assert "+https://example.org/me" in ua
