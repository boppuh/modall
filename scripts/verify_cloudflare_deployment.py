"""Validate fail-closed invariants in the Cloudflare staging topology."""

from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "deploy/cloudflare/compose.yaml"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def mapping(value: object, label: str) -> dict[str, Any]:
    require(isinstance(value, dict), f"{label} must be an object")
    return cast(dict[str, Any], value)


def main() -> None:
    compose = mapping(yaml.safe_load(COMPOSE_PATH.read_text()), "compose")
    services = mapping(compose.get("services"), "services")
    expected = {"api", "worker", "web", "cloudflared", "prometheus", "alertmanager", "grafana"}
    require(set(services) == expected, "staging services drifted")
    for name, raw_service in services.items():
        service = mapping(raw_service, f"service {name}")
        require("ports" not in service, f"{name} must not publish a host port")
        require(
            "no-new-privileges:true" in service.get("security_opt", []),
            f"{name} must disable privilege escalation",
        )
        image = service.get("image")
        if isinstance(image, str):
            require(":" in image and not image.endswith(":latest"), f"{name} image must be pinned")

    networks = mapping(compose.get("networks"), "networks")
    for name in ("application", "dashboard", "monitoring"):
        require(
            mapping(networks.get(name), f"network {name}").get("internal") is True,
            f"{name} network must remain internal",
        )

    api = mapping(services["api"], "api")
    worker = mapping(services["worker"], "worker")
    for name, service in (("api", api), ("worker", worker)):
        require(
            set(service.get("networks", [])) == {"application", "egress"},
            f"{name} must be isolated from the edge and monitoring networks",
        )

    web = mapping(services["web"], "web")
    web_networks = mapping(web.get("networks"), "web networks")
    require(set(web_networks) == {"edge", "application"}, "web network boundary drifted")
    require(
        mapping(web_networks.get("application"), "web application network").get("ipv4_address")
        == "172.30.0.10",
        "web must retain the API's pinned trusted-proxy address",
    )

    runtime = mapping(compose.get("x-runtime-environment"), "runtime environment")
    require(runtime.get("MODALL_ENVIRONMENT") == "staging", "runtime must select staging")
    require(runtime.get("MODALL_AUTH_MODE") == "oidc", "runtime must select OIDC")
    require(
        runtime.get("MODALL_AUTH_TOKEN_SOURCE") == "cloudflare_access",
        "runtime must select Cloudflare assertions",
    )
    require(
        runtime.get("MODALL_DATABASE_URL_FILE") == "/run/secrets/database-url",
        "database credentials must come from the mounted secret",
    )
    require(
        runtime.get("MODALL_SECRET_PROVIDER") == "mounted_file",
        "runtime must use mounted provider secrets",
    )
    require(
        runtime.get("MODALL_TRUSTED_PROXY_ADDRESSES") == '["172.30.0.10"]',
        "the API must trust only the fixed web gateway address",
    )
    for variable in (
        "MODALL_CONFIRMATION_HMAC_KEY_VERSIONS",
        "MODALL_IDEMPOTENCY_HMAC_KEY_VERSIONS",
    ):
        require(
            runtime.get(variable) == f'${{{variable}:-["v1"]}}',
            f"{variable} must retain a valid JSON default",
        )

    cloudflared = mapping(services["cloudflared"], "cloudflared")
    require(
        set(cloudflared.get("networks", [])) == {"dashboard", "edge"},
        "cloudflared must reach only web and Grafana",
    )
    grafana = mapping(services["grafana"], "grafana")
    require(
        set(grafana.get("networks", [])) == {"dashboard", "monitoring"},
        "Grafana must bridge only dashboard ingress and private monitoring",
    )
    command = cloudflared.get("command")
    require(
        isinstance(command, list) and "--token-file" in command,
        "cloudflared must load its tunnel token from a file",
    )

    secrets = mapping(compose.get("secrets"), "secrets")
    require(
        set(secrets) == {"cloudflare_tunnel_token", "database_url", "grafana_admin_password"},
        "staging secret projections drifted",
    )

    def require_secret_mount(service_name: str, secret_name: str, expected_target: str) -> None:
        service = mapping(services[service_name], service_name)
        mounts = service.get("secrets")
        require(isinstance(mounts, list), f"{service_name} secrets are missing")
        matching = [
            mapping(mount, f"{service_name} secret")
            for mount in mounts
            if isinstance(mount, dict) and mount.get("source") == secret_name
        ]
        require(
            len(matching) == 1 and matching[0].get("target") == expected_target,
            f"{service_name} must mount {secret_name} at {expected_target}",
        )

    require_secret_mount("api", "database_url", "database-url")
    require_secret_mount("worker", "database_url", "database-url")
    require_secret_mount("cloudflared", "cloudflare_tunnel_token", "tunnel-token")
    require_secret_mount("grafana", "grafana_admin_password", "admin-password")

    nginx = (ROOT / "deploy/cloudflare/nginx.conf").read_text()
    for term in (
        "proxy_set_header X-Real-IP $http_cf_connecting_ip;",
        'proxy_set_header X-Forwarded-For "";',
        'proxy_set_header Authorization "";',
        'proxy_set_header Cookie "";',
        "proxy_set_header Cf-Access-Jwt-Assertion $http_cf_access_jwt_assertion;",
        "location ~ ^/health/(live|ready)$",
    ):
        require(term in nginx, "web gateway forwarding policy drifted")

    prometheus = mapping(
        yaml.safe_load((ROOT / "deploy/cloudflare/prometheus.yml").read_text()), "prometheus"
    )
    jobs = prometheus.get("scrape_configs")
    require(isinstance(jobs, list), "Prometheus scrape jobs are missing")
    require(
        {mapping(job, "scrape job").get("job_name") for job in jobs}
        == {"modall-api", "modall-worker"},
        "Prometheus must scrape both Modall processes",
    )


if __name__ == "__main__":
    main()
