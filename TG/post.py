"""
Post Manager
============
Lets an admin/owner publish a formatted post (poster + title + genre +
description + a "📖 Read Now" button) to a public/private "Post Channel".

Workflow:
  1. Admin sets a Dump Channel and a Post Channel once via /postsettings
     (the bot must be an admin in both).
  2. Admin runs /newpost (or taps "➕ Create New Post"). The bot asks for the
     file(s) first -> each one is copied into the Dump Channel and its
     message id is remembered. Then it asks for poster / title / genre /
     description, shows a preview, and publishes on confirmation.
  3. The published post's "📖 Read Now" button is a deep link
     (https://t.me/<bot>?start=get_<post_id>). Clicking it opens a private
     chat with the bot, which fetches the stored file(s) from the Dump
     Channel and copies them straight to the user.
"""

import asyncio
import random
import re
import string
import time

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import ListenerTimeout as TimeoutError

from bot import Bot, Vars, logger
from Tools.db import (
    get_config, set_config, save_post, get_post,
    delete_post, all_posts, bump_post_reads, uts, get_episode_number,
    get_log_channel,
)
from Tools.base import TaskCard, progress_bar
from Tools.cworker import send_manga_chapter
from .storage import (
    retry_on_flood, igrone_error, post_targets, call_iter_chapters,
    report_errors,
)


# In-memory wizard drafts, keyed by the admin's user_id, while a post is
# being built and awaiting "✅ Confirm & Publish".
_drafts = {}

# In-memory state for the "search -> 📤 Post to Channel" auto-fetch flow,
# keyed by admin user_id, while chapters/range are being picked.
_auto_state = {}

CANCEL_WORDS = ("/cancel", "cancel")


_STATUS_RE = re.compile(r"\*\*Status\*\*:\s*`([^`]*)`")
_GENRE_RE = re.compile(r"\*\*Genres\*\*:\s*`([^`]*)`")
_DESC_RE = re.compile(r"\*\*Description\*\*:\s*<blockquote expandable><i>(.*?)</i></blockquote>", re.S)


def parse_manga_msg(bio_list: dict):
    """Best-effort extraction of status/genre/description from the scraper's
    pre-formatted `msg` blob. Every site module in Webs/ builds this from the
    same shared template (Webs/utitls.py), so one parser covers all of them.
    Returns (title, genre, status, description) — any of which may be "".
    """
    msg = bio_list.get("msg", "") or ""
    title = bio_list.get("title", "Unknown")

    status_m = _STATUS_RE.search(msg)
    genre_m = _GENRE_RE.search(msg)
    desc_m = _DESC_RE.search(msg)

    status = status_m.group(1).strip() if status_m else ""
    genre = genre_m.group(1).strip() if genre_m else ""
    description = desc_m.group(1).strip() if desc_m else ""
    if description.endswith("..."):
        description = description[:-3].strip()

    return title, genre, status, description


def _gen_post_id() -> str:
    chars = string.ascii_letters + string.digits
    while True:
        pid = "".join(random.choices(chars, k=8))
        if not get_post(pid):
            return pid


