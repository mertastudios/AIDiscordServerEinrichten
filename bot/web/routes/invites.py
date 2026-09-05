"""Server-Einladungen: auflisten, erstellen, löschen."""

from __future__ import annotations

from typing import Any, Dict, List


from ...serializers import serialize_invite
from ...util import ApiError, parse_bool, parse_int
from ..context import Ctx, guard
from ..registry import route


@route(
    "GET", "/api/v1/invites", scope="read", tags=("invites",),
    summary="Alle Invites des Servers",
    description="Zeigt Code, Kanal, Ersteller, Nutzungslimit und Ablauf — praktisch, "
                "um alte oder fremde Einladungen aufzuspüren.",
)
async def list_invites(ctx: Ctx) -> Dict[str, Any]:
    try:
        invites = await guard(ctx.guild.invites(), action="Invites laden")
    except ApiError as exc:
        if exc.code == "DISCORD_FORBIDDEN":
            raise ApiError.forbidden(
                "Invites nicht lesbar: dem Bot fehlt 'manage_guild'.",
                code="INVITES_FORBIDDEN",
            ) from exc
        raise
    vanity = getattr(ctx.guild, "vanity_url", None)
    return {
        "count": len(invites),
        "vanity_url": vanity,
        "invites_disabled": getattr(ctx.guild, "invites_disabled", None),
        "invites": [serialize_invite(i) for i in invites],
    }


@route(
    "GET", "/api/v1/invites/{code}", scope="read", tags=("invites",),
    summary="Details zu einem Invite",
    query={"with_counts": "true — Mitglieder-/Online-Zahl mitsenden"},
)
async def get_invite(ctx: Ctx) -> Dict[str, Any]:
    code = ctx.path("code") or ""
    with_counts = ctx.q_bool("with_counts", True)
    invite = await guard(
        ctx.client.fetch_invite(code, with_counts=bool(with_counts)), action=f"Invite {code} laden"
    )
    return serialize_invite(invite) or {}


@route(
    "POST", "/api/v1/channels/{channel_id}/invites", scope="write", tags=("invites",),
    summary="Invite für einen Kanal erstellen",
    body={
        "max_age": "Sekunden, 0 = unbegrenzt (Standard 0)",
        "max_uses": "Anzahl Nutzungen, 0 = unbegrenzt (Standard 0)",
        "temporary": "bool — Mitgliedschaft nur temporär",
        "unique": "bool, Standard true — erzwingt neuen Code",
        "reason": "str",
    },
)
async def create_invite(ctx: Ctx) -> Dict[str, Any]:
    channel = await ctx.channel()
    data = await ctx.body()
    if not hasattr(channel, "create_invite"):
        raise ApiError.bad_request(
            f"'{getattr(channel, 'name', channel.id)}' kann keine Invites erzeugen.",
            hint="Nutze einen Text-, Voice- oder Forum-Kanal.",
            code="CHANNEL_TYPE_MISMATCH",
        )
    max_age = parse_int(data.get("max_age"), field="max_age", default=0, minimum=0, maximum=604800)
    max_uses = parse_int(data.get("max_uses"), field="max_uses", default=0, minimum=0, maximum=100)
    invite = await guard(
        channel.create_invite(
            max_age=max_age or 0,
            max_uses=max_uses or 0,
            temporary=bool(parse_bool(data.get("temporary"), field="temporary", default=False)),
            unique=bool(parse_bool(data.get("unique"), field="unique", default=True)),
            reason=ctx.reason(data, default="Invite erstellt (Arena AI)"),
        ),
        action="Invite erstellen",
    )
    await ctx.settle(0.3)
    result = serialize_invite(invite) or {}
    result["share_url"] = invite.url
    return result


@route(
    "DELETE", "/api/v1/invites/{code}", scope="manage", tags=("invites",),
    summary="Invite löschen/ungültig machen",
    query={"reason": "str"},
)
async def delete_invite(ctx: Ctx) -> Dict[str, Any]:
    code = ctx.path("code") or ""
    invite = await guard(ctx.client.fetch_invite(code), action=f"Invite {code} laden")
    await guard(invite.delete(reason=ctx.reason(default=f"Invite {code} gelöscht (Arena AI)")),
                action=f"Invite {code} löschen")
    return {"deleted": code}


@route(
    "POST", "/api/v1/invites/cleanup", scope="danger", tags=("invites",),
    summary="Alle/ungebrauchte Invites löschen",
    body={
        "only_unused": "bool, Standard true — nur Invites mit 0 Nutzungen",
        "keep_codes": '["Code", …] — diese bleiben erhalten',
        "confirm": "true — Pflicht",
        "reason": "str",
    },
)
async def cleanup_invites(ctx: Ctx) -> Dict[str, Any]:
    import asyncio

    data = await ctx.body()
    if not parse_bool(data.get("confirm"), field="confirm", default=False):
        raise ApiError.bad_request(
            "Invite-Bereinigung ist destruktiv.",
            hint='Sende {"only_unused": true, "confirm": true}.',
            code="CONFIRM_REQUIRED",
        )
    only_unused = bool(parse_bool(data.get("only_unused"), field="only_unused", default=True))
    keep = {str(code).strip() for code in (data.get("keep_codes") or [])}

    invites = await guard(ctx.guild.invites(), action="Invites laden")
    deleted: List[str] = []
    errors: List[Dict[str, Any]] = []
    for invite in invites:
        if invite.code in keep:
            continue
        if only_unused and (invite.uses or 0) > 0:
            continue
        try:
            await guard(invite.delete(reason=ctx.reason(data, default="Invite-Bereinigung (Arena AI)")),
                        action=f"Invite {invite.code} löschen")
            deleted.append(invite.code)
        except ApiError as exc:
            errors.append({"code": invite.code, "code_error": exc.code, "message": exc.message})
        await asyncio.sleep(0.3)

    return {"deleted_count": len(deleted), "deleted": deleted, "errors": errors or None,
            "kept": sorted(keep)}

