from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.backend.auth.routes import router as auth_router
from app.backend.conversations.routes import router as conversations_router
from app.backend.messages.routes import router as messages_router
from app.frontend.routes import router as frontend_router


BASE_DIR = Path(__file__).resolve().parents[2]
FRONTEND_DIR = BASE_DIR / "frontend"


app = FastAPI(
    title="ORION",
    description="Portable, private, cross-platform AI assistant",
    version="0.1.0",
)


app.mount(
    "/static",
    StaticFiles(directory=str(FRONTEND_DIR / "static")),
    name="static",
)


app.include_router(auth_router)
app.include_router(conversations_router)
app.include_router(messages_router)
app.include_router(frontend_router)


@app.get("/")
async def root():
    return {
        "name": "ORION",
        "status": "online",
        "version": "0.1.0",
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}