def _norm_channel(value):
    """Channel values may be a numeric id or a @username - keep whichever it is."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def get_post_channel():
    return get_config("post_channel", Vars.POST_CHANNEL)


def get_dump_channel():
    return get_config("dump_channel", Vars.DUMP_CHANNEL)


# ---------------------------------------------------------------------------
# Stylish text helpers — built from actual Unicode codepoints (chr(...)),
# not hand-typed glyphs, so there's no risk of a copy/paste typo landing in
# every post. Only ever transforms plain ASCII letters, so emoji, digits,
# punctuation, and non-Latin titles (Korean, Japanese, Chinese, ...) always
# pass through untouched.
# ---------------------------------------------------------------------------

_SMALL_CAPS = {
    'a': chr(0x1D00), 'b': chr(0x0299), 'c': chr(0x1D04), 'd': chr(0x1D05),
    'e': chr(0x1D07), 'f': chr(0xA730), 'g': chr(0x0262), 'h': chr(0x029C),
    'i': chr(0x026A), 'j': chr(0x1D0A), 'k': chr(0x1D0B), 'l': chr(0x029F),
    'm': chr(0x1D0D), 'n': chr(0x0274), 'o': chr(0x1D0F), 'p': chr(0x1D18),
    'q': 'q', 'r': chr(0x0280), 's': 's', 't': chr(0x1D1B),
    'u': chr(0x1D1C), 'v': chr(0x1D20), 'w': chr(0x1D21), 'x': 'x',
    'y': chr(0x028F), 'z': chr(0x1D22),
}


def to_small_caps(text: str) -> str:
    """'sᴛʏʟɪsʜ' small-caps rendering, ASCII letters only."""
    return "".join(
        _SMALL_CAPS.get(ch.lower(), ch) if ch.isascii() and ch.isalpha() else ch
        for ch in text
    )


def to_bold_serif(text: str) -> str:
    """𝐁𝐨𝐥𝐝 mathematical-bold rendering, ASCII letters/digits only."""
    out = []
    for ch in text:
        if 'A' <= ch <= 'Z':
            out.append(chr(0x1D400 + (ord(ch) - ord('A'))))
        elif 'a' <= ch <= 'z':
            out.append(chr(0x1D41A + (ord(ch) - ord('a'))))
        elif '0' <= ch <= '9':
            out.append(chr(0x1D7CE + (ord(ch) - ord('0'))))
        else:
            out.append(ch)
    return "".join(out)


def _template_elegant(title, genre_txt, description):
    caption = f"✨ <b>{title}</b> ✨\n\n"
    if genre_txt:
        caption += f"🏷 <i>{genre_txt}</i>\n\n"
    caption += f"<blockquote expandable>{description}</blockquote>\n\n"
    caption += "📖 Tap below to start reading."
    return caption


def _template_bold(title, genre_txt, description):
    bar = "🔥" + ("━" * 13) + "🔥"
    caption = f"{bar}\n📚 <b>{to_small_caps(title)}</b>\n{bar}\n\n"
    if genre_txt:
        caption += f"🎭 <b>{to_small_caps('Genre')}:</b> <code>{genre_txt}</code>\n\n"
    caption += (
        f"📝 <b>{to_small_caps('Synopsis')}:</b>\n"
        f"<blockquote expandable>{description}</blockquote>\n\n"
    )
    caption += f"👇 {to_small_caps('Tap below to read now')} 👇"
    return caption


def _template_premium(title, genre_txt, description):
    caption = (
        "💎 <b>PREMIUM RELEASE</b> 💎\n"
        "┏━━━━━━━━━━━━━━━┓\n"
        f"┃ 📖 <b>{title}</b>\n"
        "┗━━━━━━━━━━━━━━━┛\n\n"
    )
    if genre_txt:
        caption += f"🏆 <b>Genre:</b> {genre_txt}\n\n"
    caption += f"⭐ <b>Description:</b>\n<blockquote expandable>{description}</blockquote>\n\n"
    caption += f"✨🔥 {to_bold_serif('New chapter dropped')}! Tap below now 🔥✨"
    return caption


TEMPLATES = {
    "elegant": _template_elegant,
    "bold": _template_bold,
    "premium": _template_premium,
}
TEMPLATE_ORDER = ["elegant", "bold", "premium"]
TEMPLATE_LABELS = {"elegant": "✨ Elegant", "bold": "🔥 Bold", "premium": "💎 Premium"}
DEFAULT_TEMPLATE = "elegant"


def _build_caption(title, genre_txt, description, template=DEFAULT_TEMPLATE):
    fn = TEMPLATES.get(template, _template_elegant)
    return fn(title, genre_txt, description)[:1024]


def _preview_markup(template=DEFAULT_TEMPLATE):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 Read Now", callback_data="pmp_noop")],
        [InlineKeyboardButton(
            f"🎨 Style: {TEMPLATE_LABELS.get(template, template)}  ·  tap for another",
            callback_data="pdraft_style",
        )],
        [
            InlineKeyboardButton("✅ Confirm & Publish", callback_data="pdraft_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="pdraft_cancel"),
        ],
    ])


def _settings_text():
    post_channel = get_post_channel()
    dump_channel = get_dump_channel()
    total = len(all_posts())
    return (
        "<b>📮 Post Manager</b>\n\n"
        f"<b>📢 Post Channel:</b> <code>{post_channel or 'Not Set'}</code>\n"
        f"<b>📥 Dump Channel:</b> <code>{dump_channel or 'Not Set'}</code>\n"
        f"<b>📚 Total Posts:</b> <code>{total}</code>\n\n"
        "<blockquote expandable>"
        "• <b>Dump Channel</b> — where the actual manga files are stored. The bot "
        "downloads/keeps files here, and later serves them to users.\n"
        "• <b>Post Channel</b> — where the poster + title + genre + description + "
        "\"Read Now\" post gets published.\n"
        "• The bot must be an <b>admin</b> in both channels."
        "</blockquote>"
    )


def _settings_markup():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 Set Post Channel", callback_data="pmset_post"),
            InlineKeyboardButton("📥 Set Dump Channel", callback_data="pmset_dump"),
        ],
        [InlineKeyboardButton("➕ Create New Post", callback_data="pmp_new")],
        [InlineKeyboardButton("📋 Manage Posts", callback_data="pmp_manage:1")],
        [InlineKeyboardButton("⚙️ Bot Settings ↗️", callback_data="bspanel")],
        [InlineKeyboardButton("✖️ Close", callback_data="kclose")],
    ])


# ---------------------------------------------------------------------------
# /postsettings — configure Post Channel & Dump Channel, manage posts
# ---------------------------------------------------------------------------

@Bot.on_message(filters.command(["postsettings", "pm"]) & filters.user(Vars.ADMINS))
@report_errors
async def post_settings_cmd(client, message):
    await retry_on_flood(message.reply_text)(
        _settings_text(), quote=True, reply_markup=_settings_markup()
    )


@Bot.on_callback_query(filters.regex("^pmpanel$") & filters.user(Vars.ADMINS))
@report_errors
async def post_settings_panel_cb(client, query):
    await igrone_error(query.answer)()
    await retry_on_flood(query.edit_message_text)(
        _settings_text(), reply_markup=_settings_markup()
    )


@Bot.on_callback_query(filters.regex("^pmset_") & filters.user(Vars.ADMINS))
@report_errors
async def post_settings_set_cb(client, query):
    kind = query.data.removeprefix("pmset_")  # "post" or "dump"
    label = "Post Channel" if kind == "post" else "Dump Channel"
    await igrone_error(query.answer)()

    await retry_on_flood(query.edit_message_text)(
        f"<b>📐 Send the {label}</b>\n\n"
        "<blockquote>Forward any message from that channel, or send its "
        "username (without @) or numeric ID. Make sure the bot is an admin "
        f"there.\n\nSend /cancel to abort.</blockquote>"
    )

    try:
        call = await client.listen(
            user_id=query.from_user.id, timeout=120,
            filters=filters.text | filters.forwarded,
        )
    except TimeoutError:
        return await retry_on_flood(query.message.edit_text)(
            "📐 Timed out. Run /postsettings again."
        )

    if call.text and call.text.strip().lower() in CANCEL_WORDS:
        await igrone_error(call.delete)()
        return await retry_on_flood(query.message.edit_text)(
            _settings_text(), reply_markup=_settings_markup()
        )

    value = None
    if call.forward_from_chat:
        value = call.forward_from_chat.id
    elif call.text:
        text = call.text.strip()
        try:
            value = int(text)
        except ValueError:
            value = text

    await igrone_error(call.delete)()

    if value is None:
        return await retry_on_flood(query.message.edit_text)(
            "❌ Couldn't read that as a channel. Run /postsettings again."
        )

    set_config(f"{kind}_channel", value)
    await retry_on_flood(query.message.edit_text)(f"✅ {label} set to <code>{value}</code>.")
    await asyncio.sleep(1.5)
    await retry_on_flood(query.message.edit_text)(
        _settings_text(), reply_markup=_settings_markup()
    )


# ---------------------------------------------------------------------------
# /newpost — step-by-step wizard to build & publish a post
# ---------------------------------------------------------------------------

@Bot.on_callback_query(filters.regex("^pmp_new$") & filters.user(Vars.ADMINS))
@report_errors
async def new_post_cb(client, query):
    await igrone_error(query.answer)()
    await create_post_wizard(client, query.message.chat.id, query.from_user.id)


@Bot.on_message(filters.command("newpost") & filters.user(Vars.ADMINS))
@report_errors
async def new_post_cmd(client, message):
    await create_post_wizard(client, message.chat.id, message.from_user.id)


async def _ask(client, chat_id, admin_id, text, timeout=180):
    """Send a prompt and wait for the admin's next message."""
    await retry_on_flood(client.send_message)(chat_id, text)
    try:
        return await client.listen(user_id=admin_id, timeout=timeout)
    except TimeoutError:
        await retry_on_flood(client.send_message)(chat_id, "⏰ Timed out. Post creation cancelled.")
        return None


