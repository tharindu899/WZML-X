from __future__ import annotations
from aiofiles.os import path as aiopath
from ast import literal_eval
from asyncio import create_subprocess_exec
from asyncio.subprocess import PIPE
from langcodes import Language, find as find_language
from natsort import natsorted
from os import path as ospath, walk
import re

from . import VID_MODE
from .extra_selector import ExtraSelect
from ... import LOGGER, task_dict, task_dict_lock, cores, threads
from ...core.config_manager import Config, BinConfig
from ..ext_utils.bot_lock import ff_lock
from ..ext_utils.bot_utils import sync_to_async, cmd_exec
from ..ext_utils.files_utils import clean_target
from ..ext_utils.media_utils import get_document_type, get_media_info, get_streams, FFMpeg
from ..mirror_leech_utils.status_utils.ffmpeg_status import FFmpegStatus

# tokens between dots/underscores/dashes/brackets/spaces, e.g.
# "Movie.Name.2024.sin.srt" -> ["Movie", "Name", "2024", "sin"]
_NAME_TOKEN_RE = re.compile(r"[._\-\[\]()\s]+")


def detect_sub_language(sub_path: str):
    """
    Try to detect a subtitle's language from its filename (checked from the
    end, since language tags are usually placed right before the extension,
    e.g. "movie.eng.srt", "movie.sinhala.ass", "movie.si.srt").
    Returns (iso639-2 code, display name) or (None, None) if nothing matches.
    """
    stem = ospath.splitext(ospath.basename(sub_path))[0]
    tokens = [t for t in _NAME_TOKEN_RE.split(stem) if t.isalpha()]
    for tok in reversed(tokens):
        low = tok.lower()
        try:
            if 2 <= len(low) <= 3:
                lang = Language.get(low)
                if lang.is_valid():
                    return lang.to_alpha3(), lang.display_name()
            else:
                lang = find_language(low)
                return lang.to_alpha3(), lang.display_name()
        except Exception:
            continue
    return None, None


async def get_metavideo(video_file):
    """Raw ffprobe streams+format for a video, used by the stream-aware modes."""
    stdout, stderr, rcode = await cmd_exec(
        [
            "ffprobe",
            "-hide_banner",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            video_file,
        ]
    )
    if rcode != 0:
        LOGGER.error(f"get_metavideo: {stderr}")
        return {}, {}
    metadata = literal_eval(stdout)
    return metadata.get("streams", []), metadata.get("format", {})


