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
import json
import math
import os
import queue
import re
import select
import shutil
import socket
import subprocess
import threading
import time
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
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
RADAR_BENCH_BINDING_URL = "https://codexradar.com/data/radar-bench-binding.json"
RADAR_BENCH_SCORE_URL = "https://codexradar.com/api/radar-bench-score"
RADAR_BENCH_BENCHMARK = "radar-bench-final64-20261004"
RADAR_BENCH_SCORE_VERSION = "radar-bench-v2-150"
RADAR_BENCH_SUMMARY_SCHEMA = "radar-bench-public-summary-v6-150-details"
RADAR_BENCH_CACHE_SCHEMA = "sticks3-radar-bench-cache-v1"
WIFI_BROADCAST_PORT = 47322
PROTOCOL_PREFIX = "USAGE6"


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
    fetched_epoch: int = 0
    cached: bool = False


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
        fields.append("1" if self.radar.cached else "0")
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


class RadarBenchReader:
    """Keep only complete, validated public Bench rankings as display cache."""

    def __init__(self, cache_path: Path | None = None) -> None:
        self.cache_path = cache_path or (
            Path.home() / "Library/Application Support/StickS3 Usage Bridge/radar-bench-cache.json"
        )
        self.binding: dict[str, Any] | None = None
        self._entries: dict[tuple[str, str], dict[str, Any]] = {}
        self._cycle_started_epoch = 0
        self._cache: dict[str, Any] | None = None
        self.completion_epoch = 0
        try:
            with self.cache_path.open("r", encoding="utf-8") as handle:
                record = json.load(handle)
            if not isinstance(record, dict) or record.get("schema") != RADAR_BENCH_CACHE_SCHEMA:
                raise ValueError("unexpected cache schema")
            binding = self._validate_binding(record["binding"])
            entries = self._validate_entries(record["entries"], binding)
            completion = record["completion_epoch"]
            if (type(completion) is not int or completion > int(time.time()) + 60
                    or completion < max(e["read_epoch"] for e in entries)):
                raise ValueError("invalid completion time")
            self._cache = {"binding": binding, "entries": entries}
            self.completion_epoch = completion
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"Radar Bench cache ignored: {error}", flush=True)

    @staticmethod
    def _validate_binding(value: dict[str, Any]) -> dict[str, Any]:
        if (not isinstance(value, dict)
                or value.get("enabled") is not True or value.get("production_read_verified") is not True
                or value.get("benchmark") != RADAR_BENCH_BENCHMARK
                or value.get("score_version") != RADAR_BENCH_SCORE_VERSION
                or value.get("score_max") != 150 or value.get("total_tasks") != 64
                or not isinstance(value.get("catalog_version"), str) or not value["catalog_version"]
                or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("task_set_sha256", "")))):
            raise ValueError("invalid Radar Bench binding")
        selections = value.get("model_efforts")
        if not isinstance(selections, list) or not selections:
            raise ValueError("Radar Bench selections missing")
        keys: set[tuple[str, str]] = set()
        for selection in selections:
            if (not isinstance(selection, dict) or set(selection) != {"model", "effort"}
                    or not all(isinstance(selection[k], str) and selection[k] for k in ("model", "effort"))):
                raise ValueError("invalid Radar Bench selection")
            key = (selection["model"], selection["effort"])
            if key in keys:
                raise ValueError("duplicate Radar Bench selection")
            keys.add(key)
        return {key: value[key] for key in (
            "enabled", "production_read_verified", "benchmark", "catalog_version", "task_set_sha256",
            "score_version", "score_max", "total_tasks", "model_efforts",
        )}

    @staticmethod
    def _grade_epoch(value: str) -> float:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Radar Bench grade time needs a timezone")
        return parsed.timestamp()

    @classmethod
    def _validate_summary(
        cls, value: dict[str, Any], binding: dict[str, Any], selection: tuple[str, str],
    ) -> dict[str, Any]:
        if (not isinstance(value, dict) or value.get("schema") != RADAR_BENCH_SUMMARY_SCHEMA
                or any(value.get(k) != binding[k] for k in (
                    "benchmark", "catalog_version", "task_set_sha256", "score_version", "score_max"))
                or (value.get("model"), value.get("effort")) != selection):
            raise ValueError("Radar Bench summary identity mismatch")
        coverage, required, score = value.get("coverage"), value.get("required_tasks"), value.get("score")
        if (type(coverage) is not int or type(required) is not int
                or required != binding["total_tasks"] or not 0 <= coverage <= required):
            raise ValueError("invalid Radar Bench coverage")
        if score is None:
            if coverage != 0:
                raise ValueError("covered Radar Bench result has no score")
        elif (isinstance(score, bool) or not isinstance(score, (int, float))
              or not math.isfinite(score) or not 0 <= score <= 150 or coverage == 0):
            raise ValueError("invalid Radar Bench score")
        status = "missing_current_result" if coverage == 0 else "complete" if coverage == required else "provisional"
        if value.get("score_status") != status:
            raise ValueError("Radar Bench score status disagrees with coverage")
        detail = value.get("detail_metrics") or {}
        if not isinstance(detail, dict):
            raise ValueError("invalid Radar Bench grade metadata")
        latest = detail.get("latest_graded_at")
        if latest is not None:
            if not isinstance(latest, str):
                raise ValueError("invalid Radar Bench grade time")
            cls._grade_epoch(latest)
        if score is not None and latest is None:
            raise ValueError("Radar Bench score has no actual grade time")
        return {**{key: value[key] for key in (
            "schema", "benchmark", "catalog_version", "task_set_sha256", "score_version", "score_max",
            "model", "effort", "score", "coverage", "required_tasks", "score_status",
        )}, "detail_metrics": {"latest_graded_at": latest}}

    @classmethod
    def _validate_entries(cls, values: list[dict[str, Any]], binding: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            raise ValueError("invalid Radar Bench cache entries")
        expected = {(s["model"], s["effort"]) for s in binding["model_efforts"]}
        entries: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in values:
            if not isinstance(entry, dict) or not isinstance(entry.get("summary"), dict):
                raise ValueError("invalid Radar Bench cache entry")
            summary = entry["summary"]
            key = (summary["model"], summary["effort"])
            if key not in expected or key in entries or type(entry["read_epoch"]) is not int or entry["read_epoch"] <= 0:
                raise ValueError("invalid Radar Bench cache entry")
            entries[key] = {"read_epoch": entry["read_epoch"], "summary": cls._validate_summary(summary, binding, key)}
        if set(entries) != expected:
            raise ValueError("incomplete Radar Bench cache")
        return list(entries.values())

    @staticmethod
    def _usage(entries: list[dict[str, Any]], cached: bool) -> RadarUsage:
        ranked = [e["summary"] for e in entries if e["summary"]["score"] is not None]
        ranked.sort(key=lambda s: (-s["score"], s["model"], s["effort"]))
        top = ranked[:6]
        models = tuple(RadarModel(
            f"{s['model'].removeprefix('gpt-').replace('-', ' ').title()} {s['effort']}",
            int(math.floor(s["score"] * 10 + 0.5)),
        ) for s in top)
        grade_times = [RadarBenchReader._grade_epoch(s["detail_metrics"]["latest_graded_at"]) for s in top]
        updated = ""
        if grade_times:
            latest = max(grade_times)
            updated = datetime.fromtimestamp(latest, timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
            if time.time() - latest > 48 * 3600:
                updated += " OLD"
        return RadarUsage(updated, models, min(e["read_epoch"] for e in entries), cached)

    def cached_usage(self) -> RadarUsage:
        return self._usage(self._cache["entries"], True) if self._cache else RadarUsage()

    def _read_json(self, url: str, cancel_event: threading.Event, deadline: float) -> dict[str, Any]:
        def remaining() -> float:
            if cancel_event.is_set():
                raise RuntimeError("Radar Bench refresh cancelled")
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError("Radar Bench refresh budget exhausted")
            return budget

        request = urllib.request.Request(url, headers={
            "User-Agent": "StickS3-Usage-Bridge/6.0", "Accept-Encoding": "gzip",
            "X-Radar-Bench-Selection": "user-selected-v1",
        })
        with urllib.request.urlopen(request, timeout=min(3.0, remaining())) as response:
            chunks: list[bytes] = []
            while True:
                remaining()
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            remaining()
            body = b"".join(chunks)
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
        return json.loads(body)

    def _save_cache(self, binding: dict[str, Any], entries: list[dict[str, Any]], completion: int) -> None:
        record = {"schema": RADAR_BENCH_CACHE_SCHEMA, "binding": binding, "entries": entries,
                  "completion_epoch": completion}
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.cache_path.parent,
                prefix=self.cache_path.name + ".", delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(record, handle, separators=(",", ":"))
            os.replace(temp_path, self.cache_path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def refresh(self, cancel_event: threading.Event | None = None, budget_seconds: float = 8) -> RadarUsage:
        cancelled = cancel_event if cancel_event is not None else threading.Event()
        if cancelled.is_set():
            raise RuntimeError("Radar Bench refresh cancelled")
        if self._cache and time.time() - self.completion_epoch < 300:
            return self.cached_usage()
        deadline = time.monotonic() + budget_seconds
        try:
            if not self.binding or time.time() - self._cycle_started_epoch >= 3600:
                binding = self._validate_binding(self._read_json(RADAR_BENCH_BINDING_URL, cancelled, deadline))
                if cancelled.is_set():
                    raise RuntimeError("Radar Bench refresh cancelled")
                self.binding, self._entries = binding, {}
                self._cycle_started_epoch = int(time.time())
                if self._cache and self._cache["binding"] != binding:
                    self._cache = None
                    self.completion_epoch = 0
            binding = self.binding
            missing = iter((s["model"], s["effort"]) for s in binding["model_efforts"]
                           if (s["model"], s["effort"]) not in self._entries)

            def read_selection(key: tuple[str, str]) -> dict[str, Any]:
                url = RADAR_BENCH_SCORE_URL + "?" + urllib.parse.urlencode(
                    {"model": key[0], "effort": key[1], "view": "summary"},
                )
                summary = self._validate_summary(self._read_json(url, cancelled, deadline), binding, key)
                return {"summary": summary, "read_epoch": int(time.time())}

            with ThreadPoolExecutor(max_workers=3) as pool:
                running: dict[Any, tuple[str, str]] = {}

                def enqueue() -> None:
                    if cancelled.is_set() or time.monotonic() >= deadline:
                        return
                    key = next(missing, None)
                    if key is not None:
                        running[pool.submit(read_selection, key)] = key

                for _ in range(3):
                    enqueue()
                while running and not cancelled.is_set():
                    budget = deadline - time.monotonic()
                    if budget <= 0:
                        break
                    completed, _ = wait(running, timeout=budget, return_when=FIRST_COMPLETED)
                    if not completed:
                        break
                    for future in completed:
                        key = running.pop(future)
                        if cancelled.is_set() or time.monotonic() >= deadline:
                            break
                        try:
                            self._entries[key] = future.result()
                        except Exception as error:
                            print(f"Radar Bench {key[0]} {key[1]} read failed: {error}", flush=True)
                        enqueue()
                for future in running:
                    future.cancel()
            if cancelled.is_set():
                raise RuntimeError("Radar Bench refresh cancelled")
            if len(self._entries) == len(binding["model_efforts"]):
                entries = self._validate_entries(list(self._entries.values()), binding)
                completion = int(time.time())
                try:
                    self._save_cache(binding, entries, completion)
                except OSError as error:
                    print(f"Radar Bench cache save failed: {error}", flush=True)
                self._cache = {"binding": binding, "entries": entries}
                self.completion_epoch = completion
                self.binding, self._entries, self._cycle_started_epoch = None, {}, 0
                return self._usage(entries, False)
        except Exception:
            if cancelled.is_set() or not self._cache:
                raise
        if self._cache:
            return self.cached_usage()
        raise RuntimeError(f"Radar Bench full ranking incomplete ({len(self._entries)} selections read)")


class MetricsCollector:
    def __init__(self) -> None:
        self.codex: CodexAppServer | None = None
        self.radar_reader = RadarBenchReader()

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

    def _read_codex_radar(self, cancel_event: threading.Event | None = None) -> RadarUsage:
        return self.radar_reader.refresh(cancel_event)

    def _cached_radar(self) -> RadarUsage:
        return self.radar_reader.cached_usage()

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
        cached_radar = self._cached_radar()
        snapshot = UsageSnapshot(
            host=host, fetched_epoch=int(time.time()), radar=cached_radar,
            radar_fetched_epoch=cached_radar.fetched_epoch,
            pending_mask=15 if cached_radar.fetched_epoch > 0 else 31,
        )

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
                        fields.update(radar=result, radar_fetched_epoch=result.fetched_epoch)
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