async def create_post_wizard(client, chat_id, admin_id):
    dump_channel = get_dump_channel()
    if not dump_channel:
        return await retry_on_flood(client.send_message)(
            chat_id,
            "❌ No <b>Dump Channel</b> is set yet. Set one first with /postsettings."
        )

    file_ids = []

    # Step 1 — files, saved straight into the Dump Channel
    await retry_on_flood(client.send_message)(
        chat_id,
        "<b>📁 Step 1/5 — Files</b>\n\n"
        "Send the file(s) for this post one at a time (documents, PDFs, "
        "images, or forward them from anywhere). They'll be stored in the "
        "Dump Channel.\n\nSend <code>/done</code> when finished, or "
        "<code>/cancel</code> to abort."
    )
    while True:
        try:
            call = await client.listen(user_id=admin_id, timeout=300)
        except TimeoutError:
            return await retry_on_flood(client.send_message)(chat_id, "⏰ Timed out. Post creation cancelled.")

        text = (call.text or "").strip().lower()
        if text == "/cancel":
            await igrone_error(call.delete)()
            return await retry_on_flood(client.send_message)(chat_id, "❌ Post creation cancelled.")

        if text == "/done":
            await igrone_error(call.delete)()
            break

        media = call.document or call.video or call.audio or call.photo or call.animation
        if not media:
            await retry_on_flood(client.send_message)(chat_id, "⚠️ Send a file, or /done, or /cancel.")
            continue

        try:
            copied = await retry_on_flood(call.copy)(_norm_channel(dump_channel))
            file_ids.append(copied.id)
            await retry_on_flood(client.send_message)(
                chat_id,
                f"✅ Saved to Dump Channel ({len(file_ids)} file(s) so far). Send another, or /done."
            )
        except Exception as e:
            logger.exception(e)
            await retry_on_flood(client.send_message)(
                chat_id,
                f"❌ Couldn't save that to the Dump Channel: <code>{e}</code>\n"
                "Make sure the bot is an admin there."
            )

    if not file_ids:
        return await retry_on_flood(client.send_message)(chat_id, "❌ No files were saved. Post creation cancelled.")

    # Step 2 — poster
    reply = await _ask(
        client, chat_id, admin_id,
        "<b>🖼 Step 2/5 — Poster</b>\n\nSend the poster image (as a photo), or paste an image URL."
    )
    if reply is None:
        return
    if reply.text and reply.text.strip().lower() in CANCEL_WORDS:
        await igrone_error(reply.delete)()
        return await retry_on_flood(client.send_message)(chat_id, "❌ Post creation cancelled.")

    poster = None
    if reply.photo:
        poster = reply.photo.file_id
    elif reply.text and reply.text.strip().startswith("http"):
        poster = reply.text.strip()
    await igrone_error(reply.delete)()

    if not poster:
        return await retry_on_flood(client.send_message)(chat_id, "❌ That's not a valid poster. Post creation cancelled.")

    # Step 3 — title
    reply = await _ask(client, chat_id, admin_id, "<b>📝 Step 3/5 — Title</b>\n\nSend the manga's title.")
    if reply is None:
        return
    title = (reply.text or "").strip()
    await igrone_error(reply.delete)()
    if not title or title.lower() in CANCEL_WORDS:
        return await retry_on_flood(client.send_message)(chat_id, "❌ Post creation cancelled.")

    # Step 4 — genre
    reply = await _ask(
        client, chat_id, admin_id,
        "<b>🏷 Step 4/5 — Genre</b>\n\nSend the genre(s), comma separated.\n"
        "e.g. <code>Action, Fantasy, Drama</code>"
    )
    if reply is None:
        return
    genre_raw = (reply.text or "").strip()
    await igrone_error(reply.delete)()
    if not genre_raw or genre_raw.lower() in CANCEL_WORDS:
        return await retry_on_flood(client.send_message)(chat_id, "❌ Post creation cancelled.")
    genres = [g.strip() for g in genre_raw.split(",") if g.strip()]
    genre_txt = " ".join(f"#{g.replace(' ', '_')}" for g in genres)

    # Step 5 — description
    reply = await _ask(
        client, chat_id, admin_id,
        "<b>🗒 Step 5/5 — Description</b>\n\nSend the synopsis/description."
    )
    if reply is None:
        return
    description = (reply.text or reply.caption or "").strip()
    await igrone_error(reply.delete)()
    if not description or description.lower() in CANCEL_WORDS:
        return await retry_on_flood(client.send_message)(chat_id, "❌ Post creation cancelled.")

    post_id = _gen_post_id()
    _drafts[admin_id] = {
        "post_id": post_id,
        "title": title,
        "genre": genre_txt,
        "description": description,
        "poster": poster,
        "dump_channel": _norm_channel(dump_channel),
        "file_ids": file_ids,
        "template": DEFAULT_TEMPLATE,
    }

    caption = _build_caption(title, genre_txt, description, DEFAULT_TEMPLATE)
    await retry_on_flood(client.send_photo)(
        chat_id, poster,
        caption="<b>🔎 Preview — this is exactly how it will look:</b>\n\n" + caption,
        reply_markup=_preview_markup(DEFAULT_TEMPLATE),
    )


