"""Bruecke vom P2P-Livestream zu einem Bild, das Home Assistant anzeigen kann.

Kameras ohne brauchbares RTSP liefern ihr Video nur als P2P-Datenstrom
ueber die WebSocket-Verbindung - rohes H.264 oder H.265, keine URL.
Diese Bruecke schiebt den Datenstrom in ein ffmpeg und holt am anderen
Ende Einzelbilder heraus.

Wichtig: ffmpeg wird erst gestartet, wenn das erste Videopaket da ist.
Erst dann ist bekannt, welchen Codec die Kamera benutzt - und ein
H.265-Strom durch einen H.264-Decoder ergibt schlicht nichts.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import HomeAssistant

from .websocket import EufyMaxClient

_LOGGER = logging.getLogger(__name__)

JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"

# Bilder pro Sekunde. Mehr kostet spuerbar Rechenzeit auf dem Pi.
OUTPUT_FPS = 4
JPEG_QUALITY = 6

MAX_BUFFER = 4_000_000
MAX_PENDING = 2_000_000

# Codecnamen aus den Paketen -> Eingabeformat fuer ffmpeg
CODEC_FORMATS = {
    "H264": "h264",
    "AVC": "h264",
    "H265": "hevc",
    "HEVC": "hevc",
    "UNKNOWN": "h264",
}


class P2PVideoBridge:
    """Wandelt den P2P-Datenstrom einer Kamera in Einzelbilder."""

    def __init__(
        self, hass: HomeAssistant, client: EufyMaxClient, serial: str
    ) -> None:
        """Bruecke fuer eine Kamera anlegen."""
        self.hass = hass
        self.client = client
        self.serial = serial

        self.running: bool = False
        self.latest_image: bytes | None = None
        self.frames_received: int = 0
        self.codec: str | None = None

        self._process: asyncio.subprocess.Process | None = None
        self._output_task: asyncio.Task | None = None
        self._starting: bool = False
        self._first_frame = asyncio.Event()
        self._buffer = bytearray()
        self._pending = bytearray()

    # ------------------------------------------------------------------

    async def async_start(self) -> bool:
        """Livestream anfordern. ffmpeg folgt beim ersten Paket."""
        if self.running:
            return True

        self.running = True
        self._first_frame.clear()
        self.frames_received = 0
        self._buffer.clear()
        self._pending.clear()

        self.client.add_video_handler(self.serial, self._feed)

        try:
            await self.client.async_start_livestream(self.serial)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "Livestream fuer %s nicht startbar: %s", self.serial, err
            )
            await self.async_stop()
            return False

        _LOGGER.info("P2P-Bruecke fuer %s wartet auf Videodaten", self.serial)
        return True

    async def async_stop(self) -> None:
        """Alles wieder abbauen."""
        self.client.remove_video_handler(self.serial)

        if self.running:
            try:
                await self.client.async_stop_livestream(self.serial)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Livestream-Stopp fuer %s: %s", self.serial, err)

        self.running = False
        self._starting = False

        if self._output_task:
            self._output_task.cancel()
            self._output_task = None

        await self._async_kill_process()

        self._buffer.clear()
        self._pending.clear()

    async def _async_kill_process(self) -> None:
        """ffmpeg beenden."""
        if self._process is None:
            return
        try:
            if self._process.stdin and not self._process.stdin.is_closing():
                self._process.stdin.close()
            self._process.kill()
            await self._process.wait()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("ffmpeg-Ende fuer %s: %s", self.serial, err)
        self._process = None

    # ------------------------------------------------------------------

    def _feed(self, data: bytes, codec: str | None) -> None:
        """Ein Videopaket verarbeiten."""
        if not self.running:
            return

        # Noch kein ffmpeg: Paket zwischenspeichern und Start anstossen.
        if self._process is None:
            self._pending.extend(data)
            if len(self._pending) > MAX_PENDING:
                del self._pending[:-MAX_PENDING]

            if not self._starting:
                self._starting = True
                self.codec = codec
                self.hass.async_create_task(self._async_spawn(codec))
            return

        stdin = self._process.stdin
        if stdin is None or stdin.is_closing():
            return

        try:
            stdin.write(data)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Schreiben nach ffmpeg fehlgeschlagen: %s", err)

    async def _async_spawn(self, codec: str | None) -> None:
        """ffmpeg passend zum gemeldeten Codec starten."""
        key = (codec or "H264").upper()
        input_format = CODEC_FORMATS.get(key, "h264")

        _LOGGER.info(
            "%s sendet %s - starte ffmpeg mit -f %s",
            self.serial,
            codec or "unbekannt",
            input_format,
        )

        binary = get_ffmpeg_manager(self.hass).binary

        try:
            process = await asyncio.create_subprocess_exec(
                binary,
                "-hide_banner",
                "-loglevel", "error",
                # Der Strom beginnt mitten drin: Fehler am Anfang
                # ignorieren und auf das naechste Vollbild warten.
                "-err_detect", "ignore_err",
                "-fflags", "nobuffer+discardcorrupt+genpts",
                "-flags", "low_delay",
                "-analyzeduration", "10000000",
                "-probesize", "5000000",
                "-f", input_format,
                "-i", "pipe:0",
                "-an",
                "-vf", f"fps={OUTPUT_FPS}",
                "-q:v", str(JPEG_QUALITY),
                "-f", "image2pipe",
                "-vcodec", "mjpeg",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("ffmpeg konnte nicht gestartet werden: %s", err)
            self._starting = False
            return

        if not self.running:
            # Zwischenzeitlich gestoppt.
            self._process = process
            await self._async_kill_process()
            return

        self._process = process
        self._output_task = self.hass.async_create_background_task(
            self._async_read_output(), name=f"eufy_max_bridge_{self.serial}"
        )

        # Die zwischengespeicherten Pakete nachreichen.
        if self._pending and process.stdin is not None:
            try:
                process.stdin.write(bytes(self._pending))
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Zwischenspeicher nicht schreibbar: %s", err)
            self._pending.clear()

    async def _async_read_output(self) -> None:
        """JPEG-Bilder aus der ffmpeg-Ausgabe herausschneiden."""
        process = self._process
        if process is None or process.stdout is None:
            return

        stdout = process.stdout

        try:
            while self.running:
                chunk = await stdout.read(65536)
                if not chunk:
                    break

                self._buffer.extend(chunk)

                while True:
                    start = self._buffer.find(JPEG_START)
                    if start == -1:
                        self._buffer.clear()
                        break

                    end = self._buffer.find(JPEG_END, start + 2)
                    if end == -1:
                        if start > 0:
                            del self._buffer[:start]
                        break

                    self.latest_image = bytes(self._buffer[start : end + 2])
                    self.frames_received += 1
                    del self._buffer[: end + 2]

                    if not self._first_frame.is_set():
                        self._first_frame.set()
                        _LOGGER.info("%s liefert jetzt Bilder", self.serial)

                if len(self._buffer) > MAX_BUFFER:
                    _LOGGER.debug("Bildpuffer fuer %s verworfen", self.serial)
                    self._buffer.clear()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Ausgabe von ffmpeg beendet: %s", err)
        finally:
            await self._async_log_ffmpeg_errors(process)

    async def _async_log_ffmpeg_errors(self, process) -> None:
        """Fehlermeldungen von ffmpeg sichtbar machen."""
        if process is None or process.stderr is None:
            return
        try:
            async with asyncio.timeout(2):
                data = await process.stderr.read(4000)
        except Exception:  # noqa: BLE001
            return

        if data:
            _LOGGER.warning(
                "ffmpeg meldet fuer %s: %s",
                self.serial,
                data.decode(errors="replace").strip(),
            )
