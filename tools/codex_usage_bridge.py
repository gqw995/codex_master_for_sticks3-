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
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


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
PROTOCOL_PREFIX = "USAGE4"


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
            if self.identified and self.screen_awake and payload:
                changed = payload != self.last_payload
                heartbeat_due = now - self.last_send >= 30.0
                if changed or heartbeat_due or refresh_requested:
                    self.device.write_line(payload)
                    self.last_payload = payload
                    self.last_send = now
        except OSError as error:
            self._disconnect(str(error))
        return refresh_requested

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
                ["/sbin/ifconfig"], check=False, capture_output=True, text=True, timeout=5
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
        deadline = time.monotonic() + 20.0
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
        with urllib.request.urlopen(request, timeout=20) as response:
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
    def _read_codex_radar() -> RadarUsage:
        data_error: Exception | None = None
        for url, timeout in ((CODEX_RADAR_LIVE_URL, 10), (CODEX_RADAR_DATA_URL, 20)):
            try:
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": "StickS3-Usage-Bridge/3.0"},
                    method="GET",
                )
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    data = json.load(response)
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

        request = urllib.request.Request(
            CODEX_RADAR_URL,
            headers={"User-Agent": "StickS3-Usage-Bridge/3.0"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            page = response.read().decode("utf-8", errors="replace")

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

    def collect(self, previous: UsageSnapshot | None = None) -> UsageSnapshot:
        host = socket.gethostname().split(".")[0]
        try:
            config = self._vibe_config()
            host = str(config.get("hostname") or host)
        except Exception:
            pass

        remaining, used, reset_epoch, five_hour_remaining, five_hour_reset_epoch = -1, -1, 0, -1, 0
        periods = {days: PeriodUsage() for days in (1, 7, 30)}
        radar = RadarUsage()

        def read_codex() -> tuple[int, int, int, int, int]:
            if self.codex is None:
                self.codex = CodexAppServer(self._find_codex())
            return self.codex.read_usage_limits()

        # Sources are independent. Parallel reads fit within the device's
        # refresh deadline even when one service is slow or unavailable.
        with ThreadPoolExecutor(max_workers=5) as pool:
            codex_result = pool.submit(read_codex)
            period_results = {days: pool.submit(self._read_vibe_period, days) for days in periods}
            radar_result = pool.submit(self._read_codex_radar)
            try:
                remaining, used, reset_epoch, five_hour_remaining, five_hour_reset_epoch = codex_result.result()
            except Exception as error:
                print(f"Codex read failed: {error}", flush=True)
                if self.codex:
                    self.codex.close()
                self.codex = None
            for days, result in period_results.items():
                try:
                    periods[days] = result.result()
                except Exception as error:
                    print(f"Vibe Usage {days}d read failed: {error}", flush=True)
            try:
                radar = radar_result.result()
            except Exception as error:
                print(f"Codex Radar read failed: {error}", flush=True)

        snapshot = UsageSnapshot(
            host=host,
            fetched_epoch=int(time.time()),
            quota_5h_remaining=five_hour_remaining,
            quota_5h_reset_epoch=five_hour_reset_epoch,
            quota_remaining=remaining,
            quota_used=used,
            quota_reset_epoch=reset_epoch,
            usage_24h=periods[1],
            usage_7d=periods[7],
            usage_30d=periods[30],
            radar=radar,
        )
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
    collection_results: queue.Queue[tuple[bool, Any]] = queue.Queue()
    collection_thread: threading.Thread | None = None

    def start_collection(previous: UsageSnapshot | None) -> None:
        nonlocal collection_thread
        if collection_thread and collection_thread.is_alive():
            return

        def collect_in_background() -> None:
            try:
                collection_results.put((True, collector.collect(previous)))
            except Exception as error:
                collection_results.put((False, error))

        collection_thread = threading.Thread(target=collect_in_background, daemon=True)
        collection_thread.start()

    try:
        while True:
            now = time.monotonic()
            collection_completed = False

            try:
                completed, result = collection_results.get_nowait()
            except queue.Empty:
                pass
            else:
                collection_thread = None
                if completed:
                    snapshot = result
                    payload = snapshot.wire_payload()
                    next_wifi = 0.0
                    collection_completed = True
                else:
                    print(f"Metrics collection failed: {result}", flush=True)

            collecting = bool(collection_thread and collection_thread.is_alive())
            usb_refresh_requested = usb.tick(None if collecting else payload) if usb else False
            wifi_refresh_requested = wifi.poll(snapshot.host if snapshot else "") if wifi else False
            refresh_requested = usb_refresh_requested or wifi_refresh_requested
            if wifi_refresh_requested:
                next_wifi = 0.0
            if usb and usb.identified:
                device_sleeping = not usb.screen_awake
            elif wifi and wifi.screen_awake is not None:
                device_sleeping = not wifi.screen_awake
            else:
                device_sleeping = bool(usb and not usb.screen_awake)
            # Retry packets received while a collection ran are fulfilled by
            # that result. Deliver it before considering another collection.
            if not device_sleeping and not collection_completed and (now >= next_poll or refresh_requested) and not (
                collection_thread and collection_thread.is_alive()
            ):
                start_collection(snapshot)
                next_poll = now + max(30, args.interval)

            collecting = bool(collection_thread and collection_thread.is_alive())
            if not device_sleeping and not collecting and wifi and payload and now >= next_wifi:
                try:
                    wifi.send(payload)
                except OSError as error:
                    print(f"Wi-Fi broadcast failed: {error}", flush=True)
                next_wifi = now + max(3, args.wifi_interval)

            if args.once and payload and now - started >= 7.0:
                return 0
            time.sleep(0.1)
    except KeyboardInterrupt:
        return 0
    finally:
        if usb:
            usb.close()
        if wifi:
            wifi.close()
        collector.close()


if __name__ == "__main__":
    raise SystemExit(main())
