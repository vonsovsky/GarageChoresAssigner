"""Discord OAuth login + sessions (Phase 1).

Mirrors garage-trip-digitalizace-carek: OAuth with scope `identify`, gate on the
trip's paid role, flag admins. The signed session carries the identity; we also
set the legacy `uid` cookie and upsert a local profile so the rest of the app
keeps working unchanged during the migration.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import store
from .config import settings
from .discord import fetch_member_roles

log = logging.getLogger("auth")

router = APIRouter()

oauth = OAuth()
if settings.oauth_configured:
    oauth.register(
        name="discord",
        client_id=settings.DISCORD_CLIENT_ID,
        client_secret=settings.DISCORD_CLIENT_SECRET,
        access_token_url="https://discord.com/api/oauth2/token",
        authorize_url="https://discord.com/oauth2/authorize",
        api_base_url="https://discord.com/api/",
        client_kwargs={
            "scope": "identify",
            "token_endpoint_auth_method": "client_secret_post",
        },
    )

COOKIE = "uid"  # legacy identity cookie the existing endpoints still read


# --- session helpers --------------------------------------------------------

def current_user(request: Request) -> Optional[dict[str, Any]]:
    """The logged-in Discord user, or None."""
    return request.session.get("user")


def current_uid(request: Request) -> Optional[str]:
    """The logged-in user's discord_id, or None (tablet sessions have none)."""
    user = request.session.get("user")
    return user.get("discord_id") if user else None


def require_uid(request: Request) -> str:
    """FastAPI dependency: the current user's discord_id, or 401."""
    uid = current_uid(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Login required")
    return uid


def is_tablet(request: Request) -> bool:
    return bool(request.session.get("tablet"))


def _discord_display(profile: dict[str, Any]) -> str:
    return profile.get("global_name") or profile.get("username") or profile.get("id")


def _avatar_url(profile: dict[str, Any]) -> Optional[str]:
    av, uid = profile.get("avatar"), profile.get("id")
    return f"https://cdn.discordapp.com/avatars/{uid}/{av}.png" if av and uid else None


def _login_user(request: Request, response, discord_id: str, name: str,
                handle: str, avatar_url: Optional[str], is_admin: bool) -> None:
    """Store identity in the session + legacy cookie, and keep a local profile
    cache (without clobbering the person's saved skills/capacity)."""
    request.session["user"] = {
        "discord_id": discord_id, "name": name,
        "avatar_url": avatar_url, "is_admin": is_admin,
    }
    request.session["discord_id"] = discord_id
    response.set_cookie(COOKIE, discord_id, max_age=60 * 60 * 24 * 14, samesite="lax")

    existing = store.get_profile(discord_id)
    store.upsert_profile(
        discord_id=discord_id,
        name=name,
        discord_handle=handle,
        skills=(existing or {}).get("skills", []),
        max_capacity_min=(existing or {}).get("max_capacity_min", 240),
    )


# --- routes -----------------------------------------------------------------

@router.get("/auth/discord")
async def discord_login(request: Request):
    if not settings.oauth_configured:
        return HTMLResponse(
            "<h1>Discord login isn't configured yet</h1>"
            "<p>Set DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET in the environment.</p>",
            status_code=503,
        )
    return await oauth.discord.authorize_redirect(request, settings.DISCORD_CALLBACK_URL)


@router.get("/auth/discord/callback")
async def discord_callback(request: Request):
    if not settings.oauth_configured:
        return RedirectResponse("/", status_code=302)
    try:
        token = await oauth.discord.authorize_access_token(request)
        resp = await oauth.discord.get("users/@me", token=token)
        profile = resp.json()
    except Exception as exc:  # noqa: BLE001
        # A failed token exchange is a *login* error, not a role problem — keep
        # them distinct so the message reflects the real cause.
        log.exception("discord OAuth token exchange failed")
        return HTMLResponse(
            "<h1>Login failed</h1>"
            "<p>Couldn't complete Discord login.</p>"
            f"<p style='color:#888'>Detail: {type(exc).__name__}: {exc}</p>"
            '<p><a href="/">Try again</a></p>',
            status_code=400,
        )

    discord_id = str(profile.get("id"))
    roles = await fetch_member_roles(discord_id)
    # Gate on the paid role only when role gating is actually configured.
    if settings.role_gating_configured and settings.DISCORD_PAID_ROLE and not roles.get("has_paid"):
        log.info("login denied for %s — no paid role (found=%s)", discord_id, roles.get("found"))
        return RedirectResponse("/unauthorized", status_code=302)

    redirect = RedirectResponse("/feed", status_code=302)
    _login_user(
        request, redirect,
        discord_id=discord_id,
        name=_discord_display(profile),
        handle=profile.get("username") or discord_id,
        avatar_url=_avatar_url(profile),
        is_admin=bool(roles.get("has_admin")),
    )
    return redirect


@router.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(COOKIE)
    return resp


@router.post("/auth/tablet")
async def tablet_login(request: Request, password: str = Form(...)):
    """Shared-password login so the TV/tablet can open the dashboard."""
    if not settings.TABLET_PASSWORD or password != settings.TABLET_PASSWORD:
        return RedirectResponse("/?tablet_error=1", status_code=302)
    request.session["tablet"] = True
    return RedirectResponse("/dashboard", status_code=302)


@router.get("/unauthorized")
async def unauthorized():
    return HTMLResponse(
        "<h1>Not on the list</h1>"
        "<p>Your Discord account isn't a paid attendee of this trip, so you "
        "can't log in. If that's wrong, ping an organizer.</p>"
        '<p><a href="/">Back</a></p>',
        status_code=403,
    )