@Bot.on_callback_query(filters.regex("^pdraft_") & filters.user(Vars.ADMINS))
@report_errors
async def draft_decision_cb(client, query):
    admin_id = query.from_user.id
    draft = _drafts.get(admin_id)
    if not draft:
        return await retry_on_flood(query.answer)("⚠️ No pending draft found. Run /newpost again.", show_alert=True)

    action = query.data.removeprefix("pdraft_")
    if action == "cancel":
        del _drafts[admin_id]
        await igrone_error(query.answer)("❌ Cancelled")
        return await igrone_error(query.message.delete)()

    if action == "style":
        current = draft.get("template", DEFAULT_TEMPLATE)
        next_idx = (TEMPLATE_ORDER.index(current) + 1) % len(TEMPLATE_ORDER) if current in TEMPLATE_ORDER else 0
        draft["template"] = TEMPLATE_ORDER[next_idx]
        preview_caption = _build_caption(draft["title"], draft["genre"], draft["description"], draft["template"])
        await igrone_error(query.answer)(f"Style: {TEMPLATE_LABELS[draft['template']]}")
        return await retry_on_flood(query.edit_message_caption)(
            "<b>🔎 Preview — this is exactly how it will look:</b>\n\n" + preview_caption,
            reply_markup=_preview_markup(draft["template"]),
        )

    post_channel = get_post_channel()
    if not post_channel:
        return await retry_on_flood(query.answer)(
            "❌ No Post Channel set. Set one via /postsettings first.", show_alert=True
        )

    template = draft.get("template", DEFAULT_TEMPLATE)
    caption = _build_caption(draft["title"], draft["genre"], draft["description"], template)
    button = InlineKeyboardMarkup([[
        InlineKeyboardButton("📖 Read Now", url=f"https://t.me/{Bot.username}?start=get_{draft['post_id']}")
    ]])

    try:
        sent = await retry_on_flood(client.send_photo)(
            _norm_channel(post_channel), draft["poster"], caption=caption, reply_markup=button
        )
    except Exception as e:
        logger.exception(e)
        return await retry_on_flood(query.answer)(f"❌ Failed to publish: {e}", show_alert=True)

    save_post(draft["post_id"], {
        "title": draft["title"],
        "genre": draft["genre"],
        "description": draft["description"],
        "poster": draft["poster"],
        "dump_channel": draft["dump_channel"],
        "file_ids": draft["file_ids"],
        "template": template,
        "post_channel": _norm_channel(post_channel),
        "post_msg_id": sent.id,
        "created_by": admin_id,
        "created_at": int(time.time()),
        "reads": 0,
    })
    del _drafts[admin_id]

    await igrone_error(query.answer)("✅ Published!")
    txt = f"✅ <b>Post published!</b>\n\n<b>ID:</b> <code>{draft['post_id']}</code>"
    if getattr(sent.chat, "username", None):
        txt += f"\n<b>Link:</b> https://t.me/{sent.chat.username}/{sent.id}"
    await igrone_error(query.message.edit_caption)(txt)


