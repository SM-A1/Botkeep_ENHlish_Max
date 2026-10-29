"""Telegram message effects (private chats only). Every send falls back to a plain message
if Telegram or an old client refuses the effect, so a delivery summary is never lost."""
from __future__ import annotations

FIRE = "5104841245755180586"       # 🔥
PARTY = "5046509860389126442"      # 🎉
HEART = "5044134455711629726"      # ❤️
LIKE = "5107584321108051014"       # 👍
