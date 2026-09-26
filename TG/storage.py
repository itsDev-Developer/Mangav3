from Webs import *

web_data = {
    #" Comick ": ComickWebs(),
    #" MangaMob ": MangaMobWebs(),
    " Asura Scans ": AsuraScansWebs(),
    #" Flame Comics": FlameComicsWebs(),
    #" Demonic Scans ": DemonicScansWebs(),
    " Manhua Fast ": ManhuaFastWebs(),
    " Weeb Central ": WeebCentralWebs(),
    " ManhwaClan ": ManhwaClanWebs(),
    " TempleToons ":TempleToonsWebs(),
    " Manhuaplus ": ManhuaplusWebs(),
    " Mgeko ": MgekoWebs(),
    " Manga18fx ": Manga18fxWebs(),
    " Manhwa18 ":  Manhwa18Webs(),
}


from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import asyncio
import pyrogram.errors
from pyrogram.errors import FloodWait
from bot import Vars
from loguru import logger
from pyrogram import filters
from Tools.base import AQueue, igrone_error, get_episode_number, retry_on_flood
from Tools.db import get_log_channel
from collections import OrderedDict


class BoundedCache(OrderedDict):
    """A plain dict that evicts its oldest entry once it grows past
    max_size, instead of growing forever for the life of the process.

    searchs/pagination/chaptersList/subscribes/post_targets used to be
    plain {} — every search result or manga info card anyone ever opened
    stayed cached in RAM permanently on a long-running bot. Stale entries
    were already handled gracefully everywhere ("This is an old button,
    please redo the search"), so capping these is safe: it just means a
    button older than ~2000 other interactions asks you to redo the search
    instead of working forever.
    """
    def __init__(self, max_size=2000):
        super().__init__()
        self.max_size = max_size

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.max_size:
            self.popitem(last=False)


async def call_iter_chapters(webs, data, page=1, vol=None):
    """Call a scraper's iter_chapters() exactly once, whether it's defined
    as sync (every current site module) or async. The old call sites did
    `try: await webs.iter_chapters(...) except TypeError: webs.iter_chapters(...)`
    which, for every sync implementation (all of them today), ran the full
    BeautifulSoup parse TWICE per call — once to fail the await, once more
    in the except branch. This calls it once and awaits only if it's really
    a coroutine.
    """
    if vol is not None:
        result = webs.iter_chapters(data, vol=vol, page=page)
    else:
        result = webs.iter_chapters(data, page=page)
    if asyncio.iscoroutine(result):
        result = await result
    return result


def _chat_id_of(update):
    msg = getattr(update, "message", None)
    if msg is not None and getattr(msg, "chat", None) is not None:
        return msg.chat.id
    chat = getattr(update, "chat", None)
    return chat.id if chat is not None else None


def report_errors(func):
    """Safety net for admin-facing handlers: any exception that isn't
    already caught gets shown to the admin (with the real message) instead
    of just vanishing into the logs, which is what made earlier bugs here
    look like a feature was silently "not working"."""
    import functools

    @functools.wraps(func)
    async def wrapper(client, update, *args, **kwargs):
        try:
            return await func(client, update, *args, **kwargs)
        except Exception as e:
            logger.exception(e)
            chat_id = _chat_id_of(update)
            if chat_id:
                await igrone_error(client.send_message)(
                    chat_id, f"❌ <b>Something went wrong:</b>\n<code>{e}</code>"
                )
    return wrapper


queue = AQueue()
searchs = BoundedCache(2000)
backs = {}
chaptersList = BoundedCache(3000)
queueList = {}
pagination = BoundedCache(2000)
subscribes = BoundedCache(2000)
post_targets = BoundedCache(500)  # manga info cache for the admin-only "📤 Post to Channel" button


web_data = dict(sorted(web_data.items()))
plugins_name = " ".join(web_data[i].sf for i in web_data)

def split_list(li):
    return [li[x:x + 2] for x in range(0, len(li), 2)]

def check_get_web(url):
    for web in web_data.values():
        if url.startswith(web.url):
            return web



def is_auth_query():
    async def func(flt, _, query):
        reply = query.message.reply_to_message
        if not reply:
            return True
        
        if not reply.from_user:
            return False
        
        user_id = reply.from_user.id
        query_user_id = query.from_user.id
        if user_id != query_user_id:
            await query.answer("This is not for you", show_alert=True)
            return False
        return True
    
    return filters.create(func)


def plugins_list(type=None, page=1):
    button = []
    if type and type == "updates":
        for i in web_data.keys():
            c = web_data[i].sf
            c = f"udat_{c}"
            button.append(InlineKeyboardButton(i, callback_data=c))
    elif type and type == "gens":
        for i in web_data.keys():
            c = web_data[i].sf
            c = f"gens_{c}"
            button.append(InlineKeyboardButton(i, callback_data=c))
    elif type and type == "subs":
        for i in web_data.keys():
            c = web_data[i].sf
            c = f"isubs_{c}"
            button.append(InlineKeyboardButton(i, callback_data=c))
    else:
        for i in web_data.keys():
            c = web_data[i].sf
            c = f"plugin_{c}"
            button.append(InlineKeyboardButton(i, callback_data=c))

    button = button[len(button)//2:len(button)] if page != 1 else button[:len(button)//2]
    button = split_list(button)
    button.append([
        InlineKeyboardButton(" >> ", callback_data="bk.p:2") if page == 1 else InlineKeyboardButton(" << ", callback_data="bk.p:1")
    ])
    button.append([
        InlineKeyboardButton("♞ All Search ♞", callback_data="plugin_all"),
        InlineKeyboardButton("🔥 Close 🔥", callback_data="kclose")
    ])
    return InlineKeyboardMarkup(button)

def get_webs(sf):
    return next((web for web in web_data.values() if web.sf == sf), None)



async def check_fsb(client, message):
    channel_button = []
    
    for channel_info in client.FSB:
        try:
            channel = int(channel_info[1])
        except:
            channel = channel_info[1]

        try:
            await client.get_chat_member(channel, message.from_user.id)
        except pyrogram.errors.UserNotParticipant:
            channel_link = channel_info[2] if len(channel_info) > 2 else (
                await client.export_chat_invite_link(channel) if isinstance(channel, int) 
                else f"https://telegram.me/{channel.strip()}"
            )
            channel_button.append(InlineKeyboardButton(channel_info[0], url=channel_link))
        except (pyrogram.errors.UsernameNotOccupied, pyrogram.errors.ChatAdminRequired) as e:
            await retry_on_flood(client.send_message)(
                get_log_channel(), f"Channel issue: {channel} - {type(e).__name__}"
            )
        except (pyrogram.ContinuePropagation, pyrogram.StopPropagation):
            raise
        except Exception as e:
            await retry_on_flood(client.send_message)(
                get_log_channel(), f"Force Subscribe error: {e} at {channel}"
            )

    return channel_button, []


# Optimized utility functions
def clean(txt, length=-1):
    """Clean text by removing special characters"""
    remove_chars = "_&;:None'|*?><`!@#$%^~+=\\/\n"
    for char in remove_chars:
        txt = txt.replace(char, "")
    return txt[:length] if length != -1 else txt

