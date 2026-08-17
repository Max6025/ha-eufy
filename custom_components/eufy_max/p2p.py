"""Bruecke vom P2P-Livestream zu einem Bild, das Home Assistant anzeigen kann.

Kameras ohne RTSP liefern ihr Video nur als P2P-Datenstrom ueber die
WebSocket-Verbindung - roher H.264, keine URL. Diese Bruecke schiebt
den Datenstrom in ein ffmpeg und holt am anderen Ende Einzelbilder
heraus. Home Assistant setzt daraus ein fortlaufendes Livebild zusammen.
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

# Der P2P-Strom beginnt mitten im Bild. ffmpeg muss auf das naechste
# Vollbild warten, das kann je nach Kamera etwas dauern.
FIRST_FRAME_TIMEOUT = 45
MAX_BUFFER = 4_000_000


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

        self._process: asyncio.subprocess.Process | None = None
        self._output_task: asyncio.Task | None = None
        self._first_frame = asyncio.Event()
        self._buffer = bytearray()

    # ------------------------------------------------------------------

    async def async_start(self) -> bool:
        """ffmpeg starten und den Livestream anfordern."""
        if self.running:
            return True

        binary = get_ffmpeg_manager(self.hass).binary

        try:
            self._process = await asyncio.create_subprocess_exec(
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
                "-f", "h264",
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
            return False

        self.running = True
        self._first_frame.clear()
        self.frames_received = 0
        self._buffer.clear()

        self._output_task = self.hass.async_create_background_task(
            self._async_read_output(), name=f"eufy_max_bridge_{self.serial}"
        )

        # Ab jetzt kommen die Videodaten bei uns an.
        self.client.add_video_handler(self.serial, self._feed)

        try:
            await self.client.async_start_livestream(self.serial)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "Livestream fuer %s nicht startbar: %s", self.serial, err
            )
            await self.async_stop()
            return False

        # Auf das erste Bild warten - der P2P-Aufbau dauert einige
        # Sekunden. Kommt keins, laeuft die Bruecke trotzdem weiter und
        # liefert nach, sobald ein Vollbild eintrifft.
        try:
            async with asyncio.timeout(FIRST_FRAME_TIMEOUT):
                await self._first_frame.wait()
        except TimeoutError:
            _LOGGER.warning(
                "Noch kein Bild von %s nach %s Sekunden - Bruecke laeuft weiter",
                self.serial,
                FIRST_FRAME_TIMEOUT,
            )
            return False

        _LOGGER.info("P2P-Bruecke fuer %s liefert Bilder", self.serial)
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

        if self._output_task:
            self._output_task.cancel()
            self._output_task = None

        if self._process is not None:
            try:
                if self._process.stdin and not self._process.stdin.is_closing():
                    self._process.stdin.close()
                self._process.kill()
                await self._process.wait()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("ffmpeg-Ende fuer %s: %s", self.serial, err)
            self._process = None

        self._buffer.clear()

    # ------------------------------------------------------------------

    def _feed(self, data: bytes) -> None:
        """Ein Videopaket an ffmpeg weiterreichen."""
        if not self.running or self._process is None:
            return

        stdin = self._process.stdin
        if stdin is None or stdin.is_closing():
            return

        try:
            stdin.write(data)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Schreiben nach ffmpeg fehlgeschlagen: %s", err)

    async def _async_read_output(self) -> None:
        """JPEG-Bilder aus der ffmpeg-Ausgabe herausschneiden."""
        assert self._process is not None
        stdout = self._process.stdout
        if stdout is None:
            return

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
                        # Bild noch unvollstaendig - Anfang behalten.
                        if start > 0:
                            del self._buffer[:start]
                        break

                    self.latest_image = bytes(self._buffer[start : end + 2])
                    self.frames_received += 1
                    del self._buffer[: end + 2]

                    if not self._first_frame.is_set():
                        self._first_frame.set()

                if len(self._buffer) > MAX_BUFFER:
                    _LOGGER.debug("Bildpuffer fuer %s verworfen", self.serial)
                    self._buffer.clear()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Ausgabe von ffmpeg beendet: %s", err)
        finally:
            await self._async_log_ffmpeg_errors()

    async def _async_log_ffmpeg_errors(self) -> None:
        """Fehlermeldungen von ffmpeg sichtbar machen."""
        if self._process is None or self._process.stderr is None:
            return
        try:
            async with asyncio.timeout(2):
                data = await self._process.stderr.read(4000)
        except Exception:  # noqa: BLE001
            return

        if data:
            _LOGGER.warning(
                "ffmpeg meldet fuer %s: %s",
                self.serial,
                data.decode(errors="replace").strip(),
            )
