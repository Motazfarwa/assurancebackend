from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PrestataireInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    Code_Prestataire: int = Field(gt=0)
    Nom_Prestataire: str = Field(min_length=1, max_length=60)
    Code_Famille: int
    Matricule_Fiscal: str | None = Field(default=None, max_length=30)
    Adresse_Prof: str | None = Field(default=None, max_length=50)
    Adresse_Residence: str | None = Field(default=None, max_length=50)
    Tel1: str | None = Field(default=None, max_length=30)
    Tel2: str | None = Field(default=None, max_length=30)
    Fax: str | None = Field(default=None, max_length=30)
    Code_Region: int | None = None
    Adresse_EMail: str | None = Field(default=None, max_length=100)
    Taux_RS: float | None = Field(default=None, ge=0)
    Observation: str | None = Field(default=None, max_length=100)
    Note: str | None = Field(default=None, max_length=50)
    Compagnies: list[int] = Field(min_length=1)
    Branches: list[int] = Field(min_length=1)


class PrestataireOut(PrestataireInput):
    Date_Heure_Systeme: datetime | None = None
