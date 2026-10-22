#!/usr/bin/env python3
"""Stick S3 usage bridge: Codex quota + Vibe Usage over USB and Wi-Fi.

The bridge intentionally keeps all account credentials on the Mac. It reads the
existing Codex and Vibe Usage login state, then sends only aggregate numbers to
the Stick S3.
"""

from __future__ import annotations

import argparse
import errno
import glob
import gzip
import html
import json
import os
import queue
import re
import select
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


CODEX_BIN_CANDIDATES = tuple(
    root / app / "Contents/Resources" / relative
    for root in (Path("/Applications"), Path.home() / "Applications")
    for app in ("ChatGPT.app", "Codex.app")
    for relative in ("codex-cli/CodexCLI.app/Contents/MacOS/codex", "codex-cli/bin/codex", "codex")
)
DEFAULT_VIBE_API = "https://vibecafe.ai"
CODEX_RADAR_URL = "https://codexradar.com/"
CODEX_RADAR_LIVE_URL = "https://codexradar.com/api/intelligence-efficiency-metrics"
CODEX_RADAR_DATA_URL = "https://codexradar.com/data/intelligence-efficiency.json"
WIFI_BROADCAST_PORT = 47322
PROTOCOL_PREFIX = "USAGE5"


def safe_host_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", value)[:28] or "Mac"


@dataclass(frozen=True)
class PeriodUsage:
    cost_cents: int = -1
    total_tokens: int = -1


@dataclass(frozen=True)
class RadarModel:
    name: str
    score_tenths: int


@dataclass(frozen=True)
class RadarUsage:
    updated: str = ""
    models: tuple[RadarModel, ...] = ()


@dataclass(frozen=True)
class UsageSnapshot:
    host: str
    fetched_epoch: int
    quota_5h_remaining: int = -1
    quota_5h_reset_epoch: int = 0
    quota_remaining: int = -1
    quota_used: int = -1
    quota_reset_epoch: int = 0
    usage_24h: PeriodUsage = PeriodUsage()
    usage_7d: PeriodUsage = PeriodUsage()
    usage_30d: PeriodUsage = PeriodUsage()
    radar: RadarUsage = RadarUsage()
    pending_mask: int = 0
    codex_fetched_epoch: int = 0
    vibe24_fetched_epoch: int = 0
    vibe7_fetched_epoch: int = 0
    vibe30_fetched_epoch: int = 0
    radar_fetched_epoch: int = 0

    def wire_payload(self) -> str:
        safe_host = safe_host_name(self.host)
        fields = [
            PROTOCOL_PREFIX,
            safe_host,
            str(self.fetched_epoch),
            str(self.quota_remaining),
            str(self.quota_used),
            str(self.quota_reset_epoch),
            str(self.usage_24h.cost_cents),
            str(self.usage_24h.total_tokens),
            str(self.usage_7d.cost_cents),
            str(self.usage_7d.total_tokens),
            str(self.usage_30d.cost_cents),
            str(self.usage_30d.total_tokens),
            re.sub(r"[^A-Z0-9/: -]", "", self.radar.updated)[:18],
            str(min(12, len(self.radar.models))),
        ]
        for model in self.radar.models[:12]:
            safe_name = re.sub(r"[^A-Za-z0-9 ._-]", "", model.name)[:18] or "Model"
            fields.extend((safe_name, str(model.score_tenths)))
        fields.extend((str(self.quota_5h_remaining), str(self.quota_5h_reset_epoch)))
        fields.extend(str(value) for value in (
            self.pending_mask, self.codex_fetched_epoch, self.vibe24_fetched_epoch,
            self.vibe7_fetched_epoch, self.vibe30_fetched_epoch, self.radar_fetched_epoch,
        ))
        return ",".join(fields)