@Bot.on_callback_query(filters.regex("^pmp_noop$"))
async def noop_cb(client, query):
    await igrone_error(query.answer)("This is just a preview of the button 🙂")


# ---------------------------------------------------------------------------
# Manage existing posts (list / delete)
# ---------------------------------------------------------------------------

@Bot.on_callback_query(filters.regex("^pmp_manage") & filters.user(Vars.ADMINS))
@report_errors
async def manage_posts_cb(client, query):
    await igrone_error(query.answer)()
    try:
        page = int(query.data.split(":")[-1])
    except Exception:
        page = 1

    posts = all_posts()
    items = sorted(posts.items(), key=lambda kv: kv[1].get("created_at", 0), reverse=True)

    if not items:
        button = InlineKeyboardMarkup([[InlineKeyboardButton("⇦ Back", callback_data="pmpanel")]])
        return await retry_on_flood(query.edit_message_text)("📭 No posts yet.", reply_markup=button)

    per_page = 8
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    page_items = items[(page - 1) * per_page: page * per_page]

    button = []
    for post_id, data in page_items:
        title = data.get("title", post_id)[:26]
        reads = data.get("reads", 0)
        button.append([
            InlineKeyboardButton(f"📖 {title} ({reads})", url=f"https://t.me/{Bot.username}?start=get_{post_id}"),
            InlineKeyboardButton("🗑", callback_data=f"pmp_del_{post_id}"),
        ])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"pmp_manage:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="pmp_noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"pmp_manage:{page + 1}"))
    button.append(nav)
    button.append([InlineKeyboardButton("⇦ Back", callback_data="pmpanel")])

    await retry_on_flood(query.edit_message_text)(
        f"<b>📋 Manage Posts</b> ({len(items)} total)\n"
        "<i>Number in brackets = how many times it's been delivered.</i>",
        reply_markup=InlineKeyboardMarkup(button),
    )


