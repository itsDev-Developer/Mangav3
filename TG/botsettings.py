"""
Bot Settings
============
In-bot configuration for things that used to be env-var-only: Private Mode,
the link Shortener (on/off, API, duration), Force-Sub channel(s), and the
Log/Update channels. Each setting is stored via Tools.db's
get_config()/set_config() and falls back to the original Vars.<X> env value
until an admin changes it here — the same pattern already used for the
Post Channel / Dump Channel settings in /postsettings.

Post Channel / Dump Channel themselves stay in /postsettings (TG/post.py) —
this panel just links to it for discoverability.
"""

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import ListenerTimeout as TimeoutError

from bot import Bot, Vars, logger, load_fsb_vars
from Tools.db import (
    get_config, set_config, is_private_mode, get_log_channel,
    get_update_channel, is_shortener_enabled, get_shortener_api,
    get_duration, get_force_sub_channel,
)
from .storage import retry_on_flood, igrone_error, report_errors

CANCEL_WORDS = ("/cancel", "cancel")


def _onoff(value: bool) -> str:
    return "🟢 ON" if value else "🔴 OFF"


def _settings_text():
    fsb = get_force_sub_channel()
    return (
        "<b>⚙️ Bot Settings</b>\n\n"
        f"<b>🔒 Private Mode:</b> {_onoff(is_private_mode())}\n"
        f"<b>🔗 Shortener:</b> {_onoff(is_shortener_enabled())}\n"
        f"<b>⏱ Shortener duration:</b> <code>{get_duration()}h</code>\n"
        f"<b>📢 Force-Sub:</b> <code>{fsb or 'Not Set'}</code>\n"
        f"<b>📝 Log Channel:</b> <code>{get_log_channel() or 'Not Set'}</code>\n"
        f"<b>📰 Update Channel:</b> <code>{get_update_channel() or 'Not Set'}</code>\n\n"
        "<blockquote expandable>"
        "Every value here can be changed without restarting the bot. "
        "Post Channel / Dump Channel live in /postsettings instead.</blockquote>"
    )


def _settings_markup():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🔒 Private Mode: {_onoff(is_private_mode())}", callback_data="bs_toggle_private"),
        ],
        [
            InlineKeyboardButton(f"🔗 Shortener: {_onoff(is_shortener_enabled())}", callback_data="bs_toggle_shortener"),
        ],
        [
            InlineKeyboardButton("🔗 Set Shortener API", callback_data="bs_set_shortener_api"),
            InlineKeyboardButton("⏱ Set Duration", callback_data="bs_set_duration"),
        ],
        [InlineKeyboardButton("📢 Set Force-Sub Channel(s)", callback_data="bs_set_fsb")],
        [
            InlineKeyboardButton("📝 Set Log Channel", callback_data="bs_set_log"),
            InlineKeyboardButton("📰 Set Update Channel", callback_data="bs_set_update"),
        ],
        [InlineKeyboardButton("📮 Post / Dump Channel Settings ↗️", callback_data="pmpanel")],
        [InlineKeyboardButton("✖️ Close", callback_data="kclose")],
    ])


@Bot.on_message(filters.command(["botsettings", "bs"]) & filters.user(Vars.ADMINS))
@report_errors
async def bot_settings_cmd(client, message):
    await retry_on_flood(message.reply_text)(
        _settings_text(), quote=True, reply_markup=_settings_markup()
    )


@Bot.on_callback_query(filters.regex("^bspanel$") & filters.user(Vars.ADMINS))
@report_errors
async def bot_settings_panel_cb(client, query):
    await igrone_error(query.answer)()
    await retry_on_flood(query.edit_message_text)(
        _settings_text(), reply_markup=_settings_markup()
    )


@Bot.on_callback_query(filters.regex("^bs_toggle_private$") & filters.user(Vars.ADMINS))
@report_errors
async def bot_settings_toggle_private_cb(client, query):
    set_config("is_private", not is_private_mode())
    await igrone_error(query.answer)(f"Private Mode is now {_onoff(is_private_mode())}")
    await retry_on_flood(query.edit_message_text)(_settings_text(), reply_markup=_settings_markup())


@Bot.on_callback_query(filters.regex("^bs_toggle_shortener$") & filters.user(Vars.ADMINS))
@report_errors
async def bot_settings_toggle_shortener_cb(client, query):
    set_config("shortener", not is_shortener_enabled())
    await igrone_error(query.answer)(f"Shortener is now {_onoff(is_shortener_enabled())}")
    await retry_on_flood(query.edit_message_text)(_settings_text(), reply_markup=_settings_markup())


