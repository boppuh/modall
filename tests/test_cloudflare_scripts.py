from pathlib import Path

import pytest

from scripts.qualify_cloudflare_staging import (
    is_cloudflare_access_redirect,
    load_trusted_origins,
    normalize_https_origin,
)


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


def test_cloudflare_access_redirect_requires_the_access_login_endpoint() -> None:
    assert is_cloudflare_access_redirect(
        302,
        {
            "location": (
                "https://team.cloudflareaccess.com/cdn-cgi/access/login/app?redirect_url=staging"
            )
        },
    )
    assert not is_cloudflare_access_redirect(200, {})
    assert not is_cloudflare_access_redirect(
        302, {"location": "https://identity.example.com/login"}
    )
