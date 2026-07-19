"""OpenAPI schema policy tests.

Regression coverage for #875: the app declared a hardcoded ``servers`` list
(``https://api.honcho.dev`` + ``http://localhost:8000``), which is baked into the
schema at code-definition time and drives the "Servers" dropdown in Swagger UI.
Any self-hosted deployment not reachable at exactly ``localhost:8000`` -- a
remapped host port, a LAN hostname, a reverse proxy -- got a dropdown where no
entry matched reality, so "Try it out" sent requests to the wrong origin.

Omitting ``servers`` entirely lets Swagger UI fall back to the origin the ``/docs``
page was served from, which is correct for every deployment topology. FastAPI still
advertises ``root_path`` automatically for prefix-stripping proxies.

These tests drive ``GET /openapi.json`` -- the same endpoint the browser fetches to
populate the dropdown -- rather than calling ``app.openapi()`` directly, so they also
cover the ``root_path`` server entry, which is injected per-request by the route
handler and is invisible to ``app.openapi()``.
"""

from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from src.main import app

# Deployment topologies a self-hoster actually runs. `root_path` is what the app is
# told about a proxy prefix; `origin` is where the browser reaches the API.
DEPLOYMENT_TOPOLOGIES: list[tuple[str, str, str]] = [
    # (test id, root_path, browser origin)
    ("port_remap_8070", "", "http://192.168.1.50:8070"),  # the #875 repro
    ("lan_hostname", "", "http://honcho.lan:9000"),
    (
        "localhost_default",
        "",
        "http://localhost:8000",
    ),  # the only case that ever worked
    ("proxy_prefix", "/api", "https://honcho.example.com"),
    ("proxy_prefix_trailing_slash", "/api/", "https://honcho.example.com"),
    ("proxy_root_slash", "/", "https://honcho.example.com"),  # rstrip("/") -> no entry
]


def _get_schema(root_path: str, origin: str) -> dict[str, Any]:
    """Fetch /openapi.json the way a browser at `origin` would.

    Deliberately not used as a context manager: serving the schema needs no
    database or cache, and running the lifespan handler would drag both in.
    """
    client = TestClient(app, base_url=origin, root_path=root_path)
    response = client.get("/openapi.json")
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize(
    ("root_path", "origin"),
    [pytest.param(rp, origin, id=tid) for tid, rp, origin in DEPLOYMENT_TOPOLOGIES],
)
def test_openapi_advertises_no_foreign_origin(root_path: str, origin: str) -> None:
    """No server entry may name an absolute origin.

    An absolute URL is a guess about where the API lives; it is wrong for every
    deployment that isn't the one guessed. Relative entries (or none at all) resolve
    against the origin the schema was fetched from, which is always right.
    """
    servers: list[dict[str, str]] = _get_schema(root_path, origin).get("servers", [])

    for server in servers:
        url = server.get("url", "")
        parsed = urlparse(url)
        assert not parsed.scheme and not parsed.netloc, (
            f"schema fetched from {origin} advertises absolute origin {url!r}; "
            f"a client at {origin} would send requests there instead of to "
            f"{origin} (#875)"
        )


def test_openapi_omits_servers_without_root_path() -> None:
    """Plain deployment: no `servers` key, so Swagger UI uses the docs page origin."""
    schema = _get_schema(root_path="", origin="http://192.168.1.50:8070")

    assert "servers" not in schema


@pytest.mark.parametrize(
    ("root_path", "expected"),
    [
        pytest.param("/api", [{"url": "/api"}], id="prefix"),
        pytest.param("/api/", [{"url": "/api"}], id="prefix_trailing_slash_stripped"),
        pytest.param("/", None, id="root_slash_is_not_a_prefix"),
        pytest.param("", None, id="no_prefix"),
    ],
)
def test_openapi_advertises_root_path_for_proxies(
    root_path: str, expected: list[dict[str, str]] | None
) -> None:
    """Behind a prefix-stripping proxy, FastAPI advertises `root_path` on its own.

    This is what replaces the hardcoded list for proxied deployments, so it must keep
    working once `servers` is gone.
    """
    schema = _get_schema(root_path, origin="https://honcho.example.com")

    assert schema.get("servers") == expected
