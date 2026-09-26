import asyncio
import random
import string
import time
from typing import Dict, Optional, Tuple
from bot import logger
from pyrogram.errors import FloodWait
from .db import uts, ensure_user, get_episode_number
from .img2pdf import thumbnali_images
from bot import Bot
from os import path as ospath
import pyrogram.errors


def progress_bar(pct: int, width: int = 10) -> str:
    """Tiny text progress bar, e.g. '▓▓▓▓▓░░░░░'."""
    pct = max(0, min(100, pct))
    filled = int(width * pct / 100)
    return "▓" * filled + "░" * (width - filled)


def make_throttled_progress(edit_func, label_fmt, min_interval: float = 2.0):
    """Wrap an `await edit_func(text)` call into a progress_callback(done, total)
    usable by download_and_convert_images() — throttled so it edits the
    Telegram message at most once every `min_interval` seconds (always fires
    on the final 100% update), instead of once per image, which would hit
    FloodWait almost immediately on any chapter with more than a few pages.
    """
    state = {"last": 0.0}

    async def _cb(done, total):
        now = time.monotonic()
        if done < total and (now - state["last"]) < min_interval:
            return
        state["last"] = now
        pct = int(done * 100 / total) if total else 100
        try:
            await edit_func(label_fmt.format(done=done, total=total, pct=pct, bar=progress_bar(pct)))
        except Exception:
            pass

    return _cb


def igrone_error(func, sync=False):
    async def wrapper(*args, **kwargs):
        try:
            if sync:
                return await asyncio.to_thread(func, *args, **kwargs)
            else:
                tasks = await asyncio.gather(*[func(*args, **kwargs)])
                return tasks[0]
        except Exception:
            return None
    
    return wrapper

def retry_on_flood(func):
    async def wrapper(*args, **kwargs):
        while True:
            try:
                return await func(*args, **kwargs)
            except FloodWait as e:
                logger.warning(f'FloodWait: waiting {e.value}s')
                await asyncio.sleep(e.value + 3)
            except (ValueError, pyrogram.errors.QueryIdInvalid, pyrogram.errors.MessageNotModified):
                return
            except(pyrogram.errors.exceptions.bad_request_400.WebpageCurlFailed, pyrogram.errors.exceptions.bad_request_400.WebpageMediaEmpty):
                raise
            except Exception as e:
                logger.exception(e)
                raise
    return wrapper

class Subscribes:
  """A class to manage user-specific subscriptions."""
  __slots__ = ('user_id', "manga_url", "web", "lastest_chapter", "manga_title")

  def __init__(self, manga_url: str, web: str, lastest_chapter: str, manga_title: str):
    self.manga_url: str = manga_url
    self.manga_title: str = manga_title
    self.web: str = web
    self.lastest_chapter: str = lastest_chapter
  
  def load_to_dict(self):
     return {"url": self.manga_url, "title": self.manga_title, "lastest_chapter": self.lastest_chapter}


def clean(txt, length=-1):
  txt = txt.replace("_", "").replace("&", "").replace(";", "")
  txt = txt.replace("None", "").replace(":", "").replace("'", "")
  txt = txt.replace("|", "").replace("*", "").replace("?", "")
  txt = txt.replace(">", "").replace("<", "").replace("`", "")
  txt = txt.replace("!", "").replace("@", "").replace("#", "")
  txt = txt.replace("$", "").replace("%", "").replace("^", "")
  txt = txt.replace("~", "").replace("+", "").replace("=", "")
  txt = txt.replace("/", "").replace("\\", "").replace("\n", "")
  txt = txt.replace(".jpg", "")
  if length != -1:
    txt = txt[:length]
  return txt



def get_file_name(data: list, setting: dict = {}):
    regex = setting.get('regex', None)
    
    flen = setting.get('file_name_len', "30")
    flen = int(flen) if flen else 30
    
    if len(data) > 1:
        episode_number1 = str(get_episode_number(data[0].get("title", "None")))
        if not episode_number1 or (episode_number1 == "None"):
            episode_number1 = clean(data[0].get("title", "None"))
        else:
            episode_number1 = episode_number1.zfill(int(regex)) if regex else episode_number1
        
        episode_number2 = str(get_episode_number(data[-1].get("title", "None")))
        if not episode_number2 or (episode_number2 == "None"):
            episode_number2 = clean(data[-1].get("title", "None"))
        else:
            episode_number2 = episode_number2.zfill(int(regex)) if regex else episode_number2
        
        episode_number = f"{episode_number1} - {episode_number2}"
    else:
        episode_number = str(get_episode_number(data[0].get("title", "None")))
        if (episode_number == "None") or not episode_number:
            episode_number = clean(data[0].get("title", "None"))
        else:
            episode_number = episode_number.zfill(int(regex)) if regex else episode_number

    manga_title = clean(data[0].get("manga_title", ""), flen)
    
    return manga_title, episode_number


