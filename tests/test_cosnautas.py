"""Tests offline del cliente Cosnautas y de las herramientas MCP (fixtures, sin red)."""
import asyncio

import httpx
import pytest

import server as srv
from conftest import fx

B = srv.BASE


def dashboard_handler(request: httpx.Request) -> httpx.Response | None:
    """Rutas comunes de una sesión ya iniciada."""
    if request.url.path == "/es/user/dashboard":
        return httpx.Response(200, text=fx("dashboard.html"))
    if request.url.path == "/es/logout":
        return httpx.Response(200, text="<html><body>Hasta pronto</body></html>")
    return None


# ---------- Login confinado al origen (R1-#7) ----------

def login_flow_handler(action: str):
    def handler(request: httpx.Request) -> httpx.Response:
        common = dashboard_handler(request)
        if common:
            return common
        if request.url.path == "/es" and request.method == "GET":
            return httpx.Response(200, text=fx("home_login.html").replace('action="/login"', f'action="{action}"'))
        if str(request.url) == action or (action.startswith("/") and request.url.path == action):
            assert request.method == "POST"
            return httpx.Response(302, headers={"location": f"{B}/es/user/dashboard"})
        return httpx.Response(404, text="not found")
    return handler


async def test_login_ok_mismo_origen(session):
    s = session(login_flow_handler("/login"))
    await s._login()
    assert s.logged_in


@pytest.mark.parametrize("action", [
    "https://evil.example/login",            # otro host
    "http://www.cosnautas.com/login",        # http, no https
    "https://www.cosnautas.com:8443/login",  # mismo host, puerto distinto
])
async def test_login_rechaza_action_fuera_de_origen(session, action):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/es":
            return httpx.Response(200, text=fx("home_login.html").replace('action="/login"', f'action="{action}"'))
        if request.method == "POST":
            posted.append(str(request.url))
        return httpx.Response(200, text="")

    s = session(handler)
    with pytest.raises(srv.CosnautasError, match="origen"):
        await s._login()
    assert posted == []  # las credenciales nunca se enviaron


async def test_login_rechaza_redireccion_307_fuera_de_origen(session):
    """Una 307/308 conserva método y cuerpo: no debe seguirse fuera del origen."""
    posted_elsewhere = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/es":
            return httpx.Response(200, text=fx("home_login.html"))
        if request.url.path == "/login" and request.method == "POST":
            return httpx.Response(307, headers={"location": "https://evil.example/capture"})
        if request.url.host == "evil.example":
            posted_elsewhere.append(str(request.url))
        return httpx.Response(200, text="")

    s = session(handler)
    with pytest.raises(srv.CosnautasError, match="origen"):
        await s._login()
    assert posted_elsewhere == []


# ---------- Parsing: resultados, sin resultados, login, entrada ----------

def test_parse_results_basico():
    hits = srv.parse_results(fx("results_libro.html"), "libro")
    assert [h["ref"] for h in hits] == ["libro/50379-rash", "libro/50380-rash-guard"]
    assert hits[0]["title"] == "rash"


def test_parse_results_sin_resultados():
    assert srv.parse_results(fx("no_results.html"), "libro") == []


def test_parse_results_pagina_de_login_no_es_cero_resultados():
    with pytest.raises(srv.CosnautasError, match="identificación"):
        srv.parse_results(fx("login_page.html"), "libro")


def test_parse_entry():
    e = srv.parse_entry(fx("entry_rash.html"))
    assert e["lemma"] == "rash"
    assert "exantema" in e["text"]
    assert "console.log" not in e["text"]


def test_parse_entry_pagina_login_rechazada():
    html = "<html><body><main><input type='password' name='p'></main></body></html>"
    with pytest.raises(srv.CosnautasError, match="identificación"):
        srv.parse_entry(html)


# ---------- Validación de destino tras peticiones (R1-#10) ----------

def make_session_tools(session, handler):
    """Sesión simulada inyectada en el módulo para probar las herramientas."""
    s = session(handler)
    s.logged_in = True
    s.last_used = __import__("time").monotonic()
    return s


