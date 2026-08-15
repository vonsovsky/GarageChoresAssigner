"""HTML page routes (server-rendered with Jinja2)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/")
async def join(request: Request):
    return templates.TemplateResponse(request, "join.html", {"active": "join"})


@router.get("/feed")
async def feed(request: Request):
    return templates.TemplateResponse(request, "feed.html", {"active": "feed"})


@router.get("/profile")
async def profile(request: Request):
    return templates.TemplateResponse(request, "profile.html", {"active": "profile"})


@router.get("/manage")
async def manage(request: Request):
    return templates.TemplateResponse(request, "manage.html", {"active": "manage"})


@router.get("/dashboard")
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {"active": "dashboard"})


@router.get("/chores/{task_id}")
async def chore_detail(request: Request, task_id: int):
    return templates.TemplateResponse(
        request, "chore.html", {"active": "feed", "task_id": task_id}
    )
