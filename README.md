# core-smoke

Smoke de publicación de `llm-dev-core` **v1.0.0** (semana 13). Instala el core desde
**PyPI** —no desde el checkout local— importa los nueve paquetes públicos de la
distribución y falla con `exit 1` si alguno no carga o si la versión no es `1.x`. El LLM
solo redacta el veredicto narrativo (`core-health-v1`), y se muestra únicamente si
`schema-validate` lo aprueba. No es un test más del árbol: es la comprobación de que lo
que se publicó sirve su contrato.

## Demo

```bash
uv sync                      # instala llm-dev-core==1.0.0 desde PyPI (no hay path editable)
uv run core-smoke check
#   ok  llm_client       importado
#   ok  schema_validate  importado
#   ok  secure_base      importado
#   ok  test_kit         importado
#   ok  web_api_base     importado
#   ok  ci_pack          importado
#   ok  cache_ratelimit  importado
#   ok  bot_base         importado
#   ok  parser_io        importado
# dist llm-dev-core==1.0.0 · mayor esperado 1.x · ok
# veredicto (core-health-v1): El release 1.0.0 cumple su contrato: los 9 paquetes
#   públicos importan correctamente desde la distribución publicada.
# smoke: OK — los paquetes públicos del core publicado cargan
```

Ese bloque es una corrida real: `uv.lock` fija `llm-dev-core` a la versión publicada y
`tests/test_app.py::test_core_viene_de_la_dist_publicada_no_de_un_checkout` falla si
alguien cambia el manifiesto a un path local (entonces ya no estaríamos probando la
publicación). Para ver solo la cinta, sin provider (D4):

```bash
uv run core-smoke replay-check   # dataset congelado contra el cassette 1/1
```

## Cómo funciona

```
core-smoke check
  ├─ import_packages()          # el gate real: import de los 9 paquetes públicos
  ├─ installed_version()        # importlib.metadata sobre la dist instalada
  ├─ exit code                  # 1 si algún import falla o la versión no es 1.x
  └─ client.complete(core-health-generator)  # veredicto narrativo, sanitizado
                                              # y validado contra core-health-v1
```

- El **`exit 1` lo decide el import, no el LLM**: un veredicto narrativo optimistic con
  un paquete roto sigue siendo `exit 1` (hay un test que lo fija).
- Si el LLM devuelve JSON que no valida, el comando aborta con el error de
  `schema-validate` en vez de mostrar un veredicto inventado.
- El inventario que ve el modelo son datos medidos (`ok (importado)` o el error real del
  import), no una descripción; por eso `verdict` y `notes` no pueden alucinar un fallo.

## Recicla de

| Módulo | Uso |
|---|---|
| `llm-client` | la llamada del veredicto (retry + reparación + span de 20 campos) |
| `schema-validate` | el veredicto valida contra `core-health-v1` o el comando aborta |
| `test-kit` | el prompt nace evaluado: dataset congelado + replay determinista (D4) |
| `secure-base` | el inventario se redacta antes de cruzar al proveedor |
| `cache-ratelimit` | caché TTL diario + token bucket por delante del proveedor |
| `ci-pack` | lints del audit y `render` de la página de evidencia de la flota (§10.1) |

## Limitaciones

- Solo verifica **carga de módulos y versión**. No llama a la API real de cada módulo
  (`llm_client.complete`, `ci_pack.collect`, …): es un smoke de empaquetado, no una suite
  funcional. La suite funcional del core son sus propios 107 tests, y el compat check
  (§6.1) es lo que cubre a los consumidores.
- El CI no puede ejecutar `core-smoke check` porque el comando real llama al LLM; en CI
  corre el replay del dataset (`pytest tests/test_replay.py`) más la suite con stub.
- `EXPECTED_MAJOR = "1"`: cuando salga el v2.0 hay que subir esta constante y el pin del
  manifiesto.

## En la flota

- Manifest: [`core-consumer.yml`](core-consumer.yml) (semana 13, `llm-dev-core 1.0.0`).
- New surface ≈ 12 (composición 70/30, sin módulo nuevo: es la semana de consolidación
  D10, el v1.0 se publica sin obligar a los consumidores 2–12 a migrar de `^0.x`).
- Es el smoke que ejecuta el mismo paso que la verificación manual del release: importar
  los nueve paquetes públicos desde el wheel en un venv 3.12 limpio.
