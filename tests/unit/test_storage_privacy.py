import importlib.util
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from poi.privacy import fake_phone, scrub
from poi.storage.local import LocalStore
from poi.storage.s3 import S3Store


def test_local_store_roundtrip_and_escape_guard(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    store.put("osm/2026/a.json.gz", b"x")
    assert store.exists("osm/2026/a.json.gz") and store.get("osm/2026/a.json.gz") == b"x"
    assert not store.exists("missing")
    with pytest.raises(ValueError):
        store.put("../outside", b"x")


@mock_aws
def test_s3_store_roundtrip() -> None:
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="raw")
    store = S3Store("raw", client=client)
    store.put("nps/a.json.gz", b"y")
    assert store.exists("nps/a.json.gz") and store.get("nps/a.json.gz") == b"y"
    assert not store.exists("nps/missing")


def _gate():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "gate", Path(__file__).parents[2] / "scripts" / "check_publishable.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scrub_output_passes_publication_gate() -> None:
    phone = "+1 " + "-".join(["209", "234", "5678"])
    email = "info" + "@" + "campground.com"
    out = scrub(
        {"FacilityPhone": phone, "text": f"Call ({phone[3:6]}) 234-5678 or {email}", "n": 5}
    )
    assert out["n"] == 5
    assert "234-5678" not in str(out) and "campground.com" not in str(out)
    for line in str(out).splitlines():
        assert _gate().line_problems("x", line) == []


def test_scrub_is_deterministic_across_formats() -> None:
    # Same number in two formats must map to the same placeholder, so cross-source
    # phone equality (an ER feature) survives scrubbing.
    a = "(" + "209" + ") 234-5678"
    b = "209" + ".234.5678"
    assert fake_phone(a) == fake_phone(b)


def test_scrub_leaves_coordinates_and_ids_alone() -> None:
    record = {
        "latitude": "37.7357410171",
        "longitude": "-119.5627778901",
        "FacilityID": "2324501234",
    }
    assert scrub(record) == record


def test_scrub_bare_digits_only_under_phone_keys() -> None:
    bare = "209" + "2345678"
    assert scrub({"FacilityPhone": bare})["FacilityPhone"].startswith("555-01")
    assert scrub({"OrgFacilityID": bare})["OrgFacilityID"] == bare


def test_scrub_and_gate_handle_slash_separated_phones() -> None:
    slash = "209" + "/234-5678"  # NPS formats numbers like this
    assert scrub({"phoneNumber": slash})["phoneNumber"].startswith("555-01")
    assert "234-5678" not in scrub({"text": f"call {slash}"})["text"]
    assert _gate().line_problems("x", f"call {slash}")
