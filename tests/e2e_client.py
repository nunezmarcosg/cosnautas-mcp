"""Cliente MCP de verificación extremo a extremo (local, contenedor o producción).

Uso:  python tests/e2e_client.py <URL-del-endpoint-MCP> [--quick]

--quick: solo initialize + tools/list + una búsqueda + logout (para contenedor/prod).
Sin --quick: una búsqueda por recurso, caso sin resultados y lectura de entrada.
No imprime nunca la URL completa (es una credencial).
"""
import asyncio
import sys

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

SEARCHES = [  # (resource, term) representativos de los 7 recursos
    ("libro", "rash"),
    ("dic", "placebo"),
    ("siglas", "TA"),
    ("covid", "spike"),
    ("dps", "exame"),
    ("dtq", "benzene"),
    ("alergologia", "mast cell"),
]


def data(result):
    import json
    for block in result.content:
        if block.type == "text":
            try:
                return json.loads(block.text)
            except ValueError:
                return block.text
    return result.structured_content


async def main() -> int:
    url = sys.argv[1]
    quick = "--quick" in sys.argv
    failures = []
    async with streamable_http_client(url) as (read, write, *_):
        async with ClientSession(read, write) as s:
            info = await s.initialize()
            print(f"initialize OK: {info.server_info.name} proto={info.protocol_version}")
            assert info.server_info.name == "cosnautas"
            assert info.instructions and "cosnautas_search" in info.instructions, "faltan instructions"

            tools = {t.name: t for t in (await s.list_tools()).tools}
            assert set(tools) == {"cosnautas_search", "cosnautas_get_entry", "cosnautas_logout"}
            assert tools["cosnautas_search"].annotations.read_only_hint is True
            assert tools["cosnautas_get_entry"].annotations.read_only_hint is True
            lo = tools["cosnautas_logout"].annotations
            assert lo is None or lo.read_only_hint is not True
            print("tools/list OK: anotaciones correctas (search/get_entry readOnly, logout no)")

            searches = SEARCHES[:1] if quick else SEARCHES
            first_ref = None
            for resource, term in searches:
                r = await s.call_tool("cosnautas_search", {"term": term, "resource": resource})
                d = data(r)
                n = len(d["results"]) if isinstance(d, dict) else -1
                status = "ERROR: " + str(d)[:200] if r.is_error else f"{n} resultados"
                print(f"search {resource:12s} {term!r:12s} -> {status}")
                if r.is_error or n <= 0:
                    failures.append(f"search {resource}/{term}: {status}")
                elif resource == "libro" and first_ref is None:
                    first_ref = d["results"][0]["ref"]

            if not quick:
                r = await s.call_tool("cosnautas_search", {"term": "zzzzxqwy", "resource": "libro"})
                d = data(r)
                ok = not r.is_error and d["results"] == []
                print(f"search sin resultados -> {'OK (lista vacía)' if ok else 'FALLO: ' + str(d)[:200]}")
                if not ok:
                    failures.append("caso sin resultados")

                if first_ref:
                    r = await s.call_tool("cosnautas_get_entry", {"ref": first_ref})
                    d = data(r)
                    ok = not r.is_error and isinstance(d, dict) and d.get("lemma") and len(d.get("text", "")) > 100
                    print(f"get_entry {first_ref} -> lemma={d.get('lemma') if isinstance(d, dict) else '?'} "
                          f"({len(d.get('text', '')) if isinstance(d, dict) else 0} chars) {'OK' if ok else 'FALLO'}")
                    if not ok:
                        failures.append(f"get_entry: {str(d)[:200]}")

            r = await s.call_tool("cosnautas_logout", {})
            d = data(r)
            print(f"logout -> {d}")
            if r.is_error or "cerrada" not in str(d):
                failures.append(f"logout: {d}")

    if failures:
        print("\nFALLOS:\n- " + "\n- ".join(failures))
        return 1
    print("\nVERIFICACIÓN COMPLETA: todo OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
