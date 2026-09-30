from pydantic import BaseModel


class PackageHealth(BaseModel):
    """Estado de un paquete público del core tras importarlo."""

    module: str
    ok: bool
    detail: str


class CoreHealth(BaseModel):
    """Veredicto LLM del smoke: un módulo por paquete, más el veredicto global."""

    version: str
    packages_ok: bool
    verdict: str
    notes: list[str]
