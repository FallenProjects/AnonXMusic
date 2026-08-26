import asyncio
import os
import re
import shutil
import subprocess
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union

import aiofiles
import aiohttp
from aiohttp import ClientSession
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
            cdnurl=data.get("cdnurl", "") or data.get("cdn_url", ""),
            key=data.get("key"),
            platform=data.get("platform"),
        )


_TG_URL_RE = re.compile(r"https?://t\.me/([^/]+)/(\d+)")
_CD_FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';\r\n]+)["\']?', re.IGNORECASE)


def decrypt_audio(data: bytes, hex_key: str) -> bytes:
    key = bytes.fromhex(hex_key)
    iv = bytes.fromhex("72e067fbddcbcf77ebe8bc643f630d93")
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv))
    decryptor = cipher.decryptor()
    return decryptor.update(data) + decryptor.finalize()


def rebuild_ogg_header(data: bytes) -> bytes:
    bdata = bytearray(data)
    patches = {
        0: b"OggS",
        6: b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
        26: b"\x01\x1E\x01vorbis",
        39: b"\x02",
        40: b"\x44\xAC\x00\x00",
        48: b"\x00\xE2\x04\x00",
        56: b"\xB8\x01",
        58: b"OggS",
        62: b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
    }
    for offset, patch in patches.items():
        if len(bdata) >= offset + len(patch):
            bdata[offset : offset + len(patch)] = patch
    return bytes(bdata)


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

    async def _get_session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "FallenApi":
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    def _headers(self) -> Dict[str, str]:
        return {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
        }

    async def _retry(self, coro_fn, *args, label: str = "request") -> Optional[object]:
        """Run an async callable with retries and exponential back-off."""
        for attempt in range(1, self.retries + 1):
            try:
                return await coro_fn(*args)
            except aiohttp.ClientError as exc:
                logger.warning(f"[NETWORK] {label} attempt {attempt}/{self.retries}: {exc}")
            except asyncio.TimeoutError:
                logger.warning(f"[TIMEOUT] {label} attempt {attempt}/{self.retries} exceeded {self.timeout.total}s")
            except Exception as exc:
                logger.warning(f"[ERROR] {label}: {exc}")
                return None

            if attempt < self.retries:
                await asyncio.sleep(2 ** (attempt - 1))

        logger.warning(f"[FAILED] {label}: all {self.retries} attempts exhausted.")
        return None

    async def get_info(self, url: str) -> List[MusicTrack]:
        if not self.api_url or not self.api_key:
            return []

        endpoint = f"{self.api_url}/api/get_url?{urllib.parse.urlencode({'url': url})}"

        async def _fetch() -> List[MusicTrack]:
            session = await self._get_session()
            async with session.get(endpoint, headers=self._headers()) as resp:
                data = await resp.json(content_type=None)

                if resp.status == 200 and isinstance(data, dict):
                    results = data.get("results", [])
                    return [
                        MusicTrack.from_dict(item)
                        for item in results
                        if isinstance(item, dict)
                    ]

                return []

        return await _fetch()

    async def get_track(self, url: str) -> Optional[TrackInfo]:
        if not self.api_url or not self.api_key:
            return None
        endpoint = f"{self.api_url}/api/track?{urllib.parse.urlencode({'url': url})}"

        async def _fetch() -> Optional[TrackInfo]:
            session = await self._get_session()
            async with session.get(endpoint, headers=self._headers()) as resp:
                data = await resp.json(content_type=None)

                if resp.status == 200 and isinstance(data, dict):
                    return TrackInfo.from_dict(data)

                error_msg = data.get("message") if isinstance(data, dict) else "Unknown error"
                status = data.get("status", resp.status) if isinstance(data, dict) else resp.status
                logger.warning(f"[API] {error_msg} (HTTP {status})")
                return None

        res = await self._retry(_fetch, label=f"get_track({url})")
        return res if isinstance(res, TrackInfo) else None

    async def download_cdn(
            self,
            cdn_url: str,
            key: Optional[str] = None,
            track_id: Optional[str] = None,
            platform: Optional[str] = None,
    ) -> Optional[str]:
        session = await self._get_session()

        try:
            async with session.get(cdn_url) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[HTTP {resp.status}] CDN download failed: {cdn_url}"
                    )
                    return None

                filename = _extract_filename(
                    resp.headers.get("Content-Disposition"),
                    cdn_url,
                )

                is_spotify = platform and platform.lower() == "spotify"

                if is_spotify and key and track_id:
                    filename = f"{track_id}.ogg"

                save_path = self.download_dir / filename

                if save_path.exists():
                    return str(save_path)

                raw_bytes = await resp.read()

                if is_spotify and key:
                    try:
                        decrypted = decrypt_audio(raw_bytes, key)
                        decrypted = rebuild_ogg_header(decrypted)

                        async with aiofiles.open(save_path, "wb") as f:
                            await f.write(decrypted)

                        if shutil.which("ffmpeg"):
                            fixed_path = self.download_dir / f"fixed_{filename}"

                            proc = await asyncio.create_subprocess_exec(
                                "ffmpeg",
                                "-y",
                                "-i",
                                str(save_path),
                                "-c",
                                "copy",
                                str(fixed_path),
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )

                            await proc.wait()

                            if fixed_path.exists() and fixed_path.stat().st_size > 0:
                                save_path.unlink(missing_ok=True)
                                fixed_path.rename(save_path)

                    except Exception as exc:
                        logger.warning(
                            f"[DECRYPT ERROR] Failed to decrypt track: {exc}"
                        )
                        save_path.unlink(missing_ok=True)
                        return None

                else:
                    async with aiofiles.open(save_path, "wb") as f:
                        await f.write(raw_bytes)

                return str(save_path)

        except aiohttp.ClientError as exc:
            logger.warning(f"[CDN NETWORK ERROR] {cdn_url}: {exc}")
            return None

        except asyncio.TimeoutError:
            logger.warning(
                f"[CDN TIMEOUT] Download exceeded {self.timeout.total}s: {cdn_url}"
            )
            return None

        except Exception as exc:
            logger.warning(f"[CDN DOWNLOAD ERROR] {cdn_url}: {exc}")
            return None

    async def download_track(self, url_or_track: Union[str, TrackInfo]) -> Optional[str]:
        if isinstance(url_or_track, TrackInfo):
            track = url_or_track
        else:
            track = await self.get_track(url_or_track)

        if not track or not track.cdnurl:
            logger.warning("No track metadata or CDN URL found.")
            return None

        tg_match = _TG_URL_RE.match(track.cdnurl)
        if tg_match:
            return await self._download_from_telegram(track.cdnurl)

        is_spotify = track.platform and track.platform.lower() == "spotify"
        if is_spotify and track.key and track.id:
            return await self.download_cdn(track.cdnurl, key=track.key, track_id=track.id, platform=track.platform)

        # skip dl
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


def _extract_filename(content_disposition: Optional[str], fallback_url: str) -> str:
    if content_disposition:
        match = _CD_FILENAME_RE.search(content_disposition)
        if match:
            return match.group(1).strip()

    basename = os.path.basename(fallback_url.split("?")[0])
    return basename or f"{uuid.uuid4().hex[:8]}.mp3"
