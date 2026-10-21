from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.auth import require_current_user
from app.routers.auth import router as auth_router
from app.routers.admin_users import router as admin_users_router
from app.routers.sinistres import router as sinistres_router
from app.routers.clients import router as clients_router
from app.routers.client_records import router as client_records_router
from app.routers.prestataires import router as prestataires_router
from app.routers.companies import router as companies_router
from app.routers.documents import router as documents_router
from app.routers.impayes import router as impayes_router
from app.routers.record_updates import router as record_updates_router
from app.routers import polices
from app.routers.assistant import router as assistant_router

app = FastAPI(title="Assurance API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://assurancefrontend.onrender.com",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(sinistres_router, dependencies=[Depends(require_current_user)])
app.include_router(clients_router)
app.include_router(client_records_router)
app.include_router(auth_router)
app.include_router(admin_users_router)
app.include_router(prestataires_router)
app.include_router(companies_router)
app.include_router(documents_router)
app.include_router(impayes_router)
app.include_router(polices.router)
app.include_router(assistant_router)
app.include_router(record_updates_router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/health", tags=["Health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
