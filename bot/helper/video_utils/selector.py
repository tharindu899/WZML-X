from __future__ import annotations
from aiofiles.os import path as aiopath
from ast import literal_eval
from asyncio import Event, wait_for, gather
from functools import partial
from os import path as ospath
from PIL import Image
from pyrogram.filters import regex, user
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import Message, CallbackQuery
from re import match as re_match
from time import time

from . import VID_MODE
from ..ext_utils.bot_utils import new_task, sync_to_async
from ..ext_utils.files_utils import clean_target
from ..ext_utils.links_utils import is_media
from ..ext_utils.status_utils import get_readable_time
from ..telegram_helper.button_build import ButtonMaker
from ..telegram_helper.filters import CustomFilters
from ..telegram_helper.message_utils import send_message, edit_message, delete_message
from ...core.config_manager import Config


class SelectMode:
    def __init__(self, listener, is_link=False):
        self._is_link = is_link
        self._time = time()
        self._reply = None
        self.listener = listener
        self.is_rename = False
        self.mode = ""
        self.extra_data = {}
        self.newname = ""
        self.event = Event()
        self.message_event = Event()
        self.is_cancelled = False

    async def _event_handler(self):
        pfunc = partial(cb_vidtools, obj=self)
        handler = self.listener.client.add_handler(
            CallbackQueryHandler(
                pfunc, filters=regex("^vidtool") & user(self.listener.user_id)
            ),
            group=-1,
        )
        try:
            await wait_for(self.event.wait(), timeout=180)
        except Exception:
            self.mode = "Task has been cancelled, time out!"
            self.is_cancelled = True
            self.event.set()
        finally:
            self.listener.client.remove_handler(*handler)

    @new_task
    async def message_event_handler(self, mode=""):
        pfunc = partial(message_handler, obj=self, is_sub=mode == "subfile")
        handler = self.listener.client.add_handler(
            MessageHandler(pfunc, user(self.listener.user_id)), group=1
        )
        try:
            await wait_for(self.message_event.wait(), timeout=60)
        except Exception:
            self.message_event.set()
        finally:
            self.listener.client.remove_handler(*handler)
            self.message_event.clear()

    async def _send_message(self, text, buttons):
        if not self._reply:
            self._reply = await send_message(self.listener.message, text, buttons)
        else:
            await edit_message(self._reply, text, buttons)

    def _captions(self, mode=None):
        msg = "<b>VIDEO TOOLS SETTINGS</b>"
        if vidmode := VID_MODE.get(self.mode):
            msg += f"\nMode: <b>{vidmode}</b>"
        msg += f'\nName: <b>{self.newname or "Default"}</b>'
        if self.extra_data and self.mode == "trim":
            msg += f"\nTrim Duration: <b>{list(self.extra_data.values())}</b>"
        if self.mode in ("vid_sub", "watermark"):
            hardsub = self.extra_data.get("hardsub")
            msg += f"\nHardsub Mode: <b>{'Enable' if hardsub else 'Disable'}</b>"
            if hardsub:
                msg += f"\nBold Style: <b>{'Enable' if self.extra_data.get('boldstyle') else 'Disable'}</b>"
                if fontname := self.extra_data.get("fontname") or Config.HARDSUB_FONT_NAME:
                    msg += f'\nFont Name: <b>{fontname.replace("_", " ")}</b>'
                if fontsize := self.extra_data.get("fontsize") or Config.HARDSUB_FONT_SIZE:
                    msg += f"\nFont Size: <b>{fontsize}</b>"
                if fontcolour := self.extra_data.get("fontcolour"):
                    msg += f"\nFont Colour: <b>{fontcolour}</b>"
        if quality := self.extra_data.get("quality"):
            msg += f"\nQuality: <b>{quality}</b>"
        if self.mode == "watermark" and (wmsize := self.extra_data.get("wmsize")):
            msg += f"\nWM Size: <b>{wmsize}</b>"
            if wmposition := self.extra_data.get("wmposition"):
                pos_dict = {
                    "5:5": "Top Left",
                    "main_w-overlay_w-5:5": "Top Right",
                    "5:main_h-overlay_h": "Bottom Left",
                    "w-overlay_w-5:main_h-overlay_h-5": "Bottom Right",
                }
                msg += f"\nWM Position: <b>{pos_dict[wmposition]}</b>"
            if popupwm := self.extra_data.get("popupwm"):
                msg += f"\nDisplay: <b>{popupwm}x/20s</b>"
        if self.mode == "subsync" and (typee := self.extra_data.get("type")):
            msg += f"\nSync Mode: <b>{typee.replace('sync_', '').title()}</b>"
        match mode:
            case "rename":
                msg += "\n\n<i>Send valid name with extension...</i>"
            case "watermark":
                msg += "\n\n<i>Send valid image to set as watermark...</i>"
            case "subfile":
                msg += "\n\n<i>Send valid subtitle (.ass or .srt) for hardsub...</i>"
            case "wmsize":
                msg += "\n\n<i>Choose watermark size</i>"
            case "fontsize":
                msg += (
                    "\n\n<i>Choose font size</i>\n"
                    "<b>Recommended:</b>\n"
                    "1080p: <b>21-26 </b>\n"
                    "720p: <b>16-21</b>\n"
                    "480p: <b>11-16</b>"
                )
            case "trim":
                msg += "\n\n<i>Send valid trim duration <b>hh:mm:ss hh:mm:ss</b></i>"
        msg += f"\n\n<i>Time Out: {get_readable_time(180 - (time() - self._time))}</i>"
        return msg

    async def list_buttons(self, mode=""):
        buttons, bnum = ButtonMaker(), 2
        disabled = (Config.DISABLE_VIDTOOLS or "").split()
        if not mode:
            vid_modes = dict(list(VID_MODE.items())[4:]) if self._is_link else VID_MODE
            for key, value in vid_modes.items():
                if key in disabled:
                    continue
                buttons.data_button(
                    f"{'✅ ' if self.mode == key else ''}{value}", f"vidtool {key}"
                )
            buttons.data_button(
                f"{'✅ ' if self.newname else ''}Rename", "vidtool rename", "header"
            )
            buttons.data_button("Cancel", "vidtool cancel", "footer")
            if self.mode:
                buttons.data_button("Done", "vidtool done", "footer")
            if self.mode in ("vid_sub", "watermark") and await CustomFilters.sudo(
                "", self.listener.message
            ):
                hardsub = self.extra_data.get("hardsub")
                buttons.data_button(
                    f"{'✅ ' if hardsub else ''}Hardsub", "vidtool hardsub", "header"
                )
                if hardsub:
                    buttons.data_button("Font Style", "vidtool fontstyle", "header")

            if self.mode == "vid_sub" or (
                self.mode == "watermark" and self.extra_data.get("hardsub")
            ):
                buttons.data_button(
                    f"{'✅ ' if await aiopath.exists(self.extra_data.get('subfile', '')) else ''}Sub File",
                    "vidtool subfile",
                    "header",
                )

            if self.mode in ("compress", "watermark") or self.extra_data.get("hardsub"):
                buttons.data_button("Quality", "vidtool quality", "header")
            if self.mode == "watermark":
                buttons.data_button("Popup", "vidtool popupwm", "header")
        else:

            def _buttons_style(name=True, size=True, colour=True, position="header", cb="fontstyle"):
                if name:
                    buttons.data_button("Font Name", "vidtool fontstyle fontname", position)
                if size:
                    buttons.data_button("Font Size", "vidtool fontstyle fontsize", position)
                if colour:
                    buttons.data_button("Font Colour", "vidtool fontstyle fontcolour", position)
                buttons.data_button("<<", f"vidtool {cb}", "footer")
                buttons.data_button("Done", "vidtool done", "footer")

            match mode:
                case "subsync":
                    buttons.data_button("Manual", "vidtool sync_manual")
                    buttons.data_button("Auto", "vidtool sync_auto")
                case "quality":
                    bnum = 3
                    for key in ["1080p", "720p", "540p", "480p", "360p"]:
                        buttons.data_button(
                            f"{'✅ ' if self.extra_data.get('quality') == key else ''}{key}",
                            f"vidtool quality {key}",
                        )
                    buttons.data_button("<<", "vidtool back", "footer")
                    buttons.data_button("Done", "vidtool done", "footer")
                case "popupwm":
                    bnum = 5
                    popupwm = self.extra_data.get("popupwm", 0)
                    if popupwm:
                        buttons.data_button("Reset", "vidtool popupwm 0", "header")
                    for key in range(2, 21, 2):
                        buttons.data_button(
                            f"{'✅ ' if popupwm == key else ''}{key}",
                            f"vidtool popupwm {key}",
                        )
                    buttons.data_button("<<", "vidtool back", "footer")
                    buttons.data_button("Done", "vidtool done", "footer")
                case "wmsize":
                    bnum = 3
                    for btn in [5, 10, 15, 20, 25, 30]:
                        buttons.data_button(str(btn), f"vidtool wmsize {btn}")
                case "fontstyle":
                    bnum = 3
                    _buttons_style(position=None, cb="back")
                    boldstyle = self.extra_data.get("boldstyle")
                    buttons.data_button(
                        f"{'✅ ' if boldstyle else ''}Bold Style",
                        f"vidtool fontstyle boldstyle {bool(boldstyle)}",
                        "header",
                    )
                case "fontname":
                    _buttons_style(name=False)
                    for btn in [
                        "Arial", "Impact", "Verdana", "Consolas",
                        "DejaVu_Sans", "Comic_Sans_MS", "Simple_Day_Mistu",
                    ]:
                        buttons.data_button(
                            f"{'✅ ' if btn == self.extra_data.get('fontname') else ''}{btn.replace('_', ' ')}",
                            f"vidtool fontstyle fontname {btn}",
                        )
                case "fontsize":
                    bnum = 5
                    _buttons_style(size=False)
                    for btn in range(11, 31):
                        buttons.data_button(
                            f"{'✅ ' if str(btn) == self.extra_data.get('fontsize') else ''}{btn}",
                            f"vidtool fontstyle fontsize {btn}",
                        )
                case "fontcolour":
                    bnum = 3
                    _buttons_style(colour=False)
                    colours = [
                        ("Red", "0000ff"), ("Green", "00ff00"), ("Blue", "ff0000"),
                        ("Yellow", "00ffff"), ("Orange", "0054ff"), ("Purple", "005aff"),
                        ("Soft Red", "d470ff"), ("Soft Green", "80ff80"),
                        ("Soft Blue", "ffb84d"), ("Soft Yellow", "80ffff"),
                    ]
                    for btn, hexcolour in colours:
                        buttons.data_button(
                            f"{'✅ ' if hexcolour == self.extra_data.get('fontcolour') else ''}{btn}",
                            f"vidtool fontstyle fontcolour {hexcolour}",
                        )
                case "wmposition":
                    buttons.data_button("Top Left", "vidtool wmposition 5:5")
                    buttons.data_button(
                        "Top Right", "vidtool wmposition main_w-overlay_w-5:5"
                    )
                    buttons.data_button(
                        "Bottom Left", "vidtool wmposition 5:main_h-overlay_h"
                    )
                    buttons.data_button(
                        "Bottom Right",
                        "vidtool wmposition w-overlay_w-5:main_h-overlay_h-5",
                    )
                case _:
                    buttons.data_button("<<", "vidtool back", "footer")

        await self._send_message(self._captions(mode), buttons.build_menu(bnum, 3))

    async def get_buttons(self):
        task = self._event_handler()
        await gather(self.list_buttons(), task)
        if self.is_cancelled:
            await edit_message(self._reply, self.mode)
            return None
        await delete_message(self._reply)
        return [self.mode, self.newname, self.extra_data]


