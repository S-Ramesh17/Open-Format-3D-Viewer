from fastapi import FastAPI

app = FastAPI(
    title="Open Format 3D Viewer API",
    version="0.1.0",
)


@app.get("/health")
async def health():
    return {"status": "ok"}