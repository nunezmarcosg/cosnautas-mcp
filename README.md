# cosnautas-mcp

Servidor MCP remoto (streamable HTTP) para consultar los diccionarios médicos de
[Cosnautas](https://www.cosnautas.com) con una suscripción personal, pensado para usarse
como conector personalizado de ChatGPT u otro cliente MCP.

- Uso personal: una consulta bajo demanda cada vez, sin crawling ni caché.
- La suscripción de Cosnautas admite **una** conexión simultánea; el servidor serializa
  las peticiones, cierra sesión tras un periodo de inactividad y lo documenta en sus errores.
- El endpoint MCP se sirve en una ruta secreta (`/mcp-<32 hex>`); la ruta funciona como
  credencial y el access log está desactivado por ese motivo.

## Configuración (variables de entorno)

| Variable | Descripción |
|---|---|
| `COSNAUTAS_USER` / `COSNAUTAS_PASSWORD` | Credenciales de la suscripción (obligatorias) |
| `MCP_PATH_SECRET` | 32 caracteres hex (`openssl rand -hex 16`); obligatoria |
| `COSNAUTAS_LANG` | `es` (por defecto) o `en` |
| `COSNAUTAS_IDLE_LOGOUT` | Segundos de inactividad antes de liberar la sesión (300) |
| `MCP_ALLOWED_HOSTS` | Valores permitidos de la cabecera `Host`, separados por comas |
| `PORT` | Puerto de escucha (8787) |

## Desarrollo

```bash
uv venv && uv pip install -e . --group dev
.venv/bin/pytest                          # tests offline (fixtures, sin red)
python tests/e2e_client.py <URL> [--quick]  # verificación real contra Cosnautas
```

## Despliegue

`docker build .` y servir detrás de un proxy HTTPS. Un solo proceso/worker: no escalar réplicas.
