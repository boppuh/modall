from pathlib import Path

import pytest

from scripts.qualify_cloudflare_staging import load_trusted_origins, normalize_https_origin


def test_trusted_staging_origins_are_bare_https_origins(tmp_path: Path) -> None:
    origins = tmp_path / "origins"
    origins.write_text("https://staging.example.com\nhttps://admin.example.com:8443\n")

    assert load_trusted_origins(origins) == {
        "https://admin.example.com:8443",
        "https://staging.example.com",
    }
    assert normalize_https_origin("https://staging.example.com/") == ("https://staging.example.com")


@pytest.mark.parametrize(
    "origin",
    (
        "http://staging.example.com",
        "https://staging.example.com/path",
        "https://user@staging.example.com",
        "https://staging.example.com?redirect=elsewhere",
    ),
)
def test_trusted_staging_origins_reject_non_origins(tmp_path: Path, origin: str) -> None:
    origins = tmp_path / "origins"
    origins.write_text(origin)

    with pytest.raises(ValueError, match="trusted staging origin"):
        load_trusted_origins(origins)
