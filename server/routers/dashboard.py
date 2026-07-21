from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["dashboard"])

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

# Content-Security-Policy for the dashboard pages: every asset must come from
# this origin — a remote script/style origin (the CDN supply-chain path from
# the 2026-07-21 audit) is blocked by policy even if a tag sneaks back into a
# template. 'unsafe-inline'/'unsafe-eval' stay: the pages use inline <script>
# blocks and Alpine's expression evaluator; the audit's target was the REMOTE
# origin vector, and CSP now pins us to self-hosted, pinned, committed assets.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)

_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse((TEMPLATES / "dashboard.html").read_text(), headers=_HEADERS)


@router.get("/bridge", response_class=HTMLResponse)
async def bridge():
    return HTMLResponse((TEMPLATES / "bridge.html").read_text(), headers=_HEADERS)
