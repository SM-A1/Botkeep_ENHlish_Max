"""Run: python tests/test_group.py   (pure logic, no network, no aiogram needed)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import group_utils as g

BOT = "VideoShareBot"

# ---- command parsing -------------------------------------------------------------------
assert g.group_command("/video", BOT) == ("video", "")
assert g.group_command("/video 10", BOT) == ("video", "10")
assert g.group_command("/VIDEO@videosharebot 7", BOT) == ("video", "7")      # case-insensitive
assert g.group_command("/new@VideoShareBot 3", BOT) == ("new", "3")
assert g.group_command("/video@OtherBot 5", BOT) is None                     # another bot's command
assert g.group_command("/video@", BOT) == ("video", "")                       # malformed target: no crash
assert g.group_command("/admin", BOT) is None                                # not a group command
assert g.group_command("/ban 5", BOT) is None
assert g.group_command("hello", BOT) is None                                 # normal chat ignored
assert g.group_command("12", BOT) is None
assert g.group_command("/", BOT) is None
assert g.group_command(None, BOT) is None
assert g.group_command("/video@VideoShareBot", None) is None                 # username unknown: be safe
assert g.group_command("/video", None) == ("video", "")

# ---- request size ----------------------------------------------------------------------
assert g.parse_count("5", 10) == 5
assert g.parse_count(" 10 ", 10) == 10
assert g.parse_count("11", 10) is None
assert g.parse_count("0", 10) is None
assert g.parse_count("-3", 10) is None
assert g.parse_count("abc", 10) is None
assert g.parse_count("", 10) is None
assert g.parse_count(None, 10) is None
assert g.parse_count("5 more", 10) == 5
assert g.parse_count("9999999", 10) is None                                   # > 6 digits

# ---- callback data: round trip, owner id, Telegram's 64-byte limit ------------------------
uid = 6_000_000_000
for data, expect in [
    (g.cb_quantity(uid, 5), {"action": "q", "uid": uid, "count": 5}),
    (g.cb_deliver(uid, "new", 10), {"action": "t", "uid": uid, "category": "new", "count": 10}),
    (g.cb_deliver(uid, "old", 3, shuffle=True),
     {"action": "s", "uid": uid, "category": "old", "count": 3}),
    (g.cb_close(uid), {"action": "x", "uid": uid}),
]:
    assert len(data.encode()) <= 64, data
    assert g.parse_callback(data) == expect, data

for bad in (None, "", "g:", "g:q", "g:q:1", "g:q:x:5", "g:t:1:fake:5", "g:t:1:new", "g:z:1:2",
            "g:t:1:new:5:6", "menu", "qty:5", "g:x", "g:x:abc"):
    assert g.parse_callback(bad) is None, bad                                 # never raises

# the legacy private-chat callbacks must never be mistaken for group ones
for legacy in ("type:new", "more:new:10", "fresh:old:5", "mmore:new", "qty:10", "again", "menu"):
    assert not legacy.startswith(g.GROUP_PREFIX)

# ---- misc ---------------------------------------------------------------------------------
assert g.deep_link("MyBot") == "https://t.me/MyBot"
assert g.deep_link("MyBot", "verify") == "https://t.me/MyBot?start=verify"
assert g.deep_link("MyBot", "a b&c") == "https://t.me/MyBot?start=abc"       # payload sanitised
assert g.deep_link(None) is None

assert g.mention(42, "A<b>") == '<a href="tg://user?id=42">A&lt;b&gt;</a>'   # name is escaped
assert g.mention(42, "") == '<a href="tg://user?id=42">there</a>'

class Chat:  # duck-typed
    def __init__(self, t): self.type = t
assert g.is_group_chat(Chat("group")) and g.is_group_chat(Chat("supergroup"))
assert not g.is_group_chat(Chat("private")) and not g.is_group_chat(Chat("channel"))
assert not g.is_group_chat(None)

print("test_group: all checks passed")
