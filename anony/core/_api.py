import asyncio
import re
import shutil
import subprocess
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiofiles
import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pyrogram import Client, errors

from anony import config, logger


@dataclass
class MusicTrack:
    title: str
    id: str
    url: str
    thumbnail: str
    duration: int
    channel: str
    views: str
    platform: str

    @classmethod
    def from_dict(cls, data: dict) -> "MusicTrack":
        return cls(
            title=data.get("title", ""),
            id=str(data.get("id", "")),
            url=data.get("url", ""),
            thumbnail=data.get("thumbnail", ""),
            duration=data.get("duration", 0),
            channel=data.get("channel", ""),
            views=str(data.get("views", "")),
            platform=data.get("platform", ""),
        )


@dataclass
class TrackInfo:
    id: str
    url: str
    cdnurl: str
    key: Optional[str] = None
    platform: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "TrackInfo":
        return cls(
            id=str(data.get("id", "")),
            url=data.get("url", ""),
            cdnurl=data.get("cdnurl") or data.get("cdn_url", ""),
            key=data.get("key"),
            platform=data.get("platform"),
        )


TG_URL_RE = re.compile(r"https?://t\.me/([^/]+)/(\d+)")
FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';\r\n]+)["\']?', re.IGNORECASE)
SPOTIFY_IV = bytes.fromhex("72e067fbddcbcf77ebe8bc643f630d93")


def is_spotify(platform: Optional[str]) -> bool:
    return platform and platform.lower() == "spotify"


def decrypt_audio(data: bytes, hex_key: str) -> bytes:
    cipher = Cipher(algorithms.AES(bytes.fromhex(hex_key)), modes.CTR(SPOTIFY_IV))
    decryptor = cipher.decryptor()
    return decryptor.update(data) + decryptor.finalize()


def rebuild_ogg_header(data: bytes) -> bytes:
    data = bytearray(data)

    patches = {
        0: b"OggS",
        6: b"\x00" * 10,
        26: b"\x01\x1E\x01vorbis",
        39: b"\x02",
        40: b"\x44\xAC\x00\x00",
        48: b"\x00\xE2\x04\x00",
        56: b"\xB8\x01",
        58: b"OggS",
        62: b"\x00" * 10,
    }

    for offset, patch in patches.items():
        end = offset + len(patch)
        if len(data) >= end:
            data[offset:end] = patch

    return bytes(data)


