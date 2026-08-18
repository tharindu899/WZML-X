from __future__ import annotations
from ast import literal_eval
from asyncio import Event, wait_for, gather
from functools import partial
from pyrogram.filters import regex, user
from pyrogram.handlers import CallbackQueryHandler
from pyrogram.types import CallbackQuery
from time import time

from . import VID_MODE
from ..ext_utils.bot_utils import new_task
from ..ext_utils.status_utils import get_readable_file_size, get_readable_time
from ..telegram_helper.button_build import ButtonMaker
from ..telegram_helper.message_utils import send_message, edit_message, delete_message


class ExtraSelect:
    def __init__(self, executor):
        self._listener = executor.listener
        self._time = time()
        self._reply = None
        self.executor = executor
        self.event = Event()
        self.is_cancel = False
        self.extension = [None, None, "mkv"]
        self.status = ""

    @new_task
    async def _event_handler(self):
        pfunc = partial(cb_extra, obj=self)
        handler = self._listener.client.add_handler(
            CallbackQueryHandler(
                pfunc, filters=regex("^extra") & user(self._listener.user_id)
            ),
            group=-1,
        )
        try:
            await wait_for(self.event.wait(), timeout=180)
        except Exception:
            self.event.set()
        finally:
            self._listener.client.remove_handler(*handler)

    async def update_message(self, text, buttons):
        if not self._reply:
            self._reply = await send_message(self._listener.message, text, buttons)
        else:
            await edit_message(self._reply, text, buttons)

    def streams_select(self, streams=None):
        buttons = ButtonMaker()
        if not self.executor.data:
            self.executor.data = {"stream": {}, "sdata": []}
            for stream in streams:
                indexmap = stream.get("index")
                codec_name = stream.get("codec_name")
                codec_type = stream.get("codec_type")
                lang = stream.get("tags", {}).get("language")
                if not lang:
                    lang = str(indexmap)
                if codec_type not in ("video", "audio", "subtitle"):
                    continue
                if codec_type == "audio":
                    self.executor.data["is_audio"] = True
                elif codec_type == "subtitle":
                    self.executor.data["is_sub"] = True
                self.executor.data["stream"][indexmap] = {
                    "info": f"{codec_type.title()} ~ {lang.upper()}",
                    "name": codec_name,
                    "map": indexmap,
                    "type": codec_type,
                    "lang": lang,
                }
        mode, ddict = self.executor.mode, self.executor.data
        text = ""
        for key, value in ddict["stream"].items():
            if mode == "extract":
                buttons.data_button(value["info"], f"extra {mode} {key}")
                audext, subext, vidext = self.extension
                text = (
                    f"<b>STREAM EXTRACT SETTINGS ~ {self._listener.tag}</b>\n"
                    f"<code>{self.executor.name}</code>\n"
                    f"<b></b>File Size: <b>{get_readable_file_size(self.executor.size)}</b>\n"
                    f"<b></b>Video Format: <b>{vidext.upper()}</b>\n"
                    f"<b></b>Audio Format: <b>{audext.upper()}</b>\n"
                    f"<b></b>Subtitle Format: <b>{subext.upper()}</b>\n"
                    f"<b></b>Alternative Mode: <b>{'✅ Enable' if ddict.get('alt_mode') else 'Disable'}</b>\n\n"
                    "Select available stream below to unpack!"
                )
            else:
                if value["type"] != "video":
                    buttons.data_button(value["info"], f"extra {mode} {key}")
                text = (
                    f"<b>STREAM REMOVE SETTINGS ~ {self._listener.tag}</b>\n"
                    f"<code>{self.executor.name}</code>\n"
                    f"File Size: <b>{get_readable_file_size(self.executor.size)}</b>\n"
                )
                if sdata := ddict.get("sdata"):
                    text += "\nStream will removed:\n"
                    for i, sindex in enumerate(sdata, start=1):
                        text += f"{i}. {ddict['stream'][sindex]['info']}\n".replace("✅ ", "")
                text += "\nSelect available stream below!"
        if mode == "extract":
            buttons.data_button(
                "✅ ALT Mode" if ddict.get("alt_mode") else "ALT Mode",
                f"extra {mode} alt {ddict.get('alt_mode', False)}",
                "footer",
            )
        if ddict.get("is_sub"):
            buttons.data_button("All Subs", f"extra {mode} subtitle")
        if ddict.get("is_audio"):
            buttons.data_button("All Audio", f"extra {mode} audio")
        buttons.data_button("Cancel", "extra cancel", "footer")
        if mode == "extract":
            for ext in self.extension:
                buttons.data_button(ext.upper(), f"extra {mode} extension {ext}", "header")
            buttons.data_button("Extract All", f"extra {mode} video audio subtitle")
        else:
            buttons.data_button("Reset", f"extra {mode} reset", "header")
            buttons.data_button("Reverse", f"extra {mode} reverse", "header")
            buttons.data_button("Continue", f"extra {mode} continue", "footer")
        text += f"\n\n<i>Time Out: {get_readable_time(180 - (time() - self._time))}</i>"
        return text, buttons.build_menu(2)

    async def compress_select(self, streams):
        self.executor.data = {}
        buttons = ButtonMaker()
        for stream in streams:
            indexmap = stream.get("index")
            codec_type = stream.get("codec_type")
            lang = stream.get("tags", {}).get("language") or str(indexmap)
            if codec_type == "video" and "video" not in self.executor.data:
                self.executor.data["video"] = indexmap
            if codec_type == "audio":
                buttons.data_button(f"Audio ~ {lang.upper()}", f"extra compress {indexmap}")
        buttons.data_button("Continue", "extra compress 0")
        buttons.data_button("Cancel", "extra cancel")
        await self.update_message(
            f"{self._listener.tag}, Select available audio or press <b>Continue (no audio)</b>.\n"
            f"<code>{self.executor.name}</code>",
            buttons.build_menu(2),
        )

    async def rmstream_select(self, streams):
        self.executor.data = {}
        await self.update_message(*self.streams_select(streams))

    async def convert_select(self, streams):
        buttons = ButtonMaker()
        hvid = "1080p"
        resolution = {
            "1080p": "Convert 1080p", "720p": "Convert 720p", "540p": "Convert 540p",
            "480p": "Convert 480p", "360p": "Convert 360p",
        }
        for stream in streams:
            if stream["codec_type"] == "video":
                vid_height = f'{stream["height"]}p'
                if vid_height in resolution:
                    hvid = vid_height
                break
        keys = list(resolution)
        for key in keys[keys.index(hvid) + 1:]:
            buttons.data_button(resolution[key], f"extra convert {key}")
        buttons.data_button("Cancel", "extra cancel", "footer")
        await self.update_message(
            f"{self._listener.tag}, Select available resolution to convert.\n<code>{self.executor.name}</code>",
            buttons.build_menu(2),
        )

    async def subsync_select(self):
        buttons = ButtonMaker()
        text = ""
        index = 1
        if not self.status:
            for position, file in self.executor.data["list"].items():
                if file.endswith((".srt", ".ass")):
                    ref_file = self.executor.data["final"].get(position, {}).get("ref", "")
                    text += f'{index}. {file} {"✅ " if ref_file else ""}\n'
                    but_txt = f"✅ {index}" if ref_file else str(index)
                    buttons.data_button(but_txt, f"extra subsync {position}")
                    index += 1
            buttons.data_button("Cancel", "extra cancel", "footer")
            if self.executor.data["final"]:
                buttons.data_button("Continue", "extra subsync continue", "footer")
        else:
            file = self.executor.data["list"][self.status]
            ref = self.executor.data["final"].get(self.status, {}).get("ref")
            text = f"Current: <b>{file}</b>\n"
            if ref:
                text += f"References: <b>{ref}</b>\n"
            text += "\nSelect Available References Below!\n"
            self.executor.data["final"][self.status] = {"file": file}
            for position, file in self.executor.data["list"].items():
                if position != self.status and file not in self.executor.data["final"].values():
                    text += f"{index}. {file}\n"
                    buttons.data_button(str(index), f"extra subsync select {position}")
                    index += 1
        await self.update_message(text, buttons.build_menu(5))

    async def extract_select(self, streams):
        self.executor.data = {}
        ext = [None, None, "mkv"]
        for stream in streams:
            codec_name, codec_type = stream.get("codec_name"), stream.get("codec_type")
            if codec_type == "audio" and not ext[0]:
                match codec_name:
                    case "mp3":
                        ext[0] = "ac3"
                    case "aac" | "ac3" | "eac3" | "m4a" | "mka" | "wav" as value:
                        ext[0] = value
                    case _:
                        ext[0] = "aac"
            elif codec_type == "subtitle" and not ext[1]:
                ext[1] = "srt" if codec_name == "subrip" else "ass"
        if not ext[0]:
            ext[0] = "aac"
        if not ext[1]:
            ext[1] = "srt"
        self.extension = ext
        await self.update_message(*self.streams_select(streams))

    async def get_buttons(self, *args):
        task = self._event_handler()
        if extra_mode := getattr(self, f"{self.executor.mode}_select", None):
            await extra_mode(*args)
        await task
        self.executor.event.set()
        await delete_message(self._reply)
        if self.is_cancel:
            self._listener.is_cancelled = True
            await self._listener.on_upload_error(
                f"{VID_MODE.get(self.executor.mode, self.executor.mode)} stopped by user!"
            )


