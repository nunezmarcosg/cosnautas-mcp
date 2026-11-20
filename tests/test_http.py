"""Tests del contrato HTTP: ruta secreta única, Origin/Host, healthz, arranque."""
import os

import pytest
from starlette.testclient import TestClient

import server as srv

SECRET = os.environ["MCP_PATH_SECRET"]
MCP_PATH = f"/mcp-{SECRET}"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


@pytest.fixture(scope="module")
def client():
    with TestClient(srv.build_app()) as c:
        yield c


def test_healthz_publico(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


def test_endpoint_mcp_en_ruta_secreta(client):
    r = client.post(MCP_PATH, json=INIT, headers=HEADERS)
    assert r.status_code == 200
    assert "cosnautas" in r.text


def test_rutas_no_previstas_404(client):
    for path in ["/", "/mcp", "/mcp-deadbeefdeadbeefdeadbeefdeadbeef"]:
        r = client.post(path, json=INIT, headers=HEADERS)
        assert r.status_code == 404, path


def test_barra_final_no_redirige(client):
    """El contrato de ruta única: sin 307 que duplique/revele la ruta (R1-#3)."""
    r = client.post(MCP_PATH + "/", json=INIT, headers=HEADERS, follow_redirects=False)
    assert r.status_code == 404


def test_origin_hostil_403(client):
    r = client.post(MCP_PATH, json=INIT, headers={**HEADERS, "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_sin_origin_se_acepta(client):
    # ChatGPT llama servidor-a-servidor sin cabecera Origin: debe aceptarse (ya probado arriba),
    # y un Host no permitido debe rechazarse.
    r = client.post(MCP_PATH, json=INIT, headers={**HEADERS, "Host": "evil.example"})
    assert r.status_code == 421


def test_arranque_aborta_sin_secreto_valido(monkeypatch):
    for bad in ["", "corto", "XYZ3456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef0"]:
        monkeypatch.setenv("MCP_PATH_SECRET", bad)
        with pytest.raises(SystemExit):
            srv.build_app()
