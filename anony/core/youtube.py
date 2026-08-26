# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import asyncio
import os
import random
import re
from pathlib import Path

import aiohttp
import yt_dlp
from py_yt import Playlist, VideosSearch

from anony import app, config, logger
from anony.core._api import FallenApi
from anony.helpers import Track, utils


class DummyLogger:
    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


class YouTube:
    def __init__(self):
        self.base = "https://www.youtube.com/watch?v="
        self.cookies = []
        self.checked = False
        self.cookie_dir = "anony/cookies"
        self.warned = False
        self.regex = re.compile(
            r"(https?://)?(www\.|m\.|music\.)?"
            r"(youtube\.com/(watch\?v=|shorts/|playlist\?list=)|youtu\.be/)"
            r"([A-Za-z0-9_-]{11}|PL[A-Za-z0-9_-]+)([&?][^\s]*)?"
        )
        self.iregex = re.compile(
            r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)"
            r"(?!/(watch\?v=[A-Za-z0-9_-]{11}|shorts/[A-Za-z0-9_-]{11}"
            r"|playlist\?list=PL[A-Za-z0-9_-]+|[A-Za-z0-9_-]{11}))\S*"
        )

        self.api_patterns = {
            "apple": re.compile(
                r"(?i)^https?://music\.apple\.com/[a-zA-Z-]+/(?:song/(?:[^/]+/)?\d+|album/[^/]+/\d+(?:\?i=\d+)?|playlist/[^/]+/pl\.[\w.-]+|artist/[^/]+/\d+)(?:\?.*)?$"
            ),
            "spotify": re.compile(
                r"(?i)^(https?://)?([a-z0-9-]+\.)*spotify\.com/(track|playlist|album|artist)/[a-zA-Z0-9]+(\?.*)?$"
            ),
            "jiosaavn": re.compile(
                r"(?i)https?://(?:www\.)?(?:jiosaavn|saavn)\.com/(?:s/)?(song|album|playlist|featured)(?:/[^/]+)*/([A-Za-z0-9_,-]+)(?:/)?(?:\?.*)?$"
            ),
            "deezer": re.compile(
                r"(?i)https?://(?:www\.)?deezer\.com/(?:[a-z]{2}/)?(track|album|playlist)/(\d+)"
            ),
            "soundcloud": re.compile(
                r"(?i)^(https?://)?(www\.)?soundcloud\.com/[a-zA-Z0-9_-]+/(sets/)?[a-zA-Z0-9._-]+(\?.*)?$"
            ),
            "gaana": re.compile(
                r"(?i)https?://(?:www\.)?gaana\.com/(song|album|playlist|artist)/([A-Za-z0-9\-]+)"
            ),
            "tidal": re.compile(
                r"(?i)https?://(?:www\.|listen\.)?tidal\.com/(?:browse/)?(track|album|playlist)/([a-zA-Z0-9-]+)(?:[/?].*)?$"
            ),
            "mxplayer": re.compile(
                r"(?i)https?://(?:www\.)?mxplayer\.in/(?:show|movie)/.*"
            ),
            "twitch": re.compile(
                r"(?i)https?://(?:www\.|m\.)?twitch\.tv/(?:videos|[\w._-]+/video)/\d+"
            ),
            "twitch_clip": re.compile(
                r"(?i)https?://(?:www\.|m\.)?(?:twitch\.tv/clip/[\w-]+|clips\.twitch\.tv/[\w-]+|twitch\.tv/[\w-]+/clip/[\w-]+)"
            ),
            "kick": re.compile(
                r"(?i)https?://(?:www\.)?kick\.com/[\w._-]+/videos/[a-fA-F0-9-]+"
            ),
            "kick_clip": re.compile(
                r"(?i)https?://(?:www\.)?kick\.com/[\w._-]+/clips/[\w-]+"
            ),
        }
        self.fallen = FallenApi(app)

    def get_cookies(self):
        if not self.checked:
            if os.path.exists(self.cookie_dir):
                for file in os.listdir(self.cookie_dir):
                    if file.endswith(".txt"):
                        self.cookies.append(f"{self.cookie_dir}/{file}")
            self.checked = True
        if not self.cookies:
            if not self.warned:
                self.warned = True
                logger.warning("Cookies are missing; downloads might fail.")
            return None
        return random.choice(self.cookies)

    async def save_cookies(self, urls: list[str]) -> None:
        logger.info("Saving cookies from urls...")
        async with aiohttp.ClientSession() as session:
            for url in urls:
                name = url.split("/")[-1]
                link = "https://batbin.me/raw/" + name
                async with session.get(link) as resp:
                    resp.raise_for_status()
                    os.makedirs(self.cookie_dir, exist_ok=True)
                    with open(f"{self.cookie_dir}/{name}.txt", "wb") as fw:
                        fw.write(await resp.read())
        logger.info(f"Cookies saved in {self.cookie_dir}.")

    def is_platform_url(self, url: str) -> bool:
        return any(pattern.search(url) for pattern in self.api_patterns.values())

    def valid(self, url: str) -> bool:
        if bool(re.match(self.regex, url)):
            return True
        return self.is_platform_url(url)

    def invalid(self, url: str) -> bool:
        if self.valid(url):
            return False
        return bool(re.match(self.iregex, url))

    async def search(self, query: str, m_id: int, video: bool = False) -> Track | list[Track] | None:
        if self.is_platform_url(query) and config.API_KEY and config.API_URL:
            tracks = await self.fallen.get_info(query)
            if tracks:
                if len(tracks) > 1:
                    res_tracks = []
                    for track_info in tracks[:config.PLAYLIST_LIMIT]:
                        dur_sec = track_info.duration or 0
                        dur_str = utils.seconds_to_min(dur_sec)
                        res_tracks.append(
                            Track(
                                id=track_info.id,
                                channel_name=track_info.channel or track_info.platform or "Fallen API",
                                duration=dur_str,
                                duration_sec=dur_sec,
                                message_id=m_id,
                                title=(track_info.title or "Unknown Track")[:25],
                                thumbnail=track_info.thumbnail or config.DEFAULT_THUMB,
                                url=track_info.url or query,
                                view_count=track_info.views or "",
                                video=video,
                            )
                        )
                    return res_tracks

                track_info = tracks[0]
                dur_sec = track_info.duration or 0
                dur_str = utils.seconds_to_min(dur_sec)
                return Track(
                    id=track_info.id,
                    channel_name=track_info.channel or track_info.platform or "Fallen API",
                    duration=dur_str,
                    duration_sec=dur_sec,
                    message_id=m_id,
                    title=(track_info.title or "Unknown Track")[:25],
                    thumbnail=track_info.thumbnail or config.DEFAULT_THUMB,
                    url=track_info.url or query,
                    view_count=track_info.views or "",
                    video=video,
                )

        try:
            _search = VideosSearch(query, limit=1, with_live=False)
            results = await _search.next()
        except Exception:
            results = None

        if results and results.get("result"):
            data = results["result"][0]
            return Track(
                id=data.get("id"),
                channel_name=data.get("channel", {}).get("name"),
                duration=data.get("duration"),
                duration_sec=utils.to_seconds(data.get("duration")),
                message_id=m_id,
                title=data.get("title")[:25],
                thumbnail=data.get("thumbnails", [{}])[-1].get("url").split("?")[0],
                url=data.get("link"),
                view_count=data.get("viewCount", {}).get("short"),
                video=video,
            )

        return None

    async def playlist(self, limit: int, user: str, url: str, video: bool) -> list[Track | None]:
        tracks = []
        if self.is_platform_url(url) and config.API_KEY and config.API_URL:
            api_tracks = await self.fallen.get_info(url)
            for data in api_tracks[:limit]:
                dur_sec = data.duration or 0
                dur_str = utils.seconds_to_min(dur_sec)

                track = Track(
                    id=data.id,
                    channel_name=data.channel or data.platform or "",
                    duration=dur_str,
                    duration_sec=dur_sec,
                    title=(data.title or "Unknown Track")[:25],
                    thumbnail=data.thumbnail or config.DEFAULT_THUMB,
                    url=data.url or url,
                    user=user,
                    view_count=data.views or "",
                    video=video,
                )
                tracks.append(track)
            if tracks:
                return tracks

        try:
            plist = await Playlist.get(url)
            for data in plist["videos"][:limit]:
                track = Track(
                    id=data.get("id"),
                    channel_name=data.get("channel", {}).get("name", ""),
                    duration=data.get("duration"),
                    duration_sec=utils.to_seconds(data.get("duration")),
                    title=data.get("title")[:25],
                    thumbnail=data.get("thumbnails")[-1].get("url").split("?")[0],
                    url=data.get("link").split("&list=")[0],
                    user=user,
                    view_count="",
                    video=video,
                )
                tracks.append(track)
        except Exception:
            pass
        return tracks

    async def download(self, video_id: str, video: bool = False, _url: str | None = "") -> str | None:
        url = _url or self.base + video_id
        ext = "mp4" if video else "webm"
        filename = f"downloads/{video_id}.{ext}"

        if Path(filename).exists():
            return filename


        if not video and config.API_KEY and config.API_URL:
            if path := await self.fallen.download_track(url):
                return path

        cookie = self.get_cookies()
        base_opts = {
            "outtmpl": "downloads/%(id)s.%(ext)s",
            "quiet": True,
            "noplaylist": True,
            "geo_bypass": True,
            "no_warnings": True,
            "overwrites": False,
            "logger": DummyLogger(),
            "nocheckcertificate": True,
            "cookiefile": cookie,
            "remote_components": ["ejs:github"],
        }

        if video:
            ydl_opts = {
                **base_opts,
                "format": "(bestvideo[height<=?720][width<=?1280][ext=mp4])+(bestaudio)",
                "merge_output_format": "mp4",
            }
        else:
            ydl_opts = {
                **base_opts,
                "format": "bestaudio[ext=webm][acodec=opus]",
            }

        def _download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    ydl.download([url])
                except (yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError):
                    return None
                except Exception as ex:
                    logger.warning("Download failed: %s", ex)
                    return None

            return filename

        return await asyncio.to_thread(_download)

    async def close(self):
        await self.fallen.close()