class PosixSerialDevice:
    """Line-oriented USB serial transport that never toggles DTR or RTS."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._buffer = bytearray()
        self._fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)

    def read_lines(self) -> list[str]:
        lines: list[str] = []
        while select.select([self._fd], [], [], 0)[0]:
            try:
                chunk = os.read(self._fd, 4096)
            except OSError as error:
                if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break
                raise
            if not chunk:
                raise OSError(errno.ENODEV, "USB serial device disconnected")
            self._buffer.extend(chunk)

        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            raw = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            lines.append(raw.decode("utf-8", errors="replace").strip())
        return lines

    def write_line(self, value: str) -> None:
        data = (value + "\n").encode("ascii")
        written = 0
        while written < len(data):
            written += os.write(self._fd, data[written:])

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass


class UsbHotplugManager:
    """Discovers the Stick, reconnects after unplug/replug, and syncs data."""

    def __init__(self, explicit_port: str | None = None) -> None:
        self.explicit_port = explicit_port
        self.device: PosixSerialDevice | None = None
        self.identified = False
        self.screen_awake = True
        self.opened_at = 0.0
        self.last_scan = 0.0
        self.last_send = 0.0
        self.last_payload = ""
        self.path: str | None = None
        self.rejected_until: dict[str, float] = {}
        self.last_ack_log = 0.0

    def _candidate_ports(self) -> list[str]:
        if self.explicit_port:
            return [self.explicit_port] if os.path.exists(self.explicit_port) else []
        now = time.monotonic()
        return [
            path for path in sorted(glob.glob("/dev/cu.usbmodem*"))
            if self.rejected_until.get(path, 0.0) <= now
        ]

    def _disconnect(self, reason: str, reject_candidate: bool = False) -> None:
        old_path = self.path
        if self.device:
            self.device.close()
        if self.path:
            print(f"USB disconnected ({self.path}): {reason}", flush=True)
        self.device = None
        self.path = None
        self.identified = False
        self.last_payload = ""
        self.last_send = 0.0
        if reject_candidate and old_path and not self.explicit_port:
            self.rejected_until[old_path] = time.monotonic() + 60.0

    def _try_open(self) -> None:
        now = time.monotonic()
        if now - self.last_scan < 1.0:
            return
        self.last_scan = now
        for path in self._candidate_ports():
            try:
                self.device = PosixSerialDevice(path)
                self.path = path
                self.opened_at = now
                self.identified = False
                print(f"USB candidate opened: {path}", flush=True)
                return
            except OSError:
                continue

    def tick(self, payload: str | None) -> bool:
        """Run one hotplug cycle. Returns True when the device requests refresh."""
        if not self.device:
            self._try_open()
            return False

        refresh_requested = False
        try:
            for line in self.device.read_lines():
                if line.startswith(("STICKS3_READY", "STICKS3_ALIVE", "CODEX_READY", "CODEX_ALIVE")):
                    self.screen_awake = True
                    if not self.identified:
                        self.identified = True
                        print(f"Stick S3 identified on {self.path}", flush=True)
                elif line == "STICKS3_SCREEN,OFF":
                    self.screen_awake = False
                    print("Stick S3 entered low-power sleep", flush=True)
                elif line == "STICKS3_SCREEN,ON":
                    self.screen_awake = True
                elif line in ("STICKS3_REFRESH", "CODEX_REFRESH"):
                    refresh_requested = True
                elif line == "STICKS3_REFRESH_DONE":
                    print("Stick S3 confirmed refresh response", flush=True)
                elif line.startswith(("STICKS3_STATUS,", "STICKS3_BUTTON,", "STICKS3_PAGE,",
                                      "STICKS3_POWER,", "STICKS3_BATTERY,", "STICKS3_DATA,")):
                    print(line, flush=True)
                elif line == "USAGE_ACK":
                    now = time.monotonic()
                    if self.last_ack_log == 0.0 or now - self.last_ack_log >= 300.0:
                        print("Stick S3 acknowledged USB update", flush=True)
                        self.last_ack_log = now

            now = time.monotonic()
            if not self.identified and now - self.opened_at > 5.0:
                self._disconnect("not a Stick S3 usage display", reject_candidate=True)
                return False

            if self.identified and refresh_requested:
                # A cached payload or early ACK would make old firmware report
                # success before any of the requested sources have been read.
                print("Stick S3 refresh requested; collecting new data", flush=True)
                return True
            self.send(payload)
        except OSError as error:
            self._disconnect(str(error))
        return refresh_requested

    def send(self, payload: str | None) -> None:
        if not self.device or not self.identified or not self.screen_awake or not payload:
            return
        now = time.monotonic()
        if payload != self.last_payload or now - self.last_send >= 30.0:
            try:
                self.device.write_line(payload)
                self.last_payload = payload
                self.last_send = now
            except OSError as error:
                self._disconnect(str(error))

    def close(self) -> None:
        if self.device:
            self.device.close()


class WifiBroadcaster:
    def __init__(self, port: int = WIFI_BROADCAST_PORT) -> None:
        self.port = port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self.socket.bind(("", port))
        self.socket.setblocking(False)
        self._targets: list[str] = []
        self._targets_updated = 0.0
        self.screen_awake: bool | None = None

    def _broadcast_targets(self) -> list[str]:
        now = time.monotonic()
        if self._targets and now - self._targets_updated < 60.0:
            return self._targets
        targets = ["255.255.255.255"]
        try:
            output = subprocess.run(
                ["/sbin/ifconfig"], check=False, capture_output=True, text=True, timeout=1
            ).stdout
            targets.extend(re.findall(r"\binet\s+\d+(?:\.\d+){3}.*?\bbroadcast\s+(\d+(?:\.\d+){3})", output))
        except (OSError, subprocess.SubprocessError):
            pass
        self._targets = list(dict.fromkeys(targets))
        self._targets_updated = now
        return self._targets

    def send(self, payload: str) -> None:
        data = (payload + "\n").encode("ascii")
        errors: list[OSError] = []
        delivered = False
        for address in self._broadcast_targets():
            try:
                self.socket.sendto(data, (address, self.port))
                delivered = True
            except OSError as error:
                errors.append(error)
        if not delivered and errors:
            raise errors[-1]

    def poll(self, expected_host: str = "") -> bool:
        refresh_requested = False
        safe_expected_host = safe_host_name(expected_host) if expected_host else ""
        while True:
            try:
                data, _ = self.socket.recvfrom(1024)
            except BlockingIOError:
                break
            line = data.decode("ascii", errors="ignore").strip()
            event, separator, target_host = line.partition(",")
            if event not in ("STICKS3_REFRESH", "STICKS3_SCREEN_ON", "STICKS3_SCREEN_OFF"):
                continue
            if separator and target_host and safe_expected_host and target_host != safe_expected_host:
                continue
            if event == "STICKS3_SCREEN_OFF":
                if self.screen_awake is not False:
                    print("Stick S3 entered low-power sleep over Wi-Fi", flush=True)
                self.screen_awake = False
            else:
                self.screen_awake = True
                if event == "STICKS3_REFRESH":
                    if not refresh_requested:
                        print("Stick S3 wireless refresh request received", flush=True)
                    refresh_requested = True
        return refresh_requested

    def close(self) -> None:
        self.socket.close()


class CodexAppServer:
    def __init__(self, executable: str) -> None:
        self._next_id = 1
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._process = subprocess.Popen(
            [executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "stick-s3-usage-bridge",
                    "title": "Stick S3 Usage Bridge",
                    "version": "2.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self._write({"method": "initialized"})

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in message:
                self._responses.put(message)

    def _drain_stderr(self) -> None:
        assert self._process.stderr is not None
        for _ in self._process.stderr:
            pass

    def _write(self, message: dict[str, Any]) -> None:
        if self._process.poll() is not None:
            raise RuntimeError("Codex app-server exited unexpectedly")
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self._process.stdin.flush()

    def _request(self, method: str, params: Any = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)
        deadline = time.monotonic() + (8.0 if method == "account/rateLimits/read" else 4.0)
        while time.monotonic() < deadline:
            try:
                response = self._responses.get(timeout=0.5)
            except queue.Empty:
                continue
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError(f"Codex error: {response['error']}")
            return response.get("result", {})
        raise TimeoutError(f"Timed out waiting for Codex method {method}")

    @staticmethod
    def _parse_rate_limits(result: dict[str, Any]) -> tuple[int, int, int, int, int]:
        buckets = result.get("rateLimitsByLimitId")
        if isinstance(buckets, dict):
            snapshot = buckets.get("codex") or {}
        else:
            snapshot = result.get("rateLimits") or {}
        if snapshot.get("limitId") not in (None, "codex"):
            snapshot = {}
        windows = [snapshot.get("primary"), snapshot.get("secondary")]
        windows = [window for window in windows if window and window.get("windowDurationMins")]
        if not windows:
            raise RuntimeError("Codex did not return a rate-limit window")

        def read_window(duration: int) -> tuple[int, int, int]:
            window = next(
                (item for item in windows if int(item.get("windowDurationMins") or 0) == duration),
                None,
            )
            if window is None or window.get("usedPercent") is None:
                return -1, -1, 0
            used = max(0, min(100, int(window["usedPercent"])))
            return 100 - used, used, int(window.get("resetsAt") or 0)

        weekly_remaining, weekly_used, weekly_reset = read_window(10080)
        five_hour_remaining, _, five_hour_reset = read_window(300)
        return weekly_remaining, weekly_used, weekly_reset, five_hour_remaining, five_hour_reset

    def read_usage_limits(self) -> tuple[int, int, int, int, int]:
        return self._parse_rate_limits(self._request("account/rateLimits/read"))

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()


class MetricsCollector:
    def __init__(self) -> None:
        self.codex: CodexAppServer | None = None

    @staticmethod
    def _find_codex() -> str:
        override = os.environ.get("CODEX_USAGE_CODEX_BIN")
        if override:
            if os.access(override, os.X_OK):
                return override
            raise FileNotFoundError("CODEX_USAGE_CODEX_BIN is not executable")
        discovered = shutil.which("codex")
        if discovered:
            return discovered
        for candidate in CODEX_BIN_CANDIDATES:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        raise FileNotFoundError("Could not find the Codex CLI")

    @staticmethod
    def _vibe_config() -> dict[str, Any]:
        path = Path(os.environ.get("VIBE_USAGE_CONFIG", "~/.vibe-usage/config.json")).expanduser()
        with path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
        if not config.get("apiKey"):
            raise RuntimeError("Vibe Usage is not linked on this Mac")
        return config

    @staticmethod
    def _read_vibe_period(days: int) -> PeriodUsage:
        config = MetricsCollector._vibe_config()
        base_url = config.get("apiUrl") or DEFAULT_VIBE_API
        query = urllib.parse.urlencode({"days": days, "tz": os.environ.get("TZ", "Asia/Shanghai")})
        url = urllib.parse.urljoin(base_url, "/api/usage") + "?" + query
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {config['apiKey']}"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=6) as response:
            data = json.load(response)

        total_cost = 0.0
        total_tokens = 0
        for bucket in data.get("buckets") or []:
            total_cost += float(bucket.get("estimatedCost") or 0)
            # Match the macOS App's UsageBucket.computedTotal exactly. The CLI
            # summary's `totalTokens` omits cached input on current data.
            total_tokens += sum(
                int(bucket.get(key) or 0)
                for key in ("inputTokens", "outputTokens", "reasoningOutputTokens", "cachedInputTokens")
            )
        return PeriodUsage(cost_cents=round(total_cost * 100), total_tokens=total_tokens)

    @staticmethod
    def _read_codex_radar(cancel_event: threading.Event | None = None) -> RadarUsage:
        deadline = time.monotonic() + 8.0

        def read_url(url: str) -> bytes:
            def check_deadline() -> float:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("Radar refresh cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Radar refresh exceeded 8 seconds")
                return remaining

            remaining = check_deadline()
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "StickS3-Usage-Bridge/3.0", "Accept-Encoding": "gzip"},
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=min(3.0, remaining)) as response:
                chunks: list[bytes] = []
                while True:
                    check_deadline()
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                check_deadline()
                compressed = response.headers.get("Content-Encoding", "").lower() == "gzip"
            body = b"".join(chunks)
            return gzip.decompress(body) if compressed else body

        data_error: Exception | None = None
        for url in (CODEX_RADAR_LIVE_URL, CODEX_RADAR_DATA_URL):
            try:
                data = json.loads(read_url(url))
                if data.get("benchmark_id") and data["benchmark_id"] != "deep-swe":
                    raise RuntimeError("Unexpected Codex Radar benchmark cohort")

                source = data.get("source_updated_at") or data.get("generated_at")
                source_values = source.values() if isinstance(source, dict) else (source,)
                source_dates: list[datetime] = []
                for value in source_values:
                    try:
                        source_date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if source_date.tzinfo is None:
                        source_date = source_date.replace(tzinfo=timezone.utc)
                    source_dates.append(source_date)
                if not source_dates:
                    raise RuntimeError("Codex Radar source update time was not found")
                updated_at = max(source_dates).astimezone(timezone(timedelta(hours=8)))

                model_names = {
                    "gpt-5.6-sol": "Sol",
                    "gpt-5.6-terra": "Terra",
                    "gpt-5.6-luna": "Luna",
                    "gpt-5.5": "5.5",
                }
                models: list[RadarModel] = []
                for point in data.get("points") or []:
                    model_id = str(point.get("model") or "").strip()
                    effort = str(point.get("effort") or "").strip()
                    iq = point.get("iq")
                    if not model_id or not effort or iq is None:
                        continue
                    model_name = model_names.get(model_id)
                    if not model_name:
                        model_name = model_id.removeprefix("gpt-").replace("-", " ").title()
                    models.append(
                        RadarModel(name=f"{model_name} {effort}", score_tenths=round(float(iq) * 10))
                    )
                if not models:
                    raise RuntimeError("Codex Radar model scores were not found")
                models.sort(key=lambda model: (-model.score_tenths, model.name.lower()))
                updated = updated_at.strftime("%m/%d %H:%M")
                if time.time() - updated_at.timestamp() > 48 * 3600:
                    updated += " OLD"
                return RadarUsage(updated=updated, models=tuple(models[:12]))
            except Exception as error:
                data_error = error

        page = read_url(CODEX_RADAR_URL).decode("utf-8", errors="replace")

        section_match = re.search(
            r'<section\s+class="model-iq model-iq-green".*?</section>',
            page,
            flags=re.DOTALL,
        )
        if not section_match:
            raise RuntimeError(
                f"Codex Radar data endpoint failed ({data_error}); legacy score section was not found"
            ) from data_error
        section = section_match.group(0)
        updated_match = re.search(
            r'<h2>\s*降智雷达\s*<span>(\d{1,2})月(\d{1,2})日(\d{1,2}):(\d{2})更新</span>',
            section,
        )
        card_pattern = re.compile(
            r'class="model-iq-score-chip[^\"]*"[^>]*>\s*'
            r'<span>([^<]+)</span>\s*'
            r'<div\s+class="model-iq-score-metrics"><strong>([0-9]+(?:\.[0-9]+)?)</strong>',
            flags=re.DOTALL,
        )
        if not updated_match:
            raise RuntimeError("Codex Radar update time was not found")
        month, day, hour, minute = (int(value) for value in updated_match.groups())
        updated = f"{month:02d}/{day:02d} {hour:02d}:{minute:02d}"
        models: list[RadarModel] = []
        for raw_name, raw_score in card_pattern.findall(section):
            name = " ".join(html.unescape(raw_name).split())
            if name.startswith("GPT-"):
                name = name.removeprefix("GPT-")
            models.append(RadarModel(name=name, score_tenths=round(float(raw_score) * 10)))
        if not models:
            raise RuntimeError("Codex Radar model scores were not found")
        models.sort(key=lambda model: (-model.score_tenths, model.name.lower()))
        return RadarUsage(updated=updated, models=tuple(models[:12]))

    def collect(
        self,
        previous: UsageSnapshot | None = None,
        on_progress: Callable[[UsageSnapshot], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> UsageSnapshot:
        host = socket.gethostname().split(".")[0]
        try:
            config = self._vibe_config()
            host = str(config.get("hostname") or host)
        except Exception:
            pass

        cancelled = cancel_event if cancel_event is not None else threading.Event()
        snapshot = UsageSnapshot(host=host, fetched_epoch=int(time.time()), pending_mask=31)

        def publish() -> None:
            if on_progress is not None and not cancelled.is_set():
                on_progress(snapshot)

        if cancelled.is_set():
            return replace(snapshot, pending_mask=0)
        publish()

        def read_codex() -> tuple[int, int, int, int, int]:
            try:
                if self.codex is None:
                    self.codex = CodexAppServer(self._find_codex())
                return self.codex.read_usage_limits()
            except Exception:
                if self.codex:
                    self.codex.close()
                self.codex = None
                raise

        def read_source(name: str, reader: Callable[[], Any]) -> Any:
            started = time.monotonic()
            try:
                if cancelled.is_set():
                    raise RuntimeError("refresh cancelled")
                return reader()
            finally:
                print(f"Source {name} finished in {time.monotonic() - started:.2f}s", flush=True)

        # Publish each source independently so a slow radar never holds up
        # quota or usage data. Unfinished values stay explicitly unknown.
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(read_source, "Codex", read_codex): (0, "Codex")}
            for bit, days in enumerate((1, 7, 30), start=1):
                future = pool.submit(read_source, f"Vibe {days}d", lambda days=days: self._read_vibe_period(days))
                futures[future] = (bit, f"Vibe {days}d")
            radar_result = pool.submit(
                read_source, "Radar", lambda: self._read_codex_radar(cancelled),
            )
            futures[radar_result] = (4, "Radar")
            for future in as_completed(futures):
                if cancelled.is_set():
                    for pending in futures:
                        pending.cancel()
                    break
                bit, name = futures[future]
                fields: dict[str, Any] = {
                    "fetched_epoch": int(time.time()),
                    "pending_mask": snapshot.pending_mask & ~(1 << bit),
                }
                try:
                    result = future.result()
                    completed_at = fields["fetched_epoch"]
                    if bit == 0:
                        remaining, used, reset_epoch, five_hour_remaining, five_hour_reset_epoch = result
                        fields.update(
                            quota_remaining=remaining, quota_used=used, quota_reset_epoch=reset_epoch,
                            quota_5h_remaining=five_hour_remaining, quota_5h_reset_epoch=five_hour_reset_epoch,
                            codex_fetched_epoch=completed_at,
                        )
                    elif bit == 4:
                        fields.update(radar=result, radar_fetched_epoch=completed_at)
                    else:
                        period_name, epoch_name = {
                            1: ("usage_24h", "vibe24_fetched_epoch"),
                            2: ("usage_7d", "vibe7_fetched_epoch"),
                            3: ("usage_30d", "vibe30_fetched_epoch"),
                        }[bit]
                        fields.update({period_name: result, epoch_name: completed_at})
                except Exception as error:
                    print(f"{name} read failed: {error}", flush=True)
                snapshot = replace(snapshot, **fields)
                publish()

        if cancelled.is_set():
            return UsageSnapshot(host=host, fetched_epoch=int(time.time()))
        print(
            "Updated: "
            f"Codex 5H {snapshot.quota_5h_remaining}% / 7D {snapshot.quota_remaining}% left; "
            f"24H ${snapshot.usage_24h.cost_cents / 100:.2f}/{snapshot.usage_24h.total_tokens:,}; "
            f"7D ${snapshot.usage_7d.cost_cents / 100:.2f}/{snapshot.usage_7d.total_tokens:,}; "
            f"30D ${snapshot.usage_30d.cost_cents / 100:.2f}/{snapshot.usage_30d.total_tokens:,}; "
            f"Radar {snapshot.radar.updated}/{len(snapshot.radar.models)} models",
            flush=True,
        )
        return snapshot

    def close(self) -> None:
        if self.codex:
            self.codex.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", help="limit USB discovery to one serial port")
    parser.add_argument("--interval", type=int, default=300, help="source refresh interval in seconds")
    parser.add_argument("--wifi-interval", type=int, default=10, help="Wi-Fi broadcast interval in seconds")
    parser.add_argument("--no-usb", action="store_true", help="disable USB transport")
    parser.add_argument("--no-wifi", action="store_true", help="disable Wi-Fi broadcast")
    parser.add_argument("--once", action="store_true", help="collect once, transmit briefly, and exit")
    args = parser.parse_args()

    collector = MetricsCollector()
    usb = None if args.no_usb else UsbHotplugManager(args.port)
    wifi = None if args.no_wifi else WifiBroadcaster()
    snapshot: UsageSnapshot | None = None
    payload: str | None = None
    next_poll = 0.0
    next_wifi = 0.0
    started = time.monotonic()
    collection_results: queue.Queue[tuple[int, str, Any]] = queue.Queue()
    collection_thread: threading.Thread | None = None
    collection_cancel: threading.Event | None = None
    collection_generation = 0
    refresh_pending = False
    was_awake = False

    def start_collection(previous: UsageSnapshot | None) -> None:
        nonlocal collection_thread, collection_cancel, collection_generation, payload
        if collection_thread is not None:
            return
        collection_generation += 1
        generation = collection_generation
        cancel_event = threading.Event()
        collection_cancel = cancel_event
        # Old cached values must never satisfy this new refresh request.
        payload = None

        def progress(result: UsageSnapshot) -> None:
            if not cancel_event.is_set():
                collection_results.put((generation, "progress", result))

        def collect_in_background() -> None:
            try:
                result = collector.collect(previous, on_progress=progress, cancel_event=cancel_event)
                if not cancel_event.is_set():
                    collection_results.put((generation, "done", result))
            except Exception as error:
                if not cancel_event.is_set():
                    collection_results.put((generation, "error", error))

        collection_thread = threading.Thread(target=collect_in_background, daemon=True)
        collection_thread.start()

    try:
        while True:
            now = time.monotonic()
            collection_completed = False

            if (collection_thread is not None and collection_cancel is not None
                    and collection_cancel.is_set() and not collection_thread.is_alive()):
                collection_thread = None
            try:
                generation, event, result = collection_results.get_nowait()
            except queue.Empty:
                pass
            else:
                # Keep the first pending packet visible even when every
                # source finishes within one main-loop tick. It identifies
                # a new refresh even when Unix timestamps share a second.
                valid_result = generation == collection_generation and not (
                    collection_cancel and collection_cancel.is_set()
                )
                if not valid_result:
                    event = "discarded"
                if event in ("progress", "done"):
                    snapshot = result
                    payload = snapshot.wire_payload()
                    next_wifi = 0.0
                    if event == "done":
                        collection_completed = True
                        collection_thread = None
                elif event == "error":
                    collection_thread = None
                    print(f"Metrics collection failed: {result}", flush=True)

            usb_refresh_requested = usb.tick(None) if usb else False
            wifi_refresh_requested = wifi.poll(snapshot.host if snapshot else "") if wifi else False
            refresh_requested = usb_refresh_requested or wifi_refresh_requested
            if wifi_refresh_requested:
                next_wifi = 0.0
            device_awake = bool(
                usb.screen_awake if usb and usb.identified else wifi and wifi.screen_awake is True
            )
            collecting = collection_thread is not None
            if device_awake and not was_awake:
                refresh_pending = True
            if refresh_requested and not collection_completed and (
                not collecting or (collection_cancel is not None and collection_cancel.is_set())
            ):
                refresh_pending = True
            was_awake = device_awake
            if not device_awake and not args.once:
                if collection_cancel:
                    collection_cancel.set()
                payload = None
            # Retry packets received while a collection ran are fulfilled by
            # that result. Deliver it before considering another collection.
            if (device_awake or args.once) and not collection_completed and (now >= next_poll or refresh_pending) and not (
                collection_thread is not None
            ):
                start_collection(snapshot)
                refresh_pending = False
                next_poll = now + max(30, args.interval)

            if device_awake and usb:
                usb.send(payload)
            if (device_awake or args.once) and wifi and payload and now >= next_wifi:
                try:
                    wifi.send(payload)
                except OSError as error:
                    print(f"Wi-Fi broadcast failed: {error}", flush=True)
                next_wifi = now + max(3, args.wifi_interval)

            if args.once and payload and snapshot and snapshot.pending_mask == 0 and now - started >= 7.0:
                return 0
            time.sleep(0.1)
    except KeyboardInterrupt:
        return 0
    finally:
        if collection_cancel:
            collection_cancel.set()
        if usb:
            usb.close()
        if wifi:
            wifi.close()
        collector.close()


if __name__ == "__main__":
    raise SystemExit(main())