async def message_handler(_, message: Message, obj: SelectMode, is_sub=False):
    data = None
    if obj.is_rename and message.text:
        obj.newname = message.text.strip().replace("/", "")
        obj.is_rename = False
    elif obj.mode in ("watermark", "vid_sub") and (media := is_media(message)):
        if is_sub:
            if message.document and not media.file_name.lower().endswith((".ass", ".srt")):
                await send_message(message, "Only .ass or .srt allowed!")
                return
            # Keep the original filename (prefixed with file_id to avoid
            # collisions) instead of dropping it, since language-detection
            # for the muxed subtitle relies on tokens in the original name
            # (e.g. "Lanterns_S01E01_Sinhala.srt" -> Sinhala).
            orig_name = getattr(media, "file_name", "") or f"{media.file_id}.srt"
            obj.extra_data["subfile"] = await message.download(
                ospath.join("watermark", f"{media.file_id}_{orig_name}")
            )
        else:
            if message.document and "image" not in getattr(media, "mime_type", "None"):
                await send_message(message, "Only image document allowed!")
                return
            fpath = await message.download(ospath.join("watermark", media.file_id))
            await sync_to_async(
                Image.open(fpath).convert("RGBA").save,
                ospath.join("watermark", f"{obj.listener.mid}.png"),
                "PNG",
            )
            await clean_target(fpath)
            data = "wmsize"
    elif obj.mode == "trim" and message.text:
        if match := re_match(r"(\d{2}:\d{2}:\d{2})\s(\d{2}:\d{2}:\d{2})", message.text.strip()):
            obj.extra_data.update({"start_time": match.group(1), "end_time": match.group(2)})
        else:
            await send_message(message, "Invalid trim duration format!")
            return
    obj.message_event.set()
    await gather(obj.list_buttons(data), delete_message(message))