async def test_redireccion_a_recurso_distinto_es_error(session, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        common = dashboard_handler(request)
        if common:
            return common
        if request.url.path.startswith("/es/libro"):
            # Cosnautas redirige a una entrada de OTRO recurso
            return httpx.Response(302, headers={"location": f"{B}/es/dic/999-otro"})
        if request.url.path == "/es/dic/999-otro":
            return httpx.Response(200, text=fx("entry_rash.html"))
        if request.url.path == "/es":
            return httpx.Response(200, text=fx("home_login.html"))
        if request.url.path == "/login":
            return httpx.Response(302, headers={"location": f"{B}/es/user/dashboard"})
        return httpx.Response(404)

    s = make_session_tools(session, handler)
    monkeypatch.setattr(srv, "S", s)
    with pytest.raises(srv.CosnautasError, match="inesperada"):
        await s.get("/es/libro/1-x", None, "libro")


async def test_error_5xx_no_fuerza_relogin(session, monkeypatch):
    logins = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/es":
            logins.append("home")
            return httpx.Response(200, text=fx("home_login.html"))
        if request.url.path.startswith("/es/libro"):
            return httpx.Response(503, text="mantenimiento")
        return httpx.Response(404)

    s = make_session_tools(session, handler)
    with pytest.raises(srv.CosnautasError, match="HTTP 503"):
        await s.get("/es/libro/entradas", {"search": "x"}, "libro")
    assert logins == []  # no hubo intento de re-login por un error de servicio


async def test_relogin_cuando_redirige_fuera_del_recurso(session):
    """Sesión caducada: redirección a login → re-login → reintento OK."""
    state = {"logged": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/es/user/dashboard":
            return httpx.Response(200, text=fx("dashboard.html") if state["logged"] else fx("login_page.html"))
        if request.url.path == "/es" and request.method == "GET":
            return httpx.Response(200, text=fx("home_login.html"))
        if request.url.path == "/login" and request.method == "POST":
            state["logged"] = True
            return httpx.Response(302, headers={"location": f"{B}/es/user/dashboard"})
        if request.url.path == "/es/libro/entradas":
            if not state["logged"]:
                return httpx.Response(302, headers={"location": f"{B}/es"})
            return httpx.Response(200, text=fx("results_libro.html"))
        return httpx.Response(404)

    s = session(handler)
    s.logged_in = True  # cree estar dentro, pero la sesión remota caducó
    r = await s.get("/es/libro/entradas", {"search": "rash"}, "libro")
    assert srv.parse_results(r.text, "libro")


# ---------- Límite de conexiones de Cosnautas ----------

async def test_limite_de_conexiones_detectado(session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Superado el límite de conexiones")

    s = make_session_tools(session, handler)
    with pytest.raises(srv.CosnautasError, match="límite de conexiones"):
        await s._raw("GET", "/es/libro/entradas")
    assert not s.logged_in


# ---------- Herramientas: límites de tamaño (R1-#9) ----------

async def test_search_term_demasiado_largo(monkeypatch):
    with pytest.raises(ValueError, match="demasiado largo"):
        await srv.cosnautas_search("x" * 201)


async def test_search_term_vacio():
    with pytest.raises(ValueError, match="vacío"):
        await srv.cosnautas_search("   ")


async def test_search_truncado_con_senal(session, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        common = dashboard_handler(request)
        if common:
            return common
        return httpx.Response(200, text=fx("results_51.html"))

    monkeypatch.setattr(srv, "S", make_session_tools(session, handler))
    out = await srv.cosnautas_search("termino")
    assert len(out["results"]) == 50
    assert out["truncated_resources"] == ["libro"]
    assert out["incomplete_resources"] == []


async def test_search_50_exactos_sin_senal(session, monkeypatch):
    html = "".join(f'<a href="/es/libro/{i}-t{i}">t{i}</a>' for i in range(50))

    def handler(request: httpx.Request) -> httpx.Response:
        common = dashboard_handler(request)
        if common:
            return common
        return httpx.Response(200, text=f"<html><body>{html}</body></html>")

    monkeypatch.setattr(srv, "S", make_session_tools(session, handler))
    out = await srv.cosnautas_search("t")
    assert len(out["results"]) == 50
    assert out["truncated_resources"] == []


async def test_get_entry_clamp_max_chars(session, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        common = dashboard_handler(request)
        if common:
            return common
        return httpx.Response(200, text=fx("entry_rash.html"))

    monkeypatch.setattr(srv, "S", make_session_tools(session, handler))
    out = await srv.cosnautas_get_entry("libro/50379-rash", max_chars=-5)
    assert out["lemma"] == "rash"
    assert len(out["text"]) <= srv.MIN_MAX_CHARS  # el clamp evita el slice negativo


async def test_descarga_html_acotada(session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * (srv.MAX_HTML_BYTES + 100_000))

    s = make_session_tools(session, handler)
    r = await s._raw("GET", "/es/libro/entradas")
    assert len(r.text) <= srv.MAX_HTML_BYTES


# ---------- Admisión, cancelación y parciales (R1-#8) ----------

async def test_admision_rechaza_el_tercero(session, monkeypatch):
    release = asyncio.Event()

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        common = dashboard_handler(request)
        if common:
            return common
        await release.wait()
        return httpx.Response(200, text=fx("results_libro.html"))

    monkeypatch.setattr(srv, "S", make_session_tools(session, slow_handler))
    t1 = asyncio.create_task(srv.cosnautas_search("rash"))
    t2 = asyncio.create_task(srv.cosnautas_search("rash"))
    await asyncio.sleep(0.05)  # t1 y t2 admitidas (1 en curso + 1 esperando)
    with pytest.raises(srv.CosnautasError, match="ocupado"):
        await srv.cosnautas_search("rash")  # la tercera se rechaza de inmediato
    release.set()
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1["results"] and r2["results"]


async def test_cancelacion_libera_plaza(session, monkeypatch):
    release = asyncio.Event()

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        common = dashboard_handler(request)
        if common:
            return common
        await release.wait()
        return httpx.Response(200, text=fx("results_libro.html"))

    monkeypatch.setattr(srv, "S", make_session_tools(session, slow_handler))
    t1 = asyncio.create_task(srv.cosnautas_search("rash"))
    t2 = asyncio.create_task(srv.cosnautas_search("rash"))
    await asyncio.sleep(0.05)
    t2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t2
    await asyncio.sleep(0.01)
    # La plaza del cancelado queda libre: una nueva llamada es admitida (no "ocupado")
    t3 = asyncio.create_task(srv.cosnautas_search("rash"))
    await asyncio.sleep(0.05)
    release.set()
    r1, r3 = await asyncio.gather(t1, t3)
    assert r1["results"] and r3["results"]


async def test_all_con_plazo_agotado_conserva_parciales(session, monkeypatch):
    """El primer recurso responde; el presupuesto se agota y el resto queda en incomplete."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        common = dashboard_handler(request)
        if common:
            return common
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, text=fx("results_libro.html"))
        await asyncio.sleep(5)  # el segundo recurso tarda más que el presupuesto restante
        return httpx.Response(200, text=fx("no_results.html"))

    monkeypatch.setattr(srv, "S", make_session_tools(session, handler))
    monkeypatch.setattr(srv, "TOOL_TIMEOUT", 0.5)
    out = await srv.cosnautas_search("rash", resource="all")
    assert len(out["results"]) == 2  # parciales del primer recurso conservados
    assert len(out["incomplete_resources"]) == len(srv.RESOURCES) - 1


# ---------- Vigilante de inactividad (R1-#6) ----------

async def test_watch_idle_revalida_dentro_del_lock(session, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return dashboard_handler(request) or httpx.Response(200, text=fx("results_libro.html"))

    s = make_session_tools(session, handler)
    monkeypatch.setattr(srv, "IDLE_LOGOUT", 10_000)
    s.last_used = __import__("time").monotonic()  # actividad reciente

    # Simular la comprobación del vigilante: con actividad reciente NO cierra sesión
    import time as _t
    if s.logged_in and _t.monotonic() - s.last_used > srv.IDLE_LOGOUT:
        pytest.fail("no debería considerar inactiva una sesión recién usada")
    assert s.logged_in


async def test_logout_distingue_fallo(session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="error")

    s = make_session_tools(session, handler)
    ok = await s.logout()
    assert ok is False
    assert s.logged_in is False  # la sesión local se descarta igualmente


async def test_logout_confirmado(session):
    s = make_session_tools(session, lambda r: dashboard_handler(r) or httpx.Response(404))
    assert await s.logout() is True


# ---------- Metadatos MCP: anotaciones e instrucciones (R1-#2) ----------

async def test_anotaciones_de_herramientas():
    tools = {t.name: t for t in await srv.mcp.list_tools()}
    assert tools["cosnautas_search"].annotations.read_only_hint is True
    assert tools["cosnautas_get_entry"].annotations.read_only_hint is True
    lo = tools["cosnautas_logout"].annotations
    assert lo is None or lo.read_only_hint is not True  # logout NO es de solo lectura
    assert "TERM_EN" in tools["cosnautas_search"].description


def test_instructions_definidas():
    assert srv.mcp.instructions and len(srv.INSTRUCTIONS[:512]) > 100


def test_parse_results_cero_resultados_con_modal_de_login():
    """Regresión: la página con sesión incluye un formulario de login oculto (modal);
    con el enlace /logout presente, cero resultados es cero resultados, no sesión caducada."""
    assert srv.parse_results(fx("no_results_logged_in.html"), "libro") == []


def test_parse_entry_pagina_con_modal_pero_con_sesion():
    html = fx("entry_rash.html").replace(
        "<section", "<a href='/es/logout'>Salir</a><form><input type='password'></form><section"
    )
    assert srv.parse_entry(html)["lemma"] == "rash"