async def load_images_(user_id, poster, base_url=None, poster_filename=None):
    """ 
    This Feature use for Banner , For Not Redownload Banner when user add multiple chapter in queue
    it will work for thumbnali, except CONSTANT
    """
    setting = {}
    user_setting: dict = uts[str(user_id)].get('setting', {})
    
    main_dir = f"Process/{user_id}"
    process = [
        ( "banner1_file_path", user_setting.get("banner1", None) ), 
        ( "banner2_file_path", user_setting.get("banner2", None) ),
        ( "thumb_file_name", user_setting.get("thumb", None) )
    ]
    for key, value in process:
        thumbnali = None
        if ospath.exists(f"{main_dir}/{key}.jpg"):
            thumbnali = f"{main_dir}/{key}.jpg"
            setting[key] = thumbnali
        
        elif value and value.startswith("http"):
            thumbnali = await igrone_error(thumbnali_images)(
                image_url=value, 
                download_dir=main_dir,
                file_name=key,
            )
            setting[key] = thumbnali
        elif value and value == "constant":
            thumbnali = await igrone_error(thumbnali_images)(
                image_url=poster, 
                download_dir=main_dir,
                file_name=poster_filename, base_url=base_url
            )
            setting[key] = thumbnali
            
        elif value:
            thumbnali = await igrone_error(Bot.download_media)(
                value,
                file_name=f"{main_dir}/{key}.jpg"
            )
            setting[key] = thumbnali
    
    return setting




class TaskCard:
    """ A class to manage user-specific tasks. """
    __slots__ = (
        "episode_number", "manga_title", "poster", "webs", 
        "picturesList", "sts", "url", "data", "tasks_id",
        "user_id", "chat_id", "priority", "setting",
    )
    
    def __init__(
        self, webs, sts, picturesList, 
        user_id, chat_id, priority, 
        tasks_id=None, data_list: list = [],
    ):
        ensure_user(user_id)
        self.picturesList = picturesList or []
        self.poster: str = data_list[0].get("poster", "") # data
        
        self.webs = webs
        self.sts = sts
        self.setting = uts[str(user_id)].get('setting', {})
        self.manga_title, self.episode_number = get_file_name(data_list, self.setting)
        
        if len(data_list) == 1:
            self.url = data_list[0].get("url", "") # data
        else:
            self.url = f"{data_list[0].get('url', '')} - {data_list[-1].get('url', '')}"

        self.tasks_id = tasks_id
        self.user_id = user_id
        self.chat_id = chat_id
        self.priority = priority

    async def get_banner(self) -> dict:
        tasks = await asyncio.gather(
            load_images_(
                self.user_id, 
                self.poster, 
                self.webs.url, 
                self.manga_title
            )
        )
        return tasks[0]
    
    def check_queue(self):
        if AQueue().get_count(int(self.user_id)) != 0:
            return False
        return True




