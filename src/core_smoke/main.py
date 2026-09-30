from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

import yaml
from cache_ratelimit import CachedProvider, RateLimiter, RealClock, ThrottledProvider
from llm_client import CompletionRequest, CompletionResult, LlmClient, ReplayProvider, Span
from llm_client.providers.opencode_cli import OpenCodeCLI
from schema_validate import SchemaRegistry
from secure_base import sanitize_for_prompt
from test_kit import EvalCase, EvalDataset, run

from .models import CoreHealth, PackageHealth

DEFAULT_MODEL = "opencode/big-pickle"
SCHEMA_ID = "core-health-v1"
PROMPT_ID = "core-health-generator"
DIST = "llm-dev-core"
ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = ROOT / "prompts"
DEFAULT_PROMPT = PROMPT_DIR / "core-health-generator.md"
DEFAULT_DATASET = ROOT / "evals" / "core-health.jsonl"
TTL_DAILY = 86400.0

# Superficie pública de la dist (D1): los 9 paquetes top-level que un consumidor puede usar.
PUBLIC_MODULES = (
    "llm_client",
    "schema_validate",
    "secure_base",
    "test_kit",
    "web_api_base",
    "ci_pack",
    "cache_ratelimit",
    "bot_base",
    "parser_io",
)
EXPECTED_MAJOR = "1"


def load_prompt(path: Path) -> tuple[str, str, str, Path]:
    text = path.read_text()
    if not text.startswith("---"):
        raise SystemExit(f"{path}: falta frontmatter")
    _, frontmatter, body = text.split("---", 2)
    data = yaml.safe_load(frontmatter)
    eval_path = ROOT / data["eval"] if not Path(data["eval"]).is_absolute() else Path(data["eval"])
    return data["id"], data["version"], body.strip(), eval_path


def render_core_smoke(prompt_id: str, prompt_version: str, variables: dict) -> list[dict]:
    """Templado para `core-health-generator`; el inventario de paquetes entra sanitizado
    con `secure-base` antes de cruzar al proveedor."""
    if prompt_id == PROMPT_ID:
        _, _, body, _ = load_prompt(DEFAULT_PROMPT)
        system_part = body.split("## Sistema\n", 1)[1].split("## Usuario\n", 1)[0].strip()
        user_template = body.split("## Usuario\n", 1)[1].strip()
        user_raw = user_template.format(**variables)
        return [
            {"role": "system", "content": system_part},
            {"role": "user", "content": sanitize_for_prompt(user_raw).text},
        ]
    payload = {"prompt_id": prompt_id, "prompt_version": prompt_version, "variables": variables}
    return [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def build_emitter(span_file: Path | None):
    def emit(span: Span, _result) -> None:
        if span_file is not None:
            with span_file.open("a") as handle:
                handle.write(span.as_jsonl() + "\n")
        else:
            sys.stderr.write(span.as_jsonl() + "\n")

    return emit


def build_client(inner_provider, *, clock=None, span_file=None) -> dict:
    clock = RealClock() if clock is None else clock
    cached = CachedProvider(inner_provider, ttl_seconds=TTL_DAILY, clock=clock)
    limiter = RateLimiter(rpm=120, burst=20, clock=clock)
    provider = ThrottledProvider(cached, limiter)
    registry = SchemaRegistry()
    registry.register(SCHEMA_ID, CoreHealth)
    client = LlmClient(
        provider,
        consumer_repo="core-smoke",
        model_aliases={"fast": DEFAULT_MODEL},
        renderer=render_core_smoke,
        validator=registry.make_validator(SCHEMA_ID),
        emitter=build_emitter(span_file),
    )
    return {"client": client, "cached": cached, "limiter": limiter}


def import_packages() -> list[PackageHealth]:
    """La comprobación real: importar cada paquete público de la distribución instalada."""
    results: list[PackageHealth] = []
    for module in PUBLIC_MODULES:
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001
            results.append(PackageHealth(module=module, ok=False, detail=f"{type(exc).__name__}: {exc}"))
        else:
            results.append(PackageHealth(module=module, ok=True, detail="importado"))
    return results


def installed_version() -> str:
    try:
        return metadata.version(DIST)
    except metadata.PackageNotFoundError:
        return "(no instalado)"


def version_ok(version: str) -> bool:
    return version.split(".", 1)[0] == EXPECTED_MAJOR


def inventory_panel(packages: list[PackageHealth], version: str) -> str:
    lines = [f"- dist {DIST}=={version} · {sum(p.ok for p in packages)}/{len(packages)} paquetes importan"]
    lines += [f"- {p.module}: {'ok' if p.ok else 'FALLA'} ({p.detail})" for p in packages]
    return "\n".join(lines)


def health_request(packages: list[PackageHealth], version: str, *, today: str) -> CompletionRequest:
    return CompletionRequest(
        prompt_id=PROMPT_ID,
        prompt_version="0.1.0",
        variables={
            "date": today,
            "dist": DIST,
            "version": version,
            "modulos": str(len(packages)),
            "inventario": inventory_panel(packages, version),
        },
        model_alias="fast",
        response_schema=SCHEMA_ID,
        tags=["core-smoke", "week-13"],
    )


def run_check(*, client: LlmClient, today: str) -> tuple[int, CoreHealth | None]:
    """Importa los paquetes públicos; el LLM solo pone el veredicto narrativo, que se
    muestra únicamente si `schema-validate` lo aprueba. El exit code lo decide el import."""
    packages = import_packages()
    version = installed_version()
    packages_ok = all(p.ok for p in packages) and version_ok(version)
    for p in packages:
        mark = "ok " if p.ok else "FAIL"
        print(f"  {mark} {p.module:16} {p.detail}")
    print(f"dist {DIST}=={version} · mayor esperado {EXPECTED_MAJOR}.x · "
          f"{'ok' if version_ok(version) else 'REVISAR'}")

    result = client.complete(health_request(packages, version, today=today))
    if not result.validation.ok:
        raise SystemExit("; ".join(result.validation.errors))
    verdict = result.parsed
    print(f"veredicto (core-health-v1): {verdict.verdict}")
    for note in verdict.notes:
        print(f"  - {note}")

    if not packages_ok:
        print("smoke: FALLA — la distribución publicada no sirve el contrato público")
        return 1, verdict
    print("smoke: OK — los paquetes públicos del core publicado cargan")
    return 0, verdict


def judge_for(client: LlmClient) -> Callable[[EvalCase], CompletionResult]:
    def judge(case: EvalCase) -> CompletionResult:
        return client.complete(
            CompletionRequest(
                prompt_id=case.prompt_id,
                prompt_version=case.prompt_version,
                variables=case.input,
                model_alias="fast",
                response_schema=SCHEMA_ID,
                tags=["core-smoke", "week-13"],
            )
        )

    return judge


def run_replay_check() -> int:
    dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
    provider = ReplayProvider(ROOT / "cassettes", record=False)
    client = build_client(provider)["client"]
    report = run(dataset, judge_for(client), mode="full", threshold=1.0)
    print(f"replay: pass={report.passed}/{report.total} threshold_ok={report.threshold_ok}")
    return 0 if report.threshold_ok else 1


def run_record() -> int:
    dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
    provider = ReplayProvider(ROOT / "cassettes", record=True, inner=OpenCodeCLI(DEFAULT_MODEL))
    client = build_client(provider)["client"]
    report = run(dataset, judge_for(client), mode="full", threshold=1.0)
    print(f"grabadas {len(dataset.cases)} respuestas; pass={report.passed}/{report.total}")
    return 0 if report.threshold_ok else 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="core-smoke: smoke de publicación de llm-dev-core v1.0.0 (semana 13)."
    )
    parser.add_argument(
        "--date",
        default=None,
        help="fecha para el prompt (solo determinismo de la demo)",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="check",
        choices=["check", "replay-check", "record"],
        help="check importa los paquetes públicos; replay-check/record usan la cinta (D4).",
    )
    args = parser.parse_args(argv)

    if args.command == "replay-check":
        raise SystemExit(run_replay_check())
    if args.command == "record":
        raise SystemExit(run_record())

    from datetime import date

    today = args.date or date.today().isoformat()
    client = build_client(OpenCodeCLI(DEFAULT_MODEL))["client"]
    code, _ = run_check(client=client, today=today)
    raise SystemExit(code)
