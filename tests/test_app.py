from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from core_smoke.main import (
    DEFAULT_DATASET,
    DEFAULT_PROMPT,
    EXPECTED_MAJOR,
    PUBLIC_MODULES,
    build_client,
    health_request,
    import_packages,
    inventory_panel,
    installed_version,
    judge_for,
    load_prompt,
    run_check,
    version_ok,
)
from core_smoke.models import PackageHealth
from llm_client.provider import ProviderRequest, ProviderResponse
from test_kit import EvalDataset, run as run_evalset

STUB_TEXT = json.dumps(
    {
        "version": "1.0.0",
        "packages_ok": True,
        "verdict": "el release publicado sirve su contrato público",
        "notes": ["9/9 paquetes importan desde el wheel"],
    }
)


class StubProvider:
    name = "stub"

    def __init__(self, text: str | None = None) -> None:
        self.text = text or STUB_TEXT
        self.calls = 0
        self.last_request: ProviderRequest | None = None

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        self.last_request = request
        return ProviderResponse(text=self.text, model=request.model)

    def stream(self, request: ProviderRequest) -> list[str]:
        self.calls += 1
        return [self.text]


class FakeClock:
    def __init__(self, tick: float = 0.0) -> None:
        self._tick = tick

    def monotonic(self) -> float:
        return self._tick

    def sleep(self, seconds: float) -> None:
        self._tick += seconds

    def now(self) -> datetime:
        return datetime(2026, 10, 8, 9, 0, 0)


def _client(provider: StubProvider | None = None) -> tuple[StubProvider, object]:
    provider = provider or StubProvider()
    return provider, build_client(provider, clock=FakeClock())["client"]


def test_smoke_importa_los_nueve_publicos() -> None:
    packages = import_packages()
    assert [p.module for p in packages] == list(PUBLIC_MODULES)
    assert all(p.ok for p in packages), [p.model_dump() for p in packages if not p.ok]


def test_version_instalada_es_1x() -> None:
    version = installed_version()
    assert version != "(no instalado)", "llm-dev-core no está instalado en este entorno"
    assert version_ok(version), version


def test_core_viene_de_la_dist_publicada_no_de_un_checkout() -> None:
    import schema_validate

    location = Path(schema_validate.__file__).resolve()
    assert "site-packages" in location.parts, location


def test_manifiesto_pide_la_dist_publicada_y_no_un_path_local() -> None:
    import tomllib

    data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    deps = data["project"]["dependencies"]
    assert any(d.startswith("llm-dev-core>=1.0.0") for d in deps), deps
    uv_sources = data.get("tool", {}).get("uv", {}).get("sources")
    assert not uv_sources, f"core-smoke no debe apuntar a un checkout local: {uv_sources}"


def test_version_ok_rechaza_otras_series() -> None:
    assert version_ok("1.0.0")
    assert version_ok("1.4.2")
    assert not version_ok("0.10.0")
    assert not version_ok("2.0.0")
    assert not version_ok("(no instalado)")
    assert EXPECTED_MAJOR == "1"


def test_check_verde_salida_cero() -> None:
    _, client = _client()
    code, verdict = run_check(client=client, today="2026-10-08")
    assert code == 0
    assert verdict is not None and verdict.packages_ok is True


def test_check_falla_si_hay_un_import_roto(monkeypatch: pytest.MonkeyPatch) -> None:
    packages = import_packages()
    roto = [
        *packages[:-1],
        PackageHealth(module=packages[-1].module, ok=False, detail="ModuleNotFoundError: sin wheel"),
    ]
    monkeypatch.setattr("core_smoke.main.import_packages", lambda: roto)
    _, client = _client()
    code, verdict = run_check(client=client, today="2026-10-08")
    assert code == 1, "un paquete roto debe dar exit 1 aunque el LLM diga que todo bien"
    assert verdict is not None  # el veredicto narrativo se sigue validando y mostrando


def test_check_falla_si_la_version_no_es_1x(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core_smoke.main.installed_version", lambda: "0.10.0")
    _, client = _client()
    code, _ = run_check(client=client, today="2026-10-08")
    assert code == 1


def test_inventario_panel_marca_los_fallos() -> None:
    packages = import_packages()
    roto = [*packages[:-1], PackageHealth(module="parser_io", ok=False, detail="ModuleNotFoundError: sin wheel")]
    panel = inventory_panel(roto, "1.0.0")
    assert "- dist llm-dev-core==1.0.0 · 8/9 paquetes importan" in panel
    assert "parser_io: FALLA (ModuleNotFoundError: sin wheel)" in panel


def test_prompt_llega_sanitizado_al_proveedor() -> None:
    provider, client = _client()
    run_check(client=client, today="2026-10-08")
    assert provider.calls == 1
    messages = provider.last_request.messages
    assert "Fecha: 2026-10-08" in messages[-1]["content"]
    assert "llm-dev-core==1.0.0" in messages[-1]["content"]
    assert "9/9 paquetes importan" in messages[-1]["content"]


def test_salida_invalida_aborta_con_error_de_schema() -> None:
    _, client = _client(StubProvider(text="no soy json"))
    with pytest.raises(SystemExit):
        run_check(client=client, today="2026-10-08")


def test_eval_dataset_schema_match_en_verde() -> None:
    dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
    prompt_id, prompt_version, _, _ = load_prompt(DEFAULT_PROMPT)
    assert dataset.prompt_id == prompt_id and dataset.prompt_version == prompt_version
    _, client = _client()
    report = run_evalset(dataset, judge_for(client), mode="full", threshold=1.0)
    assert report.threshold_ok, report.model_dump()
    assert report.total == 1


def test_health_request_tags_y_schema() -> None:
    request = health_request(import_packages(), installed_version(), today="2026-10-08")
    assert request.response_schema == "core-health-v1"
    assert request.prompt_id == "core-health-generator"
    assert request.tags == ["core-smoke", "week-13"]
    assert request.variables["modulos"] == "9"
    assert request.variables["dist"] == "llm-dev-core"
