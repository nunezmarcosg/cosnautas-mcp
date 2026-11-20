import os
import sys
from pathlib import Path

os.environ.setdefault("COSNAUTAS_USER", "usuario-prueba")
os.environ.setdefault("COSNAUTAS_PASSWORD", "clave-prueba")
os.environ.setdefault("MCP_PATH_SECRET", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("MCP_ALLOWED_HOSTS", "mcp.nataliaesperanza.com,testserver,127.0.0.1:8787,localhost:8787")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

import server as srv


@pytest.fixture
def session(monkeypatch):
    """Session nueva con transporte simulado y sin pausas entre peticiones."""
    monkeypatch.setattr(srv, "MIN_INTERVAL", 0.0)

    def make(handler):
        s = srv.Session()
        s.client = httpx.AsyncClient(
            base_url=srv.BASE,
            follow_redirects=True,
            timeout=5,
            transport=httpx.MockTransport(handler),
        )
        return s

    return make


FIXTURES = Path(__file__).parent / "fixtures"


def fx(name: str) -> str:
    return (FIXTURES / name).read_text()
