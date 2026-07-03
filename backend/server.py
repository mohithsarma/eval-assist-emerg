from fastapi import FastAPI, APIRouter
from fastapi.responses import FileResponse
from starlette.middleware.cors import CORSMiddleware
import os
import logging
from backend.core.config import settings
from backend.routers import auth, assessments, questions, students, evaluations, insights, interventions

# Create the main app without a prefix
app = FastAPI()

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=settings.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Router
api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(assessments.router, prefix="/assessments", tags=["assessments"])
api_router.include_router(questions.router, prefix="/assessments", tags=["questions"])
api_router.include_router(students.router, prefix="/assessments", tags=["students"])
api_router.include_router(evaluations.router, prefix="/assessments", tags=["evaluations"])
api_router.include_router(insights.router, prefix="/assessments", tags=["insights"])
api_router.include_router(interventions.router, prefix="/assessments", tags=["interventions"])

app.include_router(api_router)

# Serve media files (sample answer sheets, etc.)
from fastapi.staticfiles import StaticFiles
media_dir = os.path.join(os.path.dirname(__file__), "..", "media")
if os.path.exists(media_dir):
    app.mount("/media", StaticFiles(directory=media_dir), name="media")

# Serve frontend static assets (JS, CSS, images)
frontend_build = os.path.join(os.path.dirname(__file__), "..", "frontend", "build")
frontend_static = os.path.join(frontend_build, "static")
if os.path.exists(frontend_static):
    app.mount("/static", StaticFiles(directory=frontend_static), name="static")

# SPA catch-all — all unmatched routes return index.html
_index_html = os.path.join(frontend_build, "index.html")

@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    if os.path.exists(_index_html):
        return FileResponse(_index_html)
    return {"error": "Frontend not built. Run: cd frontend && npm run build"}

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.server:app", host="0.0.0.0", port=8000, reload=False)