class VidExecutor:
    """
    Runs the ffmpeg job picked via the -vt SelectMode menu against a file (or
    folder of files) that has *already finished downloading*. Called from
    task_listener.py right before upload, mirroring how sample-video/
    screenshots are generated.
    """

    def __init__(self, listener, path: str, gid: str):
        self.listener = listener
        self.path = path
        self._up_path = path
        self._gid = gid
        self._is_dir = False
        self.name = ""
        self.outfile = ""
        self.size = 0
        self.data = None
        self.mode = ""
        self.is_cancel = False
        self._qual = {
            "1080p": "1920",
            "720p": "1280",
            "540p": "960",
            "480p": "854",
            "360p": "640",
        }
        self._eng = FFMpeg(listener)

    # -- FFmpegStatus reads these off "self" (the object passed in) --
    @property
    def processed_bytes(self):
        return self._eng.processed_bytes

    @property
    def speed_raw(self):
        return self._eng.speed_raw

    @property
    def progress_raw(self):
        return self._eng.progress_raw

    @property
    def eta_raw(self):
        return self._eng.eta_raw

    async def execute(self):
        if not self.listener.vid_mode:
            return self._up_path
        self._is_dir = await aiopath.isdir(self.path)
        self.mode, self.name, kwargs = self.listener.vid_mode
        if self.mode in (Config.DISABLE_MULTI_VIDTOOLS or "").split():
            if path := await self._get_video():
                self.path = path
            else:
                return self._up_path
        try:
            match self.mode:
                case "vid_vid":
                    return await self._merge_vids()
                case "vid_aud":
                    return await self._merge_auds()
                case "vid_sub":
                    return await self._merge_subs(**kwargs)
                case "trim":
                    return await self._vid_trimmer(**kwargs)
                case "watermark":
                    return await self._vid_marker(**kwargs)
                case "compress":
                    return await self._vid_compress(**kwargs)
                case "subsync":
                    return await self._subsync(**kwargs)
                case "rmstream":
                    return await self._rm_stream()
                case "extract":
                    return await self._vid_extract()
                case _:
                    return await self._vid_convert()
        except Exception as e:
            LOGGER.error(f"VidExecutor [{self.mode}]: {e}", exc_info=True)
        return self._up_path

    async def _send_status(self):
        async with task_dict_lock:
            task_dict[self.listener.mid] = FFmpegStatus(
                self.listener, self, self._gid, VID_MODE.get(self.mode, "Video Tools")
            )

    async def _get_files(self):
        file_list = []
        if await aiopath.isfile(self.path):
            if (await get_document_type(self.path))[0]:
                file_list.append(self.path)
        else:
            for dirpath, _, files in await sync_to_async(walk, self.path):
                for file in natsorted(files):
                    file = ospath.join(dirpath, file)
                    if (await get_document_type(file))[0]:
                        file_list.append(file)
        return file_list

    async def _get_video(self):
        if not self._is_dir and (await get_document_type(self.path))[0]:
            return self.path
        for dirpath, _, files in await sync_to_async(walk, self.path):
            for file in natsorted(files):
                file = ospath.join(dirpath, file)
                if (await get_document_type(file))[0]:
                    return file
        return None

    async def _final_path(self, outfile=""):
        if outfile:
            self._up_path = outfile
        else:
            scan_dir = self._up_path if self._is_dir else ospath.split(self._up_path)[0]
            all_files = []
            for dirpath, _, files in await sync_to_async(walk, scan_dir):
                all_files.extend((dirpath, file) for file in files)
            if len(all_files) == 1:
                self._up_path = ospath.join(*all_files[0])
        return self._up_path

    async def _run_cmd(self, cmd, total_time, taskset=True):
        """Shared runner: builds progress via FFMpeg, executes, returns rcode."""
        if self.listener.is_cancelled:
            return -1
        self._eng.clear()
        self._eng._total_time = total_time or 1
        full_cmd = (["taskset", "-c", f"{cores}"] if taskset else []) + cmd
        async with ff_lock:
            self.listener.subproc = await create_subprocess_exec(
                *full_cmd, stdout=PIPE, stderr=PIPE
            )
            await self._eng._ffmpeg_progress()
            _, stderr = await self.listener.subproc.communicate()
        rcode = self.listener.subproc.returncode
        if self.listener.is_cancelled:
            return -1
        if rcode != 0:
            try:
                stderr = stderr.decode().strip()
            except Exception:
                stderr = "Unable to decode the error!"
            LOGGER.error(f"VidExecutor ffmpeg failed ({rcode}): {stderr}")
        return rcode

    def _base_cmd(self, *extra):
        return [
            BinConfig.FFMPEG_NAME,
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-threads",
            f"{threads}",
            *extra,
        ]

    # ---------------------------------------------------------- merge --
    async def _merge_vids(self):
        files = await self._get_files()
        if len(files) < 2:
            await self.listener.on_upload_error("Need at least 2 videos to merge!")
            return self._up_path
        await self._send_status()
        listfile = ospath.join(
            ospath.dirname(files[0]), f"vidlist_{self.listener.mid}.txt"
        )
        lines = "".join(f"file '{file}'\n" for file in files)
        await sync_to_async(_write_text, listfile, lines)
        out_dir = self.path if self._is_dir else ospath.dirname(self.path)
        outname = self.name or f"Merged_{self.listener.mid}.mkv"
        if not outname.upper().endswith((".MKV", ".MP4")):
            outname += ".mkv"
        outfile = ospath.join(out_dir, outname)
        total_time = sum([(await get_media_info(f))[0] for f in files])
        cmd = self._base_cmd(
            "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", outfile
        )
        rcode = await self._run_cmd(cmd, total_time)
        await clean_target(listfile)
        if rcode != 0:
            return self._up_path
        for file in files:
            await clean_target(file)
        return await self._final_path(outfile)

    async def _merge_auds(self):
        files = await self._get_files()
        if not self.path or len(files) < 1:
            await self.listener.on_upload_error("No video found to merge audio into!")
            return self._up_path
        video = files[0]
        auds = files[1:]
        if not auds:
            await self.listener.on_upload_error("Need at least 1 extra audio to merge!")
            return self._up_path
        await self._send_status()
        base_name, ext = ospath.splitext(video)
        outfile = f"{base_name}.merged{ext or '.mkv'}"
        cmd = self._base_cmd("-i", video)
        for aud in auds:
            cmd += ["-i", aud]
        cmd += ["-map", "0:v"]
        for i in range(len(auds)):
            cmd += ["-map", f"{i + 1}:a"]
        cmd += ["-map", "0:a?", "-c", "copy", outfile]
        total_time = (await get_media_info(video))[0]
        rcode = await self._run_cmd(cmd, total_time)
        if rcode != 0:
            return self._up_path
        for aud in auds:
            await clean_target(aud)
        await clean_target(video)
        return await self._final_path(outfile)

    async def _merge_subs(self, hardsub=False, **kwargs):
        files = await self._get_files()
        if not files:
            await self.listener.on_upload_error("No video found!")
            return self._up_path
        video = files[0]
        await self._send_status()
        base_name, ext = ospath.splitext(video)
        subfile = kwargs.get("subfile")
        has_subfile = subfile and await aiopath.exists(subfile)
        if hardsub and has_subfile:
            outfile = f"{base_name}.hardsub.mp4"
            style = self._hardsub_style(kwargs)
            escaped = subfile.replace("'", r"\'").replace(":", r"\:")
            vf = f"subtitles='{escaped}'{style}"
            cmd = self._base_cmd(
                "-i", video, "-vf", vf, "-c:v", "libx264",
                "-preset", Config.LIB264_PRESET, "-c:a", "copy", outfile,
            )
        else:
            subs = [f for f in files if f != video]
            if has_subfile and subfile not in subs:
                subs.append(subfile)
            if not subs:
                await self.listener.on_upload_error("No subtitle found to merge!")
                return self._up_path
            outfile = f"{base_name}.merged.mkv"
            cmd = self._base_cmd("-i", video)
            for sub in subs:
                cmd += ["-i", sub]
            cmd += ["-map", "0"]
            for i in range(len(subs)):
                cmd += ["-map", f"{i + 1}"]
            # "-map 0" pulls in any subtitle streams already inside the
            # source video first, so newly-muxed subs are indexed after them
            src_streams = await get_streams(video) or []
            sub_offset = sum(
                1 for s in src_streams if s.get("codec_type") == "subtitle"
            )
            for i, sub in enumerate(subs):
                s_index = sub_offset + i
                lang_code, lang_name = detect_sub_language(sub)
                if lang_code:
                    cmd += [f"-metadata:s:s:{s_index}", f"language={lang_code}"]
                    cmd += [f"-metadata:s:s:{s_index}", f"title={lang_name}"]
                else:
                    fallback = Config.AUTHOR_NAME or "CineFlow"
                    cmd += [f"-metadata:s:s:{s_index}", f"title={fallback}"]
            cmd += ["-c", "copy", outfile]
        total_time = (await get_media_info(video))[0]
        rcode = await self._run_cmd(cmd, total_time)
        if rcode != 0:
            return self._up_path
        await clean_target(video)
        return await self._final_path(outfile)

    def _hardsub_style(self, kwargs):
        fontname = (kwargs.get("fontname") or Config.HARDSUB_FONT_NAME or "Arial").replace(
            "_", " "
        )
        fontsize = kwargs.get("fontsize") or Config.HARDSUB_FONT_SIZE or "18"
        colour = kwargs.get("fontcolour", "ffffff")
        bold = "1" if kwargs.get("boldstyle") else "0"
        return (
            f":force_style='FontName={fontname},FontSize={fontsize},"
            f"PrimaryColour=&H{colour}&,Bold={bold}'"
        )

    # ---------------------------------------------------------- trim ---
    async def _vid_trimmer(self, start_time="", end_time="", **_):
        files = await self._get_files()
        if not files:
            await self.listener.on_upload_error("No video found to trim!")
            return self._up_path
        await self._send_status()
        results = []
        for video in files:
            base_name, ext = ospath.splitext(video)
            outfile = f"{base_name}.trim{ext or '.mkv'}"
            cmd = self._base_cmd(
                "-i", video, "-ss", start_time or "00:00:00",
                *(["-to", end_time] if end_time else []),
                "-c", "copy", outfile,
            )
            total_time = (await get_media_info(video))[0]
            rcode = await self._run_cmd(cmd, total_time)
            if rcode == 0:
                await clean_target(video)
                results.append(outfile)
            else:
                results.append(video)
        return await self._final_path(results[0] if len(results) == 1 else "")

    # ------------------------------------------------------ watermark --
    async def _vid_marker(
        self, hardsub=False, subfile="", quality="", wmsize="20",
        wmposition="5:5", popupwm=0, **kwargs,
    ):
        files = await self._get_files()
        if not files:
            await self.listener.on_upload_error("No video found to watermark!")
            return self._up_path
        wm_image = ospath.join("watermark", f"{self.listener.mid}.png")
        if not await aiopath.exists(wm_image):
            await self.listener.on_upload_error("No watermark image was set!")
            return self._up_path
        await self._send_status()
        results = []
        for video in files:
            base_name, ext = ospath.splitext(video)
            outfile = f"{base_name}.wm{ext or '.mkv'}"
            scale = f"iw*{int(wmsize)}/100:-1"
            overlay = f"overlay={wmposition}"
            if popupwm:
                cycle = 20 / max(1, int(popupwm))
                overlay += f":enable='lt(mod(t\\,{cycle})\\,{cycle / 2})'"
            # Build the graph as a list of filter stages so extra stages
            # (hardsub, quality cap) can be appended before we close it off
            # with an explicit output label -- avoids relying on ffmpeg's
            # implicit stream auto-selection, which gets ambiguous once a
            # filter_complex graph is involved.
            chain = f"[1:v]scale={scale}[wm];[0:v][wm]{overlay}"
            if hardsub and subfile and await aiopath.exists(subfile):
                style = self._hardsub_style(kwargs)
                escaped = subfile.replace("'", r"\'").replace(":", r"\:")
                chain += f",subtitles='{escaped}'{style}"
            if quality and quality in self._qual:
                chain += f",scale={self._qual[quality]}:-2"
            filter_complex = f"{chain}[vout]"
            cmd = self._base_cmd(
                "-i", video, "-i", wm_image,
                "-filter_complex", filter_complex,
                "-map", "[vout]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", Config.LIB264_PRESET,
                "-crf", "23", "-c:a", "copy", outfile,
            )
            total_time = (await get_media_info(video))[0]
            rcode = await self._run_cmd(cmd, total_time)
            if rcode == 0:
                await clean_target(video)
                results.append(outfile)
            else:
                results.append(video)
        return await self._final_path(results[0] if len(results) == 1 else "")

    # -------------------------------------------------------- compress --
    async def _vid_compress(self, quality="720p", **_):
        files = await self._get_files()
        if not files:
            await self.listener.on_upload_error("No video found to compress!")
            return self._up_path
        results = []
        for video in files:
            streams, _ = await get_metavideo(video)
            if not streams:
                results.append(video)
                continue
            selector = ExtraSelect(self)
            selector.mode = self.mode
            await selector.get_buttons(streams)
            if self.is_cancel or selector.is_cancel:
                return self._up_path
            audio_map = self.data.get("audio") if self.data else 0
            await self._send_status()
            base_name, ext = ospath.splitext(video)
            outfile = f"{base_name}.compressed.mp4"
            cmd = self._base_cmd("-i", video, "-map", "0:v")
            if audio_map:
                cmd += ["-map", f"0:{audio_map}"]
            else:
                cmd += ["-map", "0:a?"]
            width = self._qual.get(quality, "1280")
            cmd += [
                "-vf", f"scale={width}:-2",
                "-c:v", "libx265", "-preset", Config.LIB265_PRESET,
                "-crf", "28", "-c:a", "aac", "-b:a", "128k", outfile,
            ]
            total_time = (await get_media_info(video))[0]
            rcode = await self._run_cmd(cmd, total_time)
            if rcode == 0:
                await clean_target(video)
                results.append(outfile)
            else:
                results.append(video)
        return await self._final_path(results[0] if len(results) == 1 else "")

    # --------------------------------------------------------- convert --
    async def _vid_convert(self):
        files = await self._get_files()
        if not files:
            return self._up_path
        results = []
        for video in files:
            streams, _ = await get_metavideo(video)
            if not streams:
                results.append(video)
                continue
            selector = ExtraSelect(self)
            selector.mode = "convert"
            await selector.get_buttons(streams)
            if self.is_cancel or selector.is_cancel:
                return self._up_path
            resolution = self.data if isinstance(self.data, str) else "720p"
            await self._send_status()
            base_name, ext = ospath.splitext(video)
            outfile = f"{base_name}.{resolution}{ext or '.mkv'}"
            width = self._qual.get(resolution, "1280")
            cmd = self._base_cmd(
                "-i", video, "-vf", f"scale={width}:-2",
                "-c:v", "libx264", "-preset", Config.LIB264_PRESET,
                "-crf", "23", "-c:a", "copy", outfile,
            )
            total_time = (await get_media_info(video))[0]
            rcode = await self._run_cmd(cmd, total_time)
            if rcode == 0:
                await clean_target(video)
                results.append(outfile)
            else:
                results.append(video)
        return await self._final_path(results[0] if len(results) == 1 else "")

    # ---------------------------------------------------- remove stream --
    async def _rm_stream(self):
        files = await self._get_files()
        if not files:
            return self._up_path
        results = []
        for video in files:
            streams, _ = await get_metavideo(video)
            if not streams:
                results.append(video)
                continue
            selector = ExtraSelect(self)
            selector.mode = "rmstream"
            await selector.get_buttons(streams)
            if self.is_cancel or selector.is_cancel:
                return self._up_path
            sdata = self.data.get("sdata", []) if self.data else []
            if not sdata:
                results.append(video)
                continue
            await self._send_status()
            base_name, ext = ospath.splitext(video)
            outfile = f"{base_name}.streamless{ext or '.mkv'}"
            cmd = self._base_cmd("-i", video, "-map", "0")
            for mapindex in sdata:
                cmd += ["-map", f"-0:{mapindex}"]
            cmd += ["-c", "copy", outfile]
            total_time = (await get_media_info(video))[0]
            rcode = await self._run_cmd(cmd, total_time)
            if rcode == 0:
                await clean_target(video)
                results.append(outfile)
            else:
                results.append(video)
        return await self._final_path(results[0] if len(results) == 1 else "")

    # -------------------------------------------------------- extract --
    async def _vid_extract(self):
        files = await self._get_files()
        if not files:
            return self._up_path
        results = []
        for video in files:
            streams, _ = await get_metavideo(video)
            if not streams:
                results.append(video)
                continue
            selector = ExtraSelect(self)
            selector.mode = "extract"
            await selector.get_buttons(streams)
            if self.is_cancel or selector.is_cancel:
                return self._up_path
            key = self.data.get("key") if self.data else None
            extension = (
                self.data.get("extension", [None, None, "mkv"])
                if self.data
                else [None, None, "mkv"]
            )
            if key is None:
                results.append(video)
                continue
            await self._send_status()
            base_name = ospath.splitext(video)[0]
            keys = key if isinstance(key, list) else [key]
            extracted = []
            for k in keys:
                if k in ("video", "audio", "subtitle"):
                    ext_map = {"video": "mkv", "audio": extension[0], "subtitle": extension[1]}
                    outfile = f"{base_name}.{k}.{ext_map[k]}"
                    cmd = self._base_cmd("-i", video, "-map", f"0:{k[0]}", "-c", "copy", outfile)
                else:
                    outfile = f"{base_name}.stream{k}.{extension[2]}"
                    cmd = self._base_cmd("-i", video, "-map", f"0:{k}", "-c", "copy", outfile)
                total_time = (await get_media_info(video))[0]
                rcode = await self._run_cmd(cmd, total_time)
                if rcode == 0:
                    extracted.append(outfile)
            results.extend(extracted or [video])
        return await self._final_path(results[0] if len(results) == 1 else "")

    # ------------------------------------------------------- subsync ---
    async def _subsync(self, **_):
        if not await sync_to_async(_alass_available):
            await self.listener.on_upload_error(
                "Subtitle Sync needs the 'alass' binary, which isn't installed on this bot!"
            )
            return self._up_path
        files = await self._get_files()
        subs_video = {i: ospath.basename(f) for i, f in enumerate(files)}
        selector = ExtraSelect(self)
        selector.mode = "subsync"
        self.data = {"list": subs_video, "final": {}}
        await selector.get_buttons()
        if self.is_cancel or selector.is_cancel:
            return self._up_path
        results = []
        for position, info in self.data.get("final", {}).items():
            target = files[position]
            ref = info.get("ref")
            if not ref:
                results.append(target)
                continue
            ref_path = next((f for f in files if ospath.basename(f) == ref), None)
            if not ref_path:
                results.append(target)
                continue
            await self._send_status()
            base_name, ext = ospath.splitext(target)
            outfile = f"{base_name}.synced{ext}"
            _, stderr, rcode = await cmd_exec(["alass", ref_path, target, outfile])
            if rcode == 0:
                await clean_target(target)
                results.append(outfile)
            else:
                LOGGER.error(f"alass sync failed: {stderr}")
                results.append(target)
        return await self._final_path(results[0] if len(results) == 1 else "")


def _write_text(path, content):
    with open(path, "w") as f:
        f.write(content)


def _alass_available():
    from shutil import which

    return which("alass") is not None
