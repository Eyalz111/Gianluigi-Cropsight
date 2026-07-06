#!/usr/bin/env python3
"""
Re-surface a stuck pending approval card in Eyal's Telegram DM.

Sends a fresh card (with the LIVE callback buttons — Approve / Request Changes /
Reject / CEO-Founders-Company band picker / Custom…) for a meeting that is
`approval_status=pending` but whose card Eyal never saw. The buttons act by
meeting_id, so the running service handles them exactly like the original card;
the card text is a concise identifier (the full content is distributed on Approve).

Idempotent: does NOT touch the DB — pure Telegram send (via python-telegram-bot).

Usage:
    python scripts/resend_approval_card.py <meeting_id>
"""

import asyncio
import sys

sys.path.insert(0, __file__.rsplit("scripts", 1)[0])

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import settings
from services.supabase_client import supabase_client as sb
from guardrails.distribution import band_for_sensitivity


def _counts(mid: str) -> dict:
    out = {}
    for tbl, fk in [("tasks", "meeting_id"), ("decisions", "meeting_id"),
                    ("open_questions", "meeting_id"), ("follow_up_meetings", "source_meeting_id")]:
        try:
            out[tbl] = sb.client.table(tbl).select("id", count="exact").eq(fk, mid).execute().count or 0
        except Exception:
            out[tbl] = 0
    return out


def _keyboard(mid: str, sensitivity: str) -> InlineKeyboardMarkup:
    current = band_for_sensitivity(sensitivity)
    bands = [("ceo", "CEO-only"), ("founders", "Founders"), ("company", "Company")]
    band_row = [InlineKeyboardButton((f"● {n}" if b == current else n), callback_data=f"sens_set:{b}:{mid}")
                for b, n in bands]
    band_row.append(InlineKeyboardButton("Custom…", callback_data=f"dcust:{mid}"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Approve", callback_data=f"approve:{mid}"),
         InlineKeyboardButton("Request Changes", callback_data=f"edit:{mid}")],
        [InlineKeyboardButton("Reject", callback_data=f"reject:{mid}")],
        band_row,
    ])


async def main() -> int:
    if len(sys.argv) < 2:
        print("usage: resend_approval_card.py <meeting_id>")
        return 2
    mid = sys.argv[1]
    m = sb.get_meeting(mid)
    if not m:
        print(f"meeting {mid} not found")
        return 2
    if (m.get("approval_status") or "") != "pending":
        print(f"meeting {mid} is '{m.get('approval_status')}', not pending — refusing to resend")
        return 2

    c = _counts(mid)
    text = (
        f"📋 <b>Pending summary — waiting in your queue</b>\n\n"
        f"<b>{m.get('title', 'Untitled')}</b>\n{str(m.get('date'))[:10]}\n\n"
        f"• {c['tasks']} tasks · {c['decisions']} decisions · "
        f"{c['open_questions']} open questions · {c['follow_up_meetings']} follow-ups\n\n"
        f"Approve to distribute (pick a band or Custom first if you like)."
    )
    chat_id = settings.EYAL_TELEGRAM_ID or settings.TELEGRAM_EYAL_CHAT_ID
    bot = Bot(settings.TELEGRAM_BOT_TOKEN)
    async with bot:
        msg = await bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML",
            reply_markup=_keyboard(mid, m.get("sensitivity", "founders")),
        )
    print(f"ok — sent message {msg.message_id} to {chat_id}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