async def _ask_and_store(client, query, prompt, config_key, timeout=120, channel=False):
    """Shared "send a prompt, listen for one reply, store it" flow used by
    every text/channel setting below."""
    admin_id = query.from_user.id
    await retry_on_flood(query.edit_message_text)(
        f"{prompt}\n\nSend /cancel to abort."
    )
    try:
        call = await client.listen(
            user_id=admin_id, timeout=timeout,
            filters=(filters.text | filters.forwarded) if channel else filters.text,
        )
    except TimeoutError:
        return await retry_on_flood(query.message.edit_text)(
            "⏰ Timed out.", reply_markup=_settings_markup()
        )

    if call.text and call.text.strip().lower() in CANCEL_WORDS:
        await igrone_error(call.delete)()
        return await retry_on_flood(query.message.edit_text)(
            _settings_text(), reply_markup=_settings_markup()
        )

    value = None
    if channel and call.forward_from_chat:
        value = call.forward_from_chat.id
    elif call.text:
        text = call.text.strip()
        if channel:
            try:
                value = int(text)
            except ValueError:
                value = text
        else:
            value = text

    await igrone_error(call.delete)()

    if value is None:
        return await retry_on_flood(query.message.edit_text)(
            "❌ Didn't understand that.", reply_markup=_settings_markup()
        )

    set_config(config_key, value)
    return value


@Bot.on_callback_query(filters.regex("^bs_set_fsb$") & filters.user(Vars.ADMINS))
@report_errors
async def bot_settings_set_fsb_cb(client, query):
    await igrone_error(query.answer)()
    value = await _ask_and_store(
        client, query,
        "<b>📢 Send the Force-Sub channel(s)</b>\n\n"
        "Format: <code>ButtonText:username_or_id</code>, comma-separated for "
        "more than one, e.g. <code>Updates:mychannel,Backup:-1001234567890</code>.\n"
        "Send <code>none</code> to disable Force-Sub entirely.",
        "force_sub_channel",
    )
    if value is None:
        return
    if str(value).strip().lower() == "none":
        set_config("force_sub_channel", "")
        value = ""
    load_fsb_vars(Bot, value)  # apply immediately, no restart needed
    await retry_on_flood(query.message.edit_text)(
        f"✅ Force-Sub updated to <code>{value or 'disabled'}</code>.",
    )
    await retry_on_flood(query.message.edit_text)(_settings_text(), reply_markup=_settings_markup())


@Bot.on_callback_query(filters.regex("^bs_set_log$") & filters.user(Vars.ADMINS))
@report_errors
async def bot_settings_set_log_cb(client, query):
    await igrone_error(query.answer)()
    value = await _ask_and_store(
        client, query,
        "<b>📝 Send the Log Channel</b>\n\nForward a message from it, or send its ID/username.",
        "log_channel", channel=True,
    )
    if value is None:
        return
    await retry_on_flood(query.message.edit_text)(f"✅ Log Channel set to <code>{value}</code>.")
    await retry_on_flood(query.message.edit_text)(_settings_text(), reply_markup=_settings_markup())


@Bot.on_callback_query(filters.regex("^bs_set_update$") & filters.user(Vars.ADMINS))
@report_errors
async def bot_settings_set_update_cb(client, query):
    await igrone_error(query.answer)()
    value = await _ask_and_store(
        client, query,
        "<b>📰 Send the Update Channel</b>\n\nForward a message from it, or send its ID/username.",
        "update_channel", channel=True,
    )
    if value is None:
        return
    await retry_on_flood(query.message.edit_text)(f"✅ Update Channel set to <code>{value}</code>.")
    await retry_on_flood(query.message.edit_text)(_settings_text(), reply_markup=_settings_markup())


@Bot.on_callback_query(filters.regex("^bs_set_shortener_api$") & filters.user(Vars.ADMINS))
@report_errors
async def bot_settings_set_shortener_api_cb(client, query):
    await igrone_error(query.answer)()
    value = await _ask_and_store(
        client, query,
        "<b>🔗 Send the Shortener API URL</b>\n\nUse <code>{}</code> as the placeholder for the link to shorten.",
        "shortener_api",
    )
    if value is None:
        return
    await retry_on_flood(query.message.edit_text)("✅ Shortener API updated.")
    await retry_on_flood(query.message.edit_text)(_settings_text(), reply_markup=_settings_markup())


@Bot.on_callback_query(filters.regex("^bs_set_duration$") & filters.user(Vars.ADMINS))
@report_errors
async def bot_settings_set_duration_cb(client, query):
    await igrone_error(query.answer)()
    admin_id = query.from_user.id
    await retry_on_flood(query.edit_message_text)(
        "<b>⏱ Send the token duration in hours</b> (a number, e.g. <code>6</code>).\n\nSend /cancel to abort."
    )
    try:
        call = await client.listen(user_id=admin_id, timeout=120, filters=filters.text)
    except TimeoutError:
        return await retry_on_flood(query.message.edit_text)(
            "⏰ Timed out.", reply_markup=_settings_markup()
        )

    text = call.text.strip()
    await igrone_error(call.delete)()
    if text.lower() in CANCEL_WORDS:
        return await retry_on_flood(query.message.edit_text)(
            _settings_text(), reply_markup=_settings_markup()
        )

    try:
        hours = int(text)
    except ValueError:
        return await retry_on_flood(query.message.edit_text)(
            "❌ That's not a number.", reply_markup=_settings_markup()
        )

    set_config("duration", hours)
    await retry_on_flood(query.message.edit_text)(f"✅ Duration set to {hours}h.")
    await retry_on_flood(query.message.edit_text)(_settings_text(), reply_markup=_settings_markup())