class AQueue:
    """Singleton task queue. AQueue() always returns the SAME instance —
    this used to construct a brand new, always-empty queue every time
    TaskCard.check_queue() called AQueue() directly, so that check always
    reported "queue empty" regardless of real state. Making this a proper
    singleton fixes that without needing a circular import between
    Tools/base.py and TG/storage.py (which holds the module-level `queue =
    AQueue()` instance everything else uses).
    """
    __slots__ = ('storage_data', 'data_users', 'ongoing_tasks', 'maxsize', '_event', '_initialized')

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, maxsize: Optional[int] = None):
        if self._initialized:
            return
        self._initialized = True
        self.storage_data: Dict[str, Tuple[TaskCard, bool]] = {}  # {task_id: TaskCard, True}
        #self.data_users: Dict[int, List[str]] = {}  # {user_id: [task_ids]}
        self.ongoing_tasks: Dict[int, TaskCard] = {} # {user_id: TaskCard}
        self.maxsize = maxsize
        self._event = asyncio.Event()

    async def get_random_id(self) -> str:
        """Generate unique 7-char task ID"""
        chars = string.ascii_letters + string.digits + "s"
        while True:
            task_id = ''.join(random.choices(chars, k=7))
            if task_id not in self.storage_data and task_id not in self.ongoing_tasks:
                return task_id

    async def put(self, tasks: TaskCard, updates: bool = False) -> str:
        """Add task to queue"""
        
        if self.maxsize and len(self.storage_data) >= self.maxsize:
            raise asyncio.QueueFull("Queue full")

        tasks.tasks_id = await self.get_random_id()
        
        self.storage_data[tasks.tasks_id] = (tasks, updates)
        self._event.set()

        return tasks.tasks_id

    
    def get_available_tasks(self, user_id=None):
        """Return available tasks with better error handling"""
        tasks = [
            task_card
            for task_card in list(self.storage_data.values())
            if not self.get_ongoing_count(task_card[0].user_id)
            if not user_id or task_card[0].user_id == user_id
        ]
        sorted_tasks = sorted(tasks, key=lambda x: (x[0].priority != 0, tasks.index(x)))
        if not sorted_tasks:
            return None
        
        return sorted_tasks[0]
            
    async def get(self, worker_id: int) -> Tuple[TaskCard, bool]:
        """Get next available task. Event-driven instead of polling every
        0.1-0.9s in a tight loop — with (by default) 20 workers each doing
        that, the bot was waking up and re-scanning/re-sorting the whole
        queue roughly 10x/second per worker even while completely idle. Now
        a worker only wakes when put()/task_done() actually signals there
        may be something to do, plus an occasional safety-net timeout in
        case a signal is ever missed."""
        while True:
            try:
                available_task = self.get_available_tasks()

                if available_task:
                    self.ongoing_tasks[int(available_task[0].user_id)] = available_task[0]
                    del self.storage_data[available_task[0].tasks_id]
                    return available_task

                self._event.clear()
                try:
                    await asyncio.wait_for(self._event.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass  # periodic safety re-check

            except Exception as e:
                logger.exception(f"Error in queue get: {e}")
                await asyncio.sleep(1)

    
    async def delete_task(self, task_id: str) -> bool:
        """Delete specific task"""
        if task_id in self.storage_data:
            if self.storage_data[task_id][1] is not True:
                self.storage_data.pop(task_id)
                if task_id in self.ongoing_tasks:
                    del self.storage_data[task_id]
            return True
            
        return False
    
    async def delete_tasks(self, user_id: int) -> int:
        """Delete all tasks for user"""
        if user_id not in self.data_users:
            return 0

        deleted = 0
        user_queue = self.data_users[user_id]
        for task_id in user_queue.queue:
            if task_id in self.storage_data:
                if self.storage_data[task_id][1] is not True: # Clean up message if exists
                    chapter_card = self.storage_data.pop(task_id)
                    if chapter_card.sts:
                        try: await retry_on_flood(chapter_card.sts.delete)()
                        except Exception: pass
                    
                    deleted += 1
                
        return deleted

    def get_count(self, user_id: int = 0) -> int:
        """Get user's pending task count or total Users in queue"""
        try:
            if user_id == 0:
                # Count unique users with pending tasks
                unique_users = set()
                for task, is_completed in self.storage_data.values():
                    if not is_completed:
                        unique_users.add(int(task.user_id))
                return len(unique_users)
            else:
                user_id_str = str(user_id)  # Convert to string if stored as string
                return sum(1 for task, completed in self.storage_data.values() if str(task.user_id) == user_id_str)
                
        except (ValueError, TypeError, AttributeError) as e:
            logger.error(f"Error counting tasks for user {user_id}: {e}")
            return 0

    def task_exists(self, task_id: str) -> bool:
        """Check if task exists"""
        return task_id in self.storage_data or task_id in self.ongoing_tasks

    def qsize(self) -> int:
        return len(self.storage_data)

    def empty(self) -> bool:
        return not self.storage_data

    async def task_done(self, tasks_card: TaskCard) -> bool:
        """Mark task as done"""
        if tasks_card.user_id in self.ongoing_tasks:
            del self.ongoing_tasks[tasks_card.user_id]
            if tasks_card.tasks_id in self.storage_data:
                del self.storage_data[tasks_card.tasks_id]
            self._event.set()  # a slot freed up - another task may now be runnable
            return True
            
        return False

    def get_ongoing_count(self, user_id: int) -> int:
        """Get user's ongoing task count"""
        return sum(1 for t in self.ongoing_tasks.values() if int(t.user_id) == user_id)
