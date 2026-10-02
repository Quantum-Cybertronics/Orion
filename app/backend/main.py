from fastapi import FastAPI

from app.backend.auth.routes import router as auth_router

from app.backend.conversations.routes import router as conversations_router

from app.backend.messages.routes import router as messages_router




app = FastAPI(
    title="ORION",
    description="Portable, private, cross-platform AI assistant",
    version="0.1.0",
)

app.include_router(auth_router)
app.include_router(conversations_router)
app.include_router(messages_router)



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