@Bot.on_callback_query(filters.regex("^pmp_del_") & filters.user(Vars.ADMINS))
@report_errors
async def delete_post_cb(client, query):
    post_id = query.data.removeprefix("pmp_del_")
    post = get_post(post_id)
    if not post:
        return await retry_on_flood(query.answer)("⚠️ Already deleted.", show_alert=True)

    if post.get("post_channel") and post.get("post_msg_id"):
        await igrone_error(client.delete_messages)(
            _norm_channel(post["post_channel"]), int(post["post_msg_id"])
        )

    delete_post(post_id)
    await retry_on_flood(query.answer)("🗑 Post deleted.", show_alert=True)

    query.data = "pmp_manage:1"
    await manage_posts_cb(client, query)


# ---------------------------------------------------------------------------
# "📤 Post to Channel" — triggered from a manga's info card in search results.
# Auto-fetches details, lets the admin pick which chapters, downloads them
# straight into the Dump Channel, then hands off to the same preview/confirm
# step used by the manual /newpost wizard.
# ---------------------------------------------------------------------------

async def _gather_all_chapters(webs, data, max_pages=50):
    """Walk every page of the chapter list and return it newest-first
    (matching each site module's native order), capped at max_pages as a
    safety net against runaway pagination."""
    all_chapters = []
    seen = set()
    for page in range(1, max_pages + 1):
        try:
            chapters = await call_iter_chapters(webs, data, page=page)
        except Exception as e:
            logger.exception(e)
            break

        if not chapters:
            break

        new = [c for c in chapters if c.get("url") not in seen]
        if not new:
            break
        seen.update(c["url"] for c in new)
        all_chapters.extend(new)

        if len(chapters) < 60:  # short page = last page
            break

    return all_chapters


def _parse_range(chapters, text):
    """'5' -> chapter 5 only. '1-20' -> chapters 1..20. '-5' -> last 5."""
    text = text.strip()
    try:
        if "-" in text:
            a, b = text.split("-", 1)
            a, b = a.strip(), b.strip()
            if a == "":
                n = int(b)
                return chapters[-n:] if n > 0 else None
            start, end = int(a) - 1, int(b)
            if start < 0 or end <= start:
                return None
            return chapters[start:end]
        n = int(text)
        if n < 1 or n > len(chapters):
            return None
        return [chapters[n - 1]]
    except (ValueError, IndexError):
        return None


@Bot.on_callback_query(filters.regex("^pnew:") & filters.user(Vars.ADMINS))
@report_errors
async def post_new_from_search_cb(client, query):
    if query.data not in post_targets:
        return await retry_on_flood(query.answer)(
            "This is an old button, please redo the search", show_alert=True
        )

    if not get_dump_channel() or not get_post_channel():
        return await retry_on_flood(query.answer)(
            "❌ Set both a Dump Channel and a Post Channel first, via /postsettings.",
            show_alert=True,
        )

    webs, bio_list, data = post_targets[query.data]
    admin_id = query.from_user.id

    await igrone_error(query.answer)("🔎 Fetching chapter list...")
    await retry_on_flood(query.edit_message_caption)(
        f"<b>🔎 Fetching chapters for {bio_list.get('title', 'this manga')}...</b>"
    )

    chapters = await _gather_all_chapters(webs, bio_list)
    if not chapters:
        return await retry_on_flood(query.edit_message_caption)("❌ No chapters found.")

    chapters = list(reversed(chapters))  # oldest -> newest, so "1" = chapter 1

    _auto_state[admin_id] = {
        "webs": webs,
        "bio_list": bio_list,
        "chapters": chapters,
        "chat_id": query.message.chat.id,
    }

    total = len(chapters)
    button = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📥 All ({total})", callback_data="prange_all")],
        [InlineKeyboardButton("🆕 Latest chapter only", callback_data="prange_latest")],
        [InlineKeyboardButton("🔢 Custom range", callback_data="prange_custom")],
        [InlineKeyboardButton("❌ Cancel", callback_data="prange_cancel")],
    ])
    await retry_on_flood(query.edit_message_caption)(
        f"<b>{bio_list.get('title', 'Manga')}</b>\n\n"
        f"Found <b>{total}</b> chapter(s). What should this post include?",
        reply_markup=button,
    )