class FallenApi:
    def __init__(
        self,
        app: Client,
        *,
        retries: int = 3,
        timeout: int = 15,
        download_dir: Path = Path("downloads"),
    ):
        self.app = app
        self.api_url = config.API_URL.rstrip("/") if config.API_URL else ""
        self.api_key = config.API_KEY
        self.retries = retries
        self.timeout = aiohttp.ClientTimeout(total=timeout)

        self.download_dir = download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)

        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)

        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

        self._session = None

    async def __aenter__(self) -> "FallenApi":
        return self

    async def __aexit__(self, *_):
        await self.close()

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
        }

    async def _retry(self, func, *args, label: str = "request"):
        for attempt in range(1, self.retries + 1):
            try:
                return await func(*args)

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    f"[NETWORK] {label} "
                    f"attempt {attempt}/{self.retries}: {exc}"
                )

                if attempt < self.retries:
                    await asyncio.sleep(2 ** (attempt - 1))

            except Exception as exc:
                logger.warning(f"[ERROR] {label}: {exc}")
                return None

        logger.warning(f"[FAILED] {label}: all attempts exhausted.")
        return None

    async def get_info(self, url: str) -> list[MusicTrack]:
        if not self.api_url or not self.api_key:
            return []

        endpoint = (
            f"{self.api_url}/api/get_url?"
            f"{urllib.parse.urlencode({'url': url})}"
        )

        async def fetch():
            session = await self._get_session()
            async with session.get(endpoint, headers=self._headers()) as resp:
                if resp.status != 200:
                    return []

                data = await resp.json(content_type=None)
                if not isinstance(data, dict):
                    return []

                return [
                    MusicTrack.from_dict(item)
                    for item in data.get("results", [])
                    if isinstance(item, dict)
                ]

        return await fetch()

    async def get_track(self, url: str) -> Optional[TrackInfo]:
        if not self.api_url or not self.api_key:
            return None

        endpoint = (
            f"{self.api_url}/api/track?"
            f"{urllib.parse.urlencode({'url': url})}"
        )

        async def fetch():
            session = await self._get_session()
            async with session.get(endpoint, headers=self._headers()) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 200 and isinstance(data, dict):
                    return TrackInfo.from_dict(data)

                if isinstance(data, dict):
                    error = data.get("message", "Unknown error")
                    status = data.get("status", resp.status)
                else:
                    error = "Unknown error"
                    status = resp.status

                logger.warning(f"[API] {error} (HTTP {status})")
                return None

        result = await self._retry(fetch, label=f"get_track({url})")
        return result if isinstance(result, TrackInfo) else None

    async def download_cdn(
        self,
        cdn_url: str,
        *,
        key: Optional[str] = None,
        track_id: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> Optional[str]:
        try:
            session = await self._get_session()
            async with session.get(cdn_url) as resp:
                if resp.status != 200:
                    logger.warning(f"[HTTP {resp.status}] CDN download failed: {cdn_url}")
                    return None

                filename = extract_filename(resp.headers.get("Content-Disposition"), cdn_url)

                spotify = is_spotify(platform)
                if spotify and key and track_id:
                    filename = f"{track_id}.ogg"

                save_path = self.download_dir / filename
                if save_path.exists():
                    return str(save_path)

                raw_bytes = await resp.read()
                if spotify and key:
                    return await self._save_spotify(save_path, raw_bytes, key)

                async with aiofiles.open(save_path, "wb") as file:
                    await file.write(raw_bytes)

                return str(save_path)

        except aiohttp.ClientError as exc:
            logger.warning(f"[CDN NETWORK ERROR] {cdn_url}: {exc}")

        except asyncio.TimeoutError:
            logger.warning(
                f"[CDN TIMEOUT] Download exceeded "
                f"{self.timeout.total}s: {cdn_url}"
            )

        except Exception as exc:
            logger.warning(f"[CDN DOWNLOAD ERROR] {cdn_url}: {exc}")

        return None

    async def _save_spotify(self,path: Path, data: bytes, key: str) -> Optional[str]:
        try:
            decrypted = rebuild_ogg_header(decrypt_audio(data, key))
            async with aiofiles.open(path, "wb") as file:
                await file.write(decrypted)

            if shutil.which("ffmpeg"):
                fixed_path = self.download_dir / f"fixed_{path.name}"
                process = await asyncio.create_subprocess_exec(
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(path),
                    "-c",
                    "copy",
                    str(fixed_path),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                await process.wait()
                if fixed_path.exists() and fixed_path.stat().st_size > 0:
                    path.unlink(missing_ok=True)
                    fixed_path.rename(path)

            return str(path)

        except Exception as exc:
            logger.warning(f"[DECRYPT ERROR] Failed to decrypt track: {exc}")
            path.unlink(missing_ok=True)
            return None

    async def download_track(self, url_or_track: str | TrackInfo) -> Optional[str]:
        track = url_or_track if isinstance(url_or_track, TrackInfo) else await self.get_track(url_or_track)
        if not track or not track.cdnurl:
            logger.warning("No track metadata or CDN URL found.")
            return None

        if TG_URL_RE.match(track.cdnurl):
            return await self._download_from_telegram(track.cdnurl)

        if is_spotify(track.platform) and track.key and track.id:
            return await self.download_cdn(track.cdnurl, key=track.key, track_id=track.id, platform=track.platform)

        # Non-Spotify CDN URLs are returned directly.
        return track.cdnurl

    async def _download_from_telegram(self, tg_url: str) -> Optional[str]:
        try:
            msg = await self.app.get_messages(message_ids=tg_url)
            if not msg:
                logger.warning(f"[TG] Message {tg_url} has no downloadable media.")
                return None
            return await msg.download(file_name=str(self.download_dir / ""))
        except errors.FloodWait as exc:
            logger.warning(f"[FLOODWAIT] Sleeping {exc.value}s…")
            await asyncio.sleep(exc.value + 1)
            return await self._download_from_telegram(tg_url)
        except Exception as exc:
            logger.warning(f"[TG DOWNLOAD ERROR] {exc}")
            return None


def extract_filename(content_disposition: Optional[str], fallback_url: str) -> str:
    if content_disposition:
        match = FILENAME_RE.search(content_disposition)

        if match:
            return match.group(1).strip()

    filename = urllib.parse.urlparse(fallback_url).path.rsplit("/", 1)[-1]

    return filename or f"{uuid.uuid4().hex[:8]}.mp3"
