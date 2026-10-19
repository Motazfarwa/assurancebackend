from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class ClientInput(BaseModel):
    """Fields used by the Gestion des clients form; names match dbo.TClient."""

    model_config = ConfigDict(extra="forbid")

    code_client: str = Field(min_length=1, max_length=20)
    date_creation: datetime | None = None
    telephones_domicile: list[dict[str, str]] | None = None
    emails_domicile: list[dict[str, str]] | None = None
    telephones_bureau: list[dict[str, str]] | None = None
    emails_bureau: list[dict[str, str]] | None = None
    nom_client: str | None = None
    profession: str | None = None
    adresse: str | None = None
    ville: str | None = None
    cp: int | None = None
    tel: str | None = None
    fax: str | None = None
    adresse_b: str | None = None
    ville_b: str | None = None
    cp_b: int | None = None
    tel_b: str | None = None
    tel_b2: str | None = None
    fax_b: str | None = None
    adresse_email: str | None = None
    Correspondant: str | None = None
    Nom: str | None = None
    Prenom: str | None = None
    DateNaissance: date | None = None
    Exonerer: str | None = None
    NumeroCIN: str | None = None
    LieuCIN: str | None = None
    DateCIN: date | None = None
    Pays: str | None = None
    Nationnalite: str | None = None
    LieuNaissance: str | None = None
    Sit_Fam: str | None = None
    Titre: str | None = None
    NumeroPermis: str | None = None
    LieuPermis: str | None = None
    DatePermis: date | None = None
    Type_Client: str | None = None
    observ: str | None = None
    code_locuteur: str | None = None
    nom_locuteur: str | None = None
    code_agence: str | None = None
    code_com: int | None = None
    contact: str | None = None
    code: int | None = None
    tel_contact: str | None = None
    mobile: str | None = None