@new_task
async def cb_vidtools(_, query: CallbackQuery, obj: SelectMode):
    data = query.data.split()
    disabled = (Config.DISABLE_VIDTOOLS or "").split()
    if data[1] in disabled:
        await query.answer(f"{VID_MODE.get(data[1], data[1])} has been disabled!", True)
        return
    await query.answer()
    if data[1] == obj.mode:
        return
    match data[1]:
        case "done":
            obj.event.set()
        case "back":
            if obj.message_event:
                obj.message_event.set()
            await obj.list_buttons()
        case "cancel":
            obj.mode = "Task has been cancelled!"
            obj.is_cancelled = True
            obj.event.set()
        case "quality" | "popupwm" as value:
            if len(data) == 3:
                obj.extra_data[value] = data[2] if value == "quality" else int(data[2])
            await obj.list_buttons(value)
        case "hardsub":
            hmode = not bool(obj.extra_data.get("hardsub"))
            if not hmode and obj.mode == "vid_sub":
                for key in ("fontname", "fontsize", "fontcolour", "boldstyle"):
                    obj.extra_data.pop(key, None)
            obj.extra_data["hardsub"] = hmode
            await obj.list_buttons()
        case "subfile":
            task = obj.message_event_handler("subfile")
            await gather(obj.list_buttons("subfile"), task)
        case "fontstyle":
            mode = "fontstyle"
            if len(data) > 2:
                mode = data[2]
                is_bold = mode == "boldstyle"
                if len(data) == 4:
                    if not is_bold and obj.extra_data.get(mode) == data[3]:
                        return
                    obj.extra_data[mode] = (
                        not literal_eval(data[3]) if is_bold else data[3]
                    )
                if is_bold:
                    mode = "fontstyle"
            await obj.list_buttons(mode)
        case "sync_manual" | "sync_auto" as value:
            obj.extra_data["type"] = value
            await obj.list_buttons()
        case "wmsize" | "wmposition" as value:
            obj.extra_data[value] = data[2]
            await obj.list_buttons("wmposition" if value == "wmsize" else None)
        case value:
            if value == "rename":
                obj.is_rename = True
            else:
                obj.mode = value
                obj.extra_data.clear()
            if value in ["watermark", "rename", "trim"]:
                task = obj.message_event_handler(value)
                await gather(obj.list_buttons(value), task)
                return
            await obj.list_buttons("subsync" if value == "subsync" else "")
