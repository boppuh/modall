"""Run payload-free reachability and identity checks against Cloudflare staging."""

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import UUID


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        del req, fp, code, msg, headers, newurl
        return None


def request_status(
    base_url: str,
    path: str,
    *,
    cookie: str | None = None,
    workspace_id: UUID | None = None,
) -> tuple[int, bytes]:
    headers = {"Accept": "application/json"}
    if cookie is not None:
        headers["Cookie"] = f"CF_Authorization={cookie}"
    if workspace_id is not None:
        headers["X-Workspace-ID"] = str(workspace_id)
    request = urllib.request.Request(urljoin(base_url, path.lstrip("/")), headers=headers)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read(65_537)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65_537)
    except (OSError, urllib.error.URLError):
        raise RuntimeError(f"staging request failed for {path}") from None


def load_cookie(path: Path) -> str:
    try:
        raw = path.read_bytes()
        if not 1 <= len(raw) <= 16_384 or b"\x00" in raw:
            raise ValueError
        cookie = raw.decode("utf-8")
        if cookie != cookie.strip():
            raise ValueError
        return cookie
    except (OSError, UnicodeError, ValueError):
        raise ValueError("Access cookie file is unavailable or invalid") from None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--workspace-id", required=True, type=UUID)
    parser.add_argument("--access-cookie-file", required=True, type=Path)
    args = parser.parse_args()
    parsed = urlsplit(args.base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError("base URL must be a bare HTTPS origin")
    base_url = args.base_url.rstrip("/") + "/"
    cookie = load_cookie(args.access_cookie_file)

    unauthenticated, _ = request_status(base_url, "/v1/session")
    if 200 <= unauthenticated < 300:
        raise RuntimeError("Cloudflare Access did not reject an unauthenticated request")
    exposed_metrics, _ = request_status(base_url, "/metrics")
    if 200 <= exposed_metrics < 300:
        raise RuntimeError("the metrics surface is publicly reachable")

    for path in ("/health/live", "/health/ready"):
        status, body = request_status(base_url, path, cookie=cookie)
        try:
            health = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError(f"staging health response was invalid for {path}") from None
        if (
            status != 200
            or not isinstance(health, dict)
            or health.get("service") != "api"
            or health.get("status") not in {"ok", "ready"}
        ):
            raise RuntimeError(f"staging health check failed for {path}")

    status, body = request_status(
        base_url,
        "/v1/session",
        cookie=cookie,
        workspace_id=args.workspace_id,
    )
    try:
        session = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("staging identity response was invalid") from None
    if (
        status != 200
        or not isinstance(session, dict)
        or session.get("workspace_id") != str(args.workspace_id)
        or session.get("role") not in {"admin", "operator", "viewer"}
    ):
        raise RuntimeError("staging identity check failed")
    print(json.dumps({"status": "qualified", "workspace_id": str(args.workspace_id)}))


if __name__ == "__main__":
    main()