@Bot.on_callback_query(filters.regex("^prange_") & filters.user(Vars.ADMINS))
@report_errors
async def post_range_cb(client, query):
    admin_id = query.from_user.id
    state = _auto_state.get(admin_id)
    if not state:
        return await retry_on_flood(query.answer)(
            "⚠️ This expired — open the manga from search again.", show_alert=True
        )

    choice = query.data.removeprefix("prange_")
    await igrone_error(query.answer)()
    chapters = state["chapters"]

    if choice == "cancel":
        del _auto_state[admin_id]
        return await igrone_error(query.message.delete)()

    elif choice == "all":
        selected = chapters

    elif choice == "latest":
        selected = chapters[-1:]

    elif choice == "custom":
        await retry_on_flood(query.edit_message_caption)(
            "<b>🔢 Send the chapter range</b>\n\n"
            "e.g. <code>1-20</code> for chapters 1 through 20, <code>-5</code> for "
            "the last 5, or a single number for one chapter.\n\nSend /cancel to abort."
        )
        try:
            reply = await client.listen(user_id=admin_id, timeout=120)
        except TimeoutError:
            del _auto_state[admin_id]
            return await retry_on_flood(query.message.edit_caption)("⏰ Timed out.")

        text = (reply.text or "").strip()
        await igrone_error(reply.delete)()

        if text.lower() in CANCEL_WORDS:
            del _auto_state[admin_id]
            return await retry_on_flood(query.message.edit_caption)("❌ Cancelled.")

        selected = _parse_range(chapters, text)
        if not selected:
            del _auto_state[admin_id]
            return await retry_on_flood(query.message.edit_caption)(
                "❌ Couldn't understand that range. Open the manga from search again to retry."
            )
    else:
        return

    del _auto_state[admin_id]
    await _run_auto_post(client, state["chat_id"], admin_id, state["webs"], state["bio_list"], selected)