async def cb_extra(_, query: CallbackQuery, obj: ExtraSelect):
    data = query.data.split()
    match data[1]:
        case "cancel":
            await query.answer()
            obj.is_cancel = obj.executor.is_cancel = True
            obj.executor.data = None
            obj.event.set()
        case "subsync":
            if data[2].isdigit():
                obj.status = int(data[2])
            elif data[2] == "select":
                obj.executor.data["final"][obj.status]["ref"] = obj.executor.data["list"][int(data[3])]
                obj.status = ""
            elif data[2] == "continue":
                obj.event.set()
                return
            await gather(query.answer(), obj.subsync_select())
        case "compress":
            await query.answer()
            obj.executor.data["audio"] = int(data[2])
            obj.event.set()
        case "convert":
            await query.answer()
            obj.executor.data = data[2]
            obj.event.set()
        case "rmstream":
            ddict = obj.executor.data
            match data[2]:
                case "reset":
                    if sdata := ddict["sdata"]:
                        await query.answer()
                        for mapindex in sdata:
                            info = ddict["stream"][mapindex]["info"]
                            ddict["stream"][mapindex]["info"] = info.replace("✅ ", "")
                        sdata.clear()
                        await obj.update_message(*obj.streams_select())
                    else:
                        await query.answer("No any selected stream to reset!", True)
                case "continue":
                    if ddict["sdata"]:
                        await query.answer()
                        obj.event.set()
                    else:
                        await query.answer("Please select at least one stream!", True)
                case "audio" | "subtitle" as value:
                    await query.answer()
                    obj.executor.data["key"] = value
                    obj.event.set()
                case "reverse":
                    if ddict["sdata"]:
                        await query.answer()
                        new_sdata = [
                            x for x in ddict["stream"] if x not in ddict["sdata"] and x != 0
                        ]
                        for key, value in ddict["stream"].items():
                            info = value["info"]
                            ddict["stream"][key]["info"] = (
                                f"✅ {info}" if key in new_sdata else info.replace("✅ ", "")
                            )
                        ddict["sdata"] = new_sdata
                        await obj.update_message(*obj.streams_select())
                    else:
                        await query.answer("No any selected stream to reverse!", True)
                case value:
                    await query.answer()
                    mapindex = int(value)
                    info = ddict["stream"][mapindex]["info"]
                    if mapindex in ddict["sdata"]:
                        ddict["sdata"].remove(mapindex)
                        ddict["stream"][mapindex]["info"] = info.replace("✅ ", "")
                    else:
                        ddict["sdata"].append(mapindex)
                        ddict["stream"][mapindex]["info"] = f"✅ {info}"
                    await obj.update_message(*obj.streams_select())
        case "extract":
            value = data[2]
            await query.answer()
            if value in ("extension", "alt"):
                ext_dict = {
                    "ass": [1, "srt"], "srt": [1, "ass"],
                    "aac": [0, "ac3"], "ac3": [0, "eac3"], "eac3": [0, "m4a"],
                    "m4a": [0, "mka"], "mka": [0, "wav"], "wav": [0, "aac"],
                    "mp4": [2, "mkv"], "mkv": [2, "mp4"],
                }
                if data[3] in ext_dict:
                    index, ext = ext_dict[data[3]]
                    obj.extension[index] = ext
                if value == "alt":
                    obj.executor.data["alt_mode"] = not literal_eval(data[3])
                await obj.update_message(*obj.streams_select())
            else:
                obj.executor.data.update(
                    {
                        "key": int(value) if value.isdigit() else data[2:],
                        "extension": obj.extension,
                    }
                )
                obj.event.set()
