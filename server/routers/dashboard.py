from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["dashboard"])

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse((TEMPLATES / "dashboard.html").read_text())


@router.get("/bridge", response_class=HTMLResponse)
async def bridge():
    return HTMLResponse((TEMPLATES / "bridge.html").read_text())