async def _run_auto_post(client, chat_id, admin_id, webs, bio_list, chapters):
    dump_channel = get_dump_channel()
    try:
        dump_channel_id = int(dump_channel)
    except (TypeError, ValueError):
        return await retry_on_flood(client.send_message)(
            chat_id,
            "❌ Auto-download needs the Dump Channel set as a numeric ID.\n"
            "Reconfigure it via /postsettings by <b>forwarding</b> a message from "
            "that channel (rather than typing its @username)."
        )

    sts = await retry_on_flood(client.send_message)(
        chat_id, f"<b>📥 Preparing to download {len(chapters)} chapter(s)...</b>"
    )

    merge_size = uts.get(str(admin_id), {}).get("setting", {}).get("megre", None)
    try:
        merge_size = int(merge_size) if merge_size else None
    except (TypeError, ValueError):
        merge_size = None
    priority = uts.get(str(admin_id), {}).get("setting", {}).get("premuim", 1)

    groups = []
    if merge_size and merge_size > 1:
        for i in range(0, len(chapters), merge_size):
            groups.append(chapters[i:i + merge_size])
    else:
        groups = [[c] for c in chapters]

    file_ids = []
    done = 0
    failed = 0
    total_groups = len(groups)

    for idx, group in enumerate(groups, start=1):
        label = group[0]["title"] if len(group) == 1 else f"{group[0]['title']} … {group[-1]['title']}"
        overall_pct = int((idx - 1) * 100 / total_groups) if total_groups else 100
        _last_edit = {"t": 0.0}

        async def _group_progress(page_done, page_total, _idx=idx, _label=label, _overall_pct=overall_pct):
            now = time.monotonic()
            if page_done < page_total and (now - _last_edit["t"]) < 2.0:
                return
            _last_edit["t"] = now
            page_pct = int(page_done * 100 / page_total) if page_total else 100
            await igrone_error(sts.edit)(
                f"<b>📥 Downloading chapter {_idx}/{total_groups}</b> ({_overall_pct}% overall)\n"
                f"{progress_bar(_overall_pct)}\n\n"
                f"<i>{_label}</i>\n"
                f"Pages: {progress_bar(page_pct)} {page_done}/{page_total}\n\n"
                f"✅ {done} done · ❌ {failed} failed"
            )

        await _group_progress(0, 1)

        # Fetch every chapter's pages within this merge-group and concatenate
        # them in order, so a merged file actually contains all of them
        # (rather than only the first chapter's pages).
        group_pictures = []
        ok = True
        for chapter in group:
            try:
                pics = await webs.get_pictures(url=chapter["url"], data=chapter)
            except Exception as e:
                logger.exception(e)
                pics = None
            if not pics:
                ok = False
                break
            group_pictures.extend(pics)

        if not ok or not group_pictures:
            failed += 1
            continue

        try:
            tasks_card = TaskCard(
                data_list=group,
                picturesList=group_pictures,
                webs=webs,
                sts=None,
                user_id=admin_id,
                chat_id=dump_channel_id,
                priority=priority,
                tasks_id=f"apost{admin_id}{idx}",
            )
            page_progress_cb = _group_progress
            doc = await send_manga_chapter(tasks_card, progress_callback=page_progress_cb)
        except Exception as e:
            logger.exception(e)
            doc = None

        if doc:
            file_ids.extend(m.id for m in doc)
            done += 1
        else:
            failed += 1

    if not file_ids:
        return await retry_on_flood(sts.edit)(
            "❌ Couldn't download any chapters. Check the bot's admin permissions "
            "in the Dump Channel, or try again."
        )

    await igrone_error(sts.edit)(f"<b>✅ Downloaded {done}/{total_groups} — building preview...</b>")

    title, genre, status, description = parse_manga_msg(bio_list)
    if status:
        description = f"<b>Status:</b> {status}\n\n{description}" if description else f"<b>Status:</b> {status}"
    if not description:
        description = "No description available."

    poster = bio_list.get("poster") or random.choice(Vars.PICS)

    post_id = _gen_post_id()
    _drafts[admin_id] = {
        "post_id": post_id,
        "title": title,
        "genre": genre,
        "description": description,
        "poster": poster,
        "dump_channel": dump_channel_id,
        "file_ids": file_ids,
        "template": DEFAULT_TEMPLATE,
    }

    caption = _build_caption(title, genre, description, DEFAULT_TEMPLATE)
    await igrone_error(sts.delete)()
    await retry_on_flood(client.send_photo)(
        chat_id, poster,
        caption="<b>🔎 Preview — auto-fetched, review before publishing:</b>\n\n" + caption,
        reply_markup=_preview_markup(DEFAULT_TEMPLATE),
    )
    if failed:
        await retry_on_flood(client.send_message)(
            chat_id, f"<i>⚠️ {failed} chapter group(s) failed to download and were skipped.</i>"
        )


# ---------------------------------------------------------------------------
# Delivery — called from /start when the deep link is ?start=get_<post_id>
# ---------------------------------------------------------------------------

@report_errors
async def deliver_post(client, message, post_id):
    post = get_post(post_id)
    if not post:
        return await retry_on_flood(message.reply_text)(
            "❌ This link is invalid or has expired.", quote=True
        )

    sts = await retry_on_flood(message.reply_text)("<code>📦 Fetching your file(s)...</code>", quote=True)
    dump_channel = post.get("dump_channel")
    file_ids = post.get("file_ids", [])
    if not dump_channel or not file_ids:
        return await retry_on_flood(sts.edit_text)("❌ No files are linked to this post. Please contact the admin.")

    sent = 0
    for msg_id in file_ids:
        try:
            await retry_on_flood(client.copy_message)(
                message.chat.id, _norm_channel(dump_channel), int(msg_id)
            )
            sent += 1
            await asyncio.sleep(1)
        except Exception as e:
            logger.exception(f"Failed delivering post {post_id} file {msg_id}: {e}")

    if not sent:
        return await retry_on_flood(sts.edit_text)("❌ Couldn't fetch the file(s). Please contact the admin.")

    bump_post_reads(post_id)
    await retry_on_flood(sts.edit_text)(
        f"✅ Sent {sent} file(s) for <b>{post.get('title', 'this manga')}</b>. Enjoy reading! 📖"
    )

    log_channel = get_log_channel()
    if log_channel:
        await igrone_error(client.send_message)(
            log_channel,
            f"📖 Post <code>{post_id}</code> (<b>{post.get('title')}</b>) delivered to "
            f"<code>{message.from_user.id}</code> [{message.from_user.mention()}]"
        )
