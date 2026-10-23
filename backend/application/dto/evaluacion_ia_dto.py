from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FichaTecnicaIA(BaseModel):
    """Datos minimos para solicitar IA; no certifica viabilidad profesional."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    nombre: str = Field(min_length=1, max_length=200)
    descripcion: str = Field(min_length=1, max_length=4000)
    ubicacion: str = Field(min_length=1, max_length=300)
    tipo_proyecto: str = Field(min_length=1, max_length=150)
    presupuesto: float = Field(gt=0, allow_inf_nan=False)
    beneficiarios: int = Field(gt=0, strict=True)


class DictamenIAResponse(BaseModel):
    """Contrato publico y esquema estructurado para la evaluacion IA."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        from_attributes=True,
    )

    puntaje: float = Field(ge=0, le=100, allow_inf_nan=False)
    viabilidad: Literal["ALTA", "MEDIA", "BAJA"]
    justificacion: str = Field(min_length=10)
    observaciones: list[str]
    recomendaciones: list[str]

    @field_validator("observaciones", "recomendaciones")
    @classmethod
    def reject_blank_items(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("Las entradas del dictamen no pueden estar vacias")
        return [value.strip() for value in values]

    @model_validator(mode="after")
    def validate_score_band(self) -> "DictamenIAResponse":
        expected = "ALTA" if self.puntaje >= 70 else "MEDIA" if self.puntaje >= 40 else "BAJA"
        if self.viabilidad != expected:
            raise ValueError("El puntaje y la viabilidad no son coherentes")
        return self
