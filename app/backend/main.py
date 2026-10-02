from fastapi import FastAPI


app = FastAPI(
    title="ORION",
    description="Portable, private, cross-platform AI assistant",
    version="0.1.0",
)


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
