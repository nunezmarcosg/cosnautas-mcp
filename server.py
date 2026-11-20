"""Servidor MCP remoto (streamable HTTP) para consultar Cosnautas con una suscripción personal.

Uso personal: una consulta bajo demanda cada vez, sin crawling ni caché.

Variables de entorno:
  COSNAUTAS_USER          correo o usuario            (obligatoria)
  COSNAUTAS_PASSWORD      contraseña                  (obligatoria)
  MCP_PATH_SECRET         32 caracteres hex; el endpoint MCP se sirve en /mcp-<secreto>  (obligatoria)
  COSNAUTAS_LANG          es | en (por defecto es)
  COSNAUTAS_IDLE_LOGOUT   segundos de inactividad antes de cerrar sesión (por defecto 300)
  MCP_ALLOWED_HOSTS       hosts permitidos (cabecera Host), separados por comas
  PORT                    puerto de escucha (por defecto 8787)

La ruta /mcp-<secreto> funciona como credencial: no debe aparecer en logs ni documentos.
El access log de uvicorn está desactivado por ese motivo.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
import time
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
import uvicorn
from bs4 import BeautifulSoup
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import PlainTextResponse

BASE = "https://www.cosnautas.com"
LANG = os.environ.get("COSNAUTAS_LANG", "es")
IDLE_LOGOUT = int(os.environ.get("COSNAUTAS_IDLE_LOGOUT", "300"))
PORT = int(os.environ.get("PORT", "8787"))

MIN_INTERVAL = 1.0          # segundos entre peticiones a Cosnautas
MAX_TERM_LEN = 200          # longitud máxima del término de búsqueda
MAX_RESULTS = 50            # resultados máximos por recurso
MAX_HTML_BYTES = 2_097_152  # 2 MB: tope de descarga antes de parsear
MIN_MAX_CHARS, MAX_MAX_CHARS = 200, 50_000
TOOL_TIMEOUT = 50.0         # presupuesto total de una herramienta (s)
LOCK_WAIT = 20.0            # espera máxima por el lock de la sesión (s)
MAX_PENDING = 2             # llamadas admitidas (en curso + en espera); la siguiente se rechaza
MAX_REDIRECTS_LOGIN = 3
LIMIT_MSG = "Superado el límite de conexiones"

# recurso -> (nombre, criterio por defecto que usa la búsqueda rápida de la web)
RESOURCES: dict[str, tuple[str, str | None]] = {
    "libro": ("Libro Rojo", None),
    "dic": ("Diccionario de investigación clínica", "SIMPLE"),
    "siglas": ("Siglas médicas en español", None),
    "covid": ("Diccionario de covid-19", "TERM_EN"),
    "dps": ("DPS", "SIMPLE"),
    "dtq": ("DTQ", "SIMPLE"),
    "alergologia": ("Alergología e inmunología", "CONTAINS"),
}
# Valores del selector de criterio observados en el Libro Rojo.
CRITERIOS = {"SIMPLE", "STARTS", "CONTAINS", "T_ES", "T_EN", "ALL", "TERM_EN"}

INSTRUCTIONS = (
    "USO OBLIGATORIO: ante CUALQUIER pregunta sobre traducción, equivalencia o significado de un "
    "término médico o científico (inglés↔español), consulta SIEMPRE Cosnautas ANTES de responder, "
    "aunque creas saber la respuesta: el usuario es traductor médico profesional y necesita el "
    "criterio del Libro Rojo (Fernando A. Navarro), no la traducción genérica del modelo. "
    "Flujo: 1) cosnautas_search(term, resource); 2) cosnautas_get_entry(ref) para el texto completo; "
    "3) responde citando lo que dice la entrada, señalando si difiere de tu criterio propio. "
    "Recursos: libro (Libro Rojo EN→ES, el principal y el predeterminado), dic (investigación "
    "clínica), siglas (siglas médicas en español), covid, dps (dudas portugués), dtq (términos "
    "químicos), alergologia. "
    "La cuenta admite UNA sola conexión: si la dueña está usando cosnautas.com en su navegador, las "
    "consultas fallan con error de límite de conexiones; no reintentes en bucle, informa al usuario. "
    "cosnautas_logout libera la conexión."
)

mcp = MCPServer("cosnautas", instructions=INSTRUCTIONS, log_level="WARNING")


class CosnautasError(RuntimeError):
    pass


def _origin(url: httpx.URL) -> tuple[str, str | None, int | None]:
    port = url.port or (443 if url.scheme == "https" else 80 if url.scheme == "http" else None)
    return (url.scheme, url.host, port)


_BASE_ORIGIN = _origin(httpx.URL(BASE))


@dataclass
class Resp:
    status_code: int
    url: httpx.URL
    text: str


class Session:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            base_url=BASE,
            follow_redirects=True,
            timeout=30,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
                "Accept-Language": f"{LANG},en;q=0.8",
            },
        )
        self.lock = asyncio.Lock()
        self.logged_in = False
        self.last_used = 0.0
        self.last_request = 0.0
        self.watcher: asyncio.Task | None = None
        self.pending = 0  # llamadas de herramienta admitidas (en curso + en espera)

    async def _raw(
        self,
        method: str,
        url: str | httpx.URL,
        *,
        params: dict | None = None,
        data: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        allow_error_status: bool = False,
    ) -> Resp:
        wait = MIN_INTERVAL - (time.monotonic() - self.last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        req = self.client.build_request(method, url, params=params, data=data, headers=headers)
        r = await self.client.send(req, stream=True, follow_redirects=follow_redirects)
        try:
            chunks: list[bytes] = []
            size = 0
            async for chunk in r.aiter_bytes():
                room = MAX_HTML_BYTES - size
                chunks.append(chunk[:room])
                size += len(chunk)
                if size >= MAX_HTML_BYTES:  # tope de descarga: lo ya leído basta para parsear
                    break
        finally:
            await r.aclose()
        self.last_request = time.monotonic()
        text = b"".join(chunks).decode(r.encoding or "utf-8", "replace")
        if LIMIT_MSG.lower() in text.lower():
            self.logged_in = False
            raise CosnautasError(
                "Cosnautas: límite de conexiones superado. Hay otra sesión abierta con esta "
                "cuenta (navegador u otro dispositivo). Ciérrala y vuelve a intentarlo."
            )
        if r.status_code >= 400 and not allow_error_status:
            # Error del servicio: NO se trata como sesión caducada ni se fuerza re-login.
            raise CosnautasError(
                f"Cosnautas devolvió HTTP {r.status_code} para {r.url.path}. "
                "Servicio con problemas o petición rechazada; reintenta más tarde."
            )
        return Resp(r.status_code, r.url, text)

    async def _check(self) -> bool:
        r = await self._raw("GET", f"/{LANG}/user/dashboard", allow_error_status=True)
        return "/user/dashboard" in r.url.path and "/logout" in r.text

    async def _post_login(self, action: httpx.URL, data: dict[str, str], referer: str) -> Resp:
        """POST de credenciales confinado al origen de BASE, con redirecciones manuales."""
        if _origin(action) != _BASE_ORIGIN:
            raise CosnautasError(
                f"El formulario de login apunta fuera del origen esperado ({action}); "
                "no se envían credenciales."
            )
        headers = {"Referer": referer, "X-Requested-With": "XMLHttpRequest"}
        method, body = "POST", data
        url = action
        for _ in range(MAX_REDIRECTS_LOGIN + 1):
            req = self.client.build_request(method, url, data=body, headers=headers)
            r = await self.client.send(req, stream=True, follow_redirects=False)
            await r.aclose()
            self.last_request = time.monotonic()
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location", "")
                target = httpx.URL(urljoin(str(url), loc))
                if _origin(target) != _BASE_ORIGIN:
                    raise CosnautasError(
                        f"Redirección de login fuera del origen esperado ({target}); se aborta."
                    )
                url = target
                if r.status_code in (301, 302, 303):
                    method, body = "GET", None  # el cuerpo (credenciales) no se reenvía
                continue
            return Resp(r.status_code, r.url, "")
        raise CosnautasError("Demasiadas redirecciones durante el login.")

    async def _login(self) -> None:
        user = os.environ.get("COSNAUTAS_USER")
        pwd = os.environ.get("COSNAUTAS_PASSWORD")
        if not user or not pwd:
            raise CosnautasError("Faltan COSNAUTAS_USER / COSNAUTAS_PASSWORD en el entorno.")

        home = await self._raw("GET", f"/{LANG}")
        soup = BeautifulSoup(home.text, "html.parser")
        form = next((f for f in soup.find_all("form") if f.find("input", {"type": "password"})), None)

        action = f"{BASE}/login"
        if form is not None:
            # Se descubre el formulario en tiempo de ejecución: campos ocultos (_token),
            # primer campo de texto/correo como usuario y el campo de contraseña.
            data: dict[str, str] = {}
            user_set = False
            for inp in form.find_all("input"):
                name, typ = inp.get("name"), (inp.get("type") or "text").lower()
                if not name:
                    continue
                if typ == "hidden":
                    data[name] = inp.get("value", "")
                elif typ == "password":
                    data[name] = pwd
                elif typ in ("text", "email") and not user_set:
                    data[name] = user
                    user_set = True
                elif typ == "checkbox":
                    data[name] = inp.get("value") or "on"
            raw_action = (form.get("action") or "").strip()
            if raw_action and raw_action != "#":
                action = urljoin(str(home.url), raw_action)
        else:
            # Formulario no encontrado (p. ej. se inyecta por JS): nombres de campo supuestos.
            tok = soup.find("input", {"name": "_token"})
            data = {"email": user, "password": pwd, "remember": "1"}
            if tok is not None:
                data["_token"] = tok.get("value", "")

        r = await self._post_login(httpx.URL(action), data, referer=str(home.url))
        if not await self._check():
            raise CosnautasError(
                f"Login fallido (HTTP {r.status_code}). Revisa las credenciales; si son correctas, "
                "el formulario de login no coincide con lo esperado y hay que ajustar _login()."
            )
        self.logged_in = True

    async def _watch_idle(self) -> None:
        while True:
            await asyncio.sleep(30)
            if self.logged_in and time.monotonic() - self.last_used > IDLE_LOGOUT:
                async with self.lock:
                    # Revalidar DENTRO del lock: una consulta puede haber llegado mientras tanto.
                    if self.logged_in and time.monotonic() - self.last_used > IDLE_LOGOUT:
                        await self._logout()

    async def _logout(self) -> bool:
        """Cierra la sesión remota. Devuelve True solo si Cosnautas confirmó el cierre."""
        if not self.logged_in:
            return True
        try:
            await self._raw("GET", f"/{LANG}/logout")
        except Exception:
            self.logged_in = False
            return False
        self.logged_in = False
        return True

    def _expected(self, url: httpx.URL, resource: str) -> bool:
        """La URL final debe ser del origen de BASE y del recurso concretamente solicitado."""
        if _origin(url) != _BASE_ORIGIN:
            return False
        parts = [p for p in url.path.split("/") if p]
        return len(parts) >= 2 and parts[0] == LANG and parts[1] == resource

    async def get(self, path: str, params: dict | None, resource: str) -> Resp:
        try:
            await asyncio.wait_for(self.lock.acquire(), LOCK_WAIT)
        except TimeoutError:
            raise CosnautasError(
                "Servidor ocupado: hay otra consulta en curso. Reintenta en unos segundos."
            ) from None
        try:
            if self.watcher is None or self.watcher.done():
                self.watcher = asyncio.create_task(self._watch_idle())
            if not self.logged_in:
                if await self._check():
                    self.logged_in = True
                else:
                    await self._login()
            r = await self._raw("GET", path, params=params)
            if not self._expected(r.url, resource):  # sesión caducada: redirige fuera
                self.logged_in = False
                await self._login()
                r = await self._raw("GET", path, params=params)
                if not self._expected(r.url, resource):
                    raise CosnautasError(
                        f"Cosnautas devolvió una página inesperada ({r.url.path}) para el "
                        f"recurso '{resource}'; no se usan sus datos."
                    )
            self.last_used = time.monotonic()
            return r
        finally:
            self.lock.release()

    async def logout(self) -> bool:
        async with self.lock:
            return await self._logout()

    async def aclose(self) -> None:
        """Cierre ordenado: liberar la conexión de Cosnautas y el cliente HTTP."""
        with contextlib.suppress(Exception):
            async with asyncio.timeout(5):
                await self.logout()
        await self.client.aclose()


def _entry_re(resource: str) -> re.Pattern[str]:
    return re.compile(rf"/{re.escape(resource)}/(\d+)-([^/?#]+)$")


def _session_invalid(soup: BeautifulSoup) -> bool:
    """Página sin sesión: hay formulario de contraseña y FALTA el enlace de logout.

    Las páginas de Cosnautas con sesión iniciada incluyen un formulario de login oculto
    (modal), así que el password input por sí solo no distingue nada; el enlace /logout
    solo aparece con sesión válida (verificado empíricamente el 2026-11-21).
    """
    if soup.find("input", {"type": "password"}) is None:
        return False
    return not any("/logout" in (a.get("href") or "") for a in soup.find_all("a", href=True))


def parse_results(html: str, resource: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    pat, seen, out = _entry_re(resource), set(), []
    for a in soup.find_all("a", href=True):
        m = pat.search(httpx.URL(urljoin(BASE, a["href"])).path)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        title = next(iter(a.stripped_strings), "") or m.group(2)
        out.append({"resource": resource, "ref": f"{resource}/{m.group(1)}-{m.group(2)}", "title": title})
    if not out and _session_invalid(soup):
        raise CosnautasError(
            "Cosnautas devolvió una página de identificación en lugar de resultados; "
            "la sesión no es válida. Reintenta."
        )
    return out


def parse_entry(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    if _session_invalid(soup):
        raise CosnautasError(
            "Cosnautas devolvió una página de identificación en lugar de la entrada; "
            "la sesión no es válida. Reintenta."
        )
    node = (
        soup.select_one("section.entrada .entrada-content")
        or soup.select_one("section.entrada")
        or soup.find("main")
    )
    if node is None:
        raise CosnautasError("No se ha encontrado el contenido de la entrada (¿ha cambiado el HTML?).")
    lema = soup.select_one("h1.cos-entrada-lema")
    for t in node.find_all(["script", "style", "form"]):
        t.decompose()
    for br in node.find_all("br"):
        br.replace_with("\n")
    for blk in node.find_all(["p", "div", "li", "h2", "h3", "tr"]):
        blk.append("\n")
    text = re.sub(r"[ \t\r\f\v]+", " ", node.get_text())
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return {"lemma": lema.get_text(" ", strip=True) if lema else None, "text": text}


S = Session()


@contextlib.contextmanager
def _admitted():
    """Admisión explícita: con MAX_PENDING llamadas en curso/espera, la siguiente se rechaza ya."""
    if S.pending >= MAX_PENDING:
        raise CosnautasError(
            "Servidor ocupado: demasiadas consultas simultáneas. Reintenta en unos segundos."
        )
    S.pending += 1
    try:
        yield
    finally:
        S.pending -= 1


async def _search_one(term: str, resource: str, criterio: str | None) -> list[dict]:
    default = RESOURCES[resource][1]
    params = {"search": term, "fast": "1"}
    if criterio or default:
        params["criterio"] = criterio or default
    hits: list[dict] = []
    if criterio is None:  # listado rápido, como el buscador del panel
        r = await S.get(f"/{LANG}/{resource}/entradas", params, resource)
        hits = parse_results(r.text, resource)
    if not hits:  # búsqueda completa; con un único resultado la web redirige a la entrada
        params.pop("fast", None)
        r = await S.get(f"/{LANG}/{resource}", params, resource)
        m = _entry_re(resource).search(r.url.path)
        if m:
            hits = [{"resource": resource, "ref": f"{resource}/{m.group(1)}-{m.group(2)}", "title": m.group(2)}]
        else:
            hits = parse_results(r.text, resource)
    return hits


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def cosnautas_search(term: str, resource: str = "libro", criterio: str | None = None) -> dict:
    """Busca un término en los diccionarios médicos de Cosnautas. ÚSALA SIEMPRE, como primer paso
    obligatorio, ante cualquier duda de traducción o terminología médica/científica EN↔ES (también
    siglas, covid, portugués o química), antes de responder con tu propio criterio.
    resource: libro (Libro Rojo), dic, siglas, covid, dps, dtq, alergologia, o "all" (todos).
    criterio (opcional): SIMPLE, STARTS, CONTAINS, T_ES, T_EN, TERM_EN, ALL. Sin criterio se usa
    la búsqueda rápida, que devuelve solo las primeras coincidencias.
    Devuelve {results, truncated_resources, incomplete_resources}: `truncated_resources` lista los
    recursos con más coincidencias de las devueltas; `incomplete_resources` los que no se pudieron
    consultar por falta de tiempo. Cada resultado trae un `ref` para pasar a cosnautas_get_entry.
    """
    term = term.strip()
    if not term:
        raise ValueError("term vacío")
    if len(term) > MAX_TERM_LEN:
        raise ValueError(f"term demasiado largo (máximo {MAX_TERM_LEN} caracteres)")
    if criterio and criterio not in CRITERIOS:
        raise ValueError(f"criterio no válido; opciones: {sorted(CRITERIOS)}")
    targets = list(RESOURCES) if resource == "all" else [resource]
    if any(t not in RESOURCES for t in targets):
        raise ValueError(f"resource no válido; opciones: {sorted(RESOURCES)} o 'all'")

    with _admitted():
        deadline = time.monotonic() + TOOL_TIMEOUT
        results: list[dict] = []
        truncated: list[str] = []
        incomplete: list[str] = []
        for t in targets:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                incomplete.append(t)
                continue
            try:
                # TimeoutError capturado POR RECURSO: se conservan los parciales ya obtenidos.
                async with asyncio.timeout(remaining):
                    hits = await _search_one(term, t, criterio)
            except TimeoutError:
                incomplete.append(t)
                continue
            if len(hits) > MAX_RESULTS:
                hits = hits[:MAX_RESULTS]
                truncated.append(t)
            results.extend(hits)
        return {"results": results, "truncated_resources": truncated, "incomplete_resources": incomplete}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def cosnautas_get_entry(ref: str, max_chars: int = 20000) -> dict:
    """Devuelve el texto de una entrada. `ref` viene de cosnautas_search (p. ej. "libro/50379-rash")."""
    m = re.fullmatch(r"([a-z]+)/(\d+-[^/?#\s]+)", ref.strip())
    if not m or m.group(1) not in RESOURCES:
        raise ValueError("ref no válido; usa el `ref` devuelto por cosnautas_search")
    max_chars = max(MIN_MAX_CHARS, min(MAX_MAX_CHARS, max_chars))
    with _admitted():
        async with asyncio.timeout(TOOL_TIMEOUT):
            r = await S.get(f"/{LANG}/{m.group(1)}/{m.group(2)}", None, m.group(1))
        entry = parse_entry(r.text)
        truncated = len(entry["text"]) > max_chars
        return {
            "resource": RESOURCES[m.group(1)][0],
            "lemma": entry["lemma"],
            "text": entry["text"][:max_chars],
            "truncated": truncated,
            "url": str(r.url),
        }


@mcp.tool()  # SIN read_only_hint: muta la sesión compartida; la confirmación de ChatGPT es correcta.
async def cosnautas_logout() -> str:
    """Cierra la sesión de Cosnautas para liberar la conexión única de la suscripción."""
    with _admitted():
        async with asyncio.timeout(TOOL_TIMEOUT):
            ok = await S.logout()
    return "Sesión cerrada." if ok else (
        "No se pudo confirmar el cierre con Cosnautas; la sesión local se ha descartado y la "
        "conexión remota caducará sola en unos minutos."
    )


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> PlainTextResponse:
    # Sonda de vida: no toca Cosnautas.
    return PlainTextResponse("ok")


def build_app():
    secret = os.environ.get("MCP_PATH_SECRET", "")
    if not re.fullmatch(r"[0-9a-f]{32}", secret):
        print("MCP_PATH_SECRET ausente o inválida: deben ser 32 caracteres hex "
              "(genera una con `openssl rand -hex 16`).", file=sys.stderr)
        sys.exit(1)
    allowed_hosts = [
        h.strip()
        for h in os.environ.get(
            "MCP_ALLOWED_HOSTS",
            f"mcp.nataliaesperanza.com,127.0.0.1:{PORT},localhost:{PORT}",
        ).split(",")
        if h.strip()
    ]
    app = mcp.streamable_http_app(
        streamable_http_path=f"/mcp-{secret}",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[],  # ChatGPT es servidor-a-servidor (sin Origin); un Origin presente → 403
        ),
    )
    app.router.redirect_slashes = False  # /mcp-<secreto>/ no debe redirigir (contrato de ruta única)

    # Cierre ordenado: al parar el proceso (SIGTERM del redeploy), liberar la conexión de Cosnautas.
    orig_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        async with orig_lifespan(a) as state:
            try:
                yield state
            finally:
                await S.aclose()

    app.router.lifespan_context = lifespan
    return app


if __name__ == "__main__":
    uvicorn.run(
        build_app(),
        host="0.0.0.0",
        port=PORT,
        access_log=False,  # la ruta es una credencial: no debe quedar en el access log
        log_level="warning",
    )
