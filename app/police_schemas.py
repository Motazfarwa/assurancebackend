
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict


class PoliceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    num_police: Optional[str] = None

    code_compagnie: Optional[int] = None
    code_client: str = Field(min_length=1, max_length=8)
    code_branche: str = Field(pattern=r"^[0-9]{3}$")

    effet: Optional[datetime] = None
    heure_effet: Optional[datetime] = None
    expiration: Optional[datetime] = None
    echeance: Optional[str] = None

    duree: Optional[int] = None
    etat: Optional[str] = None
    date_mvt: Optional[datetime] = None
    date_creation: Optional[datetime] = None

    renouvelable: Optional[bool] = None
    participation: Optional[bool] = None
    tous_risque: Optional[bool] = None
    hors_taxe: Optional[bool] = None

    risque: Optional[str] = None
    num_imat: Optional[str] = None
    marque: Optional[str] = None
    serie: Optional[str] = None
    puissance: Optional[int] = None
    circulation: Optional[datetime] = None

    garantie: Optional[str] = None
    franchise: Optional[int] = None
    capital: Optional[float] = None
    obser: Optional[str] = None


class QuittanceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_saisie: Optional[datetime] = None
    date_emission: Optional[datetime] = None
    echeance: Optional[datetime] = None
    periode: Optional[str] = None

    prime_net: Optional[float] = None
    prime_total: Optional[float] = None
    commission: Optional[float] = None

    montant_quittance: Optional[float] = None
    solde_quittance: Optional[float] = None

    taxe: Optional[float] = None
    timbre: Optional[float] = None
    cout_attestation: Optional[float] = None
    transport: Optional[float] = None
    frais_quit: Optional[float] = None

    num_attestation: Optional[str] = None
    observation: str = ""
    type_quittance: Optional[str] = None


class GarantiesCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vol: Optional[float] = None
    incendie: Optional[float] = None
    bdg: Optional[float] = None
    personnes_transportees: Optional[float] = None
    ind_conducteur: Optional[float] = None
    remarquage: Optional[float] = None


class PoliceWithQuittanceCreate(BaseModel):
    police: PoliceCreate
    quittance: QuittanceCreate
    garanties: Optional[GarantiesCreate] = None
