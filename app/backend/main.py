from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.backend.agents.manager import get_run_manager
from app.backend.agents.routes import router as runs_router
from app.backend.agents.service import fail_interrupted_runs
from app.backend.ai.routes import router as ai_router
from app.backend.ai.runtime import get_runtime
from app.backend.attachments.routes import router as attachments_router
from app.backend.auth.routes import router as auth_router
from app.backend.conversations.routes import router as conversations_router
from app.backend.database import engine
from app.backend.migrations_runner import run_migrations
from app.backend.messages.routes import router as messages_router
from app.backend.storage import compact_on_shutdown
from app.frontend.routes import router as frontend_router


BASE_DIR = Path(__file__).resolve().parents[2]
FRONTEND_DIR = BASE_DIR / "frontend"


@asynccontextmanager
async def lifespan(_: FastAPI):
    run_migrations()

    # Runs a previous ORION process never finished can no longer complete.
    fail_interrupted_runs(engine)

    runtime = get_runtime()
    runtime.start()  # loads the model in the background; does not block

    try:
        yield
    finally:
        get_run_manager().cancel_all()
        runtime.stop()
        compact_on_shutdown(engine)


app = FastAPI(
    lifespan=lifespan,
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
app.include_router(ai_router)
app.include_router(attachments_router)
app.include_router(conversations_router)
app.include_router(runs_router)
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
