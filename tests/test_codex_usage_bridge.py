from __future__ import annotations

import json
import gzip
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from tools import codex_usage_bridge


class FakeResponse:
    def __init__(self, body: str | bytes, headers: dict[str, str] | None = None) -> None:
        self.body = io.BytesIO(body.encode("utf-8") if isinstance(body, str) else body)
        self.headers = headers or {}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.body.read(size)


class CodexRadarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cache_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache_dir.cleanup)
        self.cache_path = Path(self.cache_dir.name) / "radar-cache.json"
        self.reader = codex_usage_bridge.RadarBenchReader(self.cache_path)
        options = [
            {"model": model, "effort": effort}
            for model in (
                "gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol",
                "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-luna",
            )
            for effort in (
                ("low", "medium", "high", "xhigh", "max")
                if "luna" in model else ("low", "medium", "high", "xhigh", "max", "ultra")
            )
        ]
        self.binding = {
            "enabled": True,
            "interface_confirmed": True,
            "production_read_verified": True,
            "transport_origin": "https://api.codexradar.com",
            "endpoint_path": "/api/v1/radar-bench-score",
            "benchmark": "radar-bench-final64-20261004",
            "score_version": "radar-bench-v2-150",
            "score_max": 150,
            "catalog_version": "test-catalog-v1",
            "task_set_sha256": "a" * 64,
            "total_tasks": 64,
            "total_cells": 320,
            "source_counts": {"deepswe": 15, "pompeii": 16, "tb4": 14, "science": 19},
            "model_efforts": options,
        }
        self.summaries = {
            (option["model"], option["effort"]): self.summary(option, float(index))
            for index, option in enumerate(options)
        }

    def summary(self, option: dict[str, str], score: float | None) -> dict[str, object]:
        return {
            "schema": "radar-bench-public-summary-v6-150-details",
            "benchmark": self.binding["benchmark"],
            "score_version": "radar-bench-v2-150",
            "score_max": 150,
            "catalog_version": self.binding["catalog_version"],
            "task_set_sha256": self.binding["task_set_sha256"],
            **option,
            "score": score,
            "coverage": 64 if score is not None else 0,
            "required_tasks": 64,
            "score_status": "complete" if score is not None else "missing_current_result",
            "graded_runs_count": 64 if score is not None else 0,
            "source_counts": self.binding["source_counts"],
            "detail_metrics": {
                "latest_graded_at": "2026-10-21T02:39:21Z" if score is not None else None,
            },
        }

    def respond(self, url: str, cancel_event: threading.Event, deadline: float) -> dict[str, object]:
        if "radar-bench-binding.json" in url:
            return json.loads(json.dumps(self.binding))
        query = codex_usage_bridge.urllib.parse.parse_qs(
            codex_usage_bridge.urllib.parse.urlparse(url).query
        )
        key = (query["model"][0], query["effort"][0])
        return json.loads(json.dumps(self.summaries[key]))

    def refresh(self, now: int = 200) -> codex_usage_bridge.RadarUsage:
        with (
            mock.patch.object(self.reader, "_read_json", side_effect=self.respond),
            mock.patch.object(codex_usage_bridge.time, "time", return_value=now),
        ):
            return self.reader.refresh()

    def test_full_ranking_sorts_raw_scores_before_rounding_and_takes_six(self) -> None:
        options = self.binding["model_efforts"]
        # These two scores round to the same tenth, but the later option wins.
        self.summaries[(options[0]["model"], options[0]["effort"])] = self.summary(options[0], 140.01)
        self.summaries[(options[1]["model"], options[1]["effort"])] = self.summary(options[1], 140.04)
        radar = self.refresh()

        self.assertEqual(len(json.loads(self.cache_path.read_text())["entries"]), 34)
        self.assertEqual(len(radar.models), 6)
        self.assertEqual(radar.models[0], codex_usage_bridge.RadarModel("6.1 Sol medium", 1400))
        self.assertEqual(radar.models[1], codex_usage_bridge.RadarModel("6.1 Sol low", 1400))
        self.assertEqual([model.score_tenths for model in radar.models[2:]], [330, 320, 310, 300])
        self.assertTrue(self.cache_path.exists())
        self.assertFalse(radar.cached)

    def test_public_150_point_score_is_not_scaled_again(self) -> None:
        option = self.binding["model_efforts"][0]
        self.summaries[(option["model"], option["effort"])] = self.summary(option, 102.63728753664665)

        self.assertEqual(self.refresh().models[0].score_tenths, 1026)

    def test_null_score_is_valid_ungraded_data_and_still_completes_full_cache(self) -> None:
        option = self.binding["model_efforts"][0]
        self.summaries[(option["model"], option["effort"])] = self.summary(option, None)
        radar = self.refresh()

        self.assertEqual(len(json.loads(self.cache_path.read_text())["entries"]), 34)
        self.assertEqual(len(radar.models), 6)
        self.assertNotIn("6.1 Sol low", [model.name for model in radar.models])
        self.assertTrue(self.cache_path.exists())

    def test_identity_mismatch_or_missing_summary_does_not_replace_good_cache(self) -> None:
        self.refresh(200)
        saved = self.cache_path.read_bytes()
        option = self.binding["model_efforts"][0]
        key = (option["model"], option["effort"])
        changes = (
            {"schema": "old-schema"},
            {"score_version": "old-score-version"},
            {"score_max": 100},
            {"benchmark": "different-benchmark"},
            {"catalog_version": "different-catalog"},
            {"task_set_sha256": "b" * 64},
            {"model": "gpt-unbound"},
            {"effort": "unbound"},
            {"score": float("nan")},
            {"score": 151},
            {"required_tasks": 63},
            {"score": None, "coverage": 1},
        )
        for change in changes:
            with self.subTest(change=change):
                self.reader = codex_usage_bridge.RadarBenchReader(self.cache_path)
                self.summaries[key] = self.summary(option, 50)
                self.summaries[key].update(change)
                result = self.refresh(600)
                self.assertEqual(self.cache_path.read_bytes(), saved)
                self.assertTrue(result.cached)
                self.assertEqual(result.fetched_epoch, 200)

        self.reader = codex_usage_bridge.RadarBenchReader(self.cache_path)
        self.summaries[key] = self.summary(option, 50)

        def missing(url: str, cancel_event: threading.Event, deadline: float) -> dict[str, object]:
            if "radar-bench-binding.json" not in url:
                query = codex_usage_bridge.urllib.parse.parse_qs(codex_usage_bridge.urllib.parse.urlparse(url).query)
                if (query["model"][0], query["effort"][0]) == key:
                    raise OSError("selection unavailable")
            return self.respond(url, cancel_event, deadline)

        with (
            mock.patch.object(self.reader, "_read_json", side_effect=missing),
            mock.patch.object(codex_usage_bridge.time, "time", return_value=700),
        ):
            result = self.reader.refresh()
        self.assertEqual(self.cache_path.read_bytes(), saved)
        self.assertTrue(result.cached)
        self.assertEqual(result.fetched_epoch, 200)

    def test_cache_reload_and_failed_refresh_keep_original_time(self) -> None:
        live = self.refresh(200)
        self.reader = codex_usage_bridge.RadarBenchReader(self.cache_path)
        with mock.patch.object(codex_usage_bridge.time, "time", return_value=900):
            cached = self.reader.cached_usage()
        self.assertTrue(cached.cached)
        self.assertEqual(cached.fetched_epoch, live.fetched_epoch)
        self.assertEqual(cached.updated, live.updated)
        self.assertEqual(cached.models, live.models)

        with mock.patch.object(self.reader, "_read_json", side_effect=OSError("offline")):
            cached = self.reader.refresh()
        self.assertTrue(cached.cached)
        self.assertEqual(cached.fetched_epoch, 200)

    def test_cancelled_reader_starts_no_requests_and_writes_no_cache(self) -> None:
        cancelled = threading.Event()
        cancelled.set()
        with mock.patch.object(self.reader, "_read_json") as read:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                self.reader.refresh(cancelled)
        read.assert_not_called()
        self.assertFalse(self.cache_path.exists())

    def test_partial_sweep_resumes_next_wake_with_bounded_concurrency_and_drops_late_results(self) -> None:
        cancelled = threading.Event()
        release = threading.Event()
        blocked = threading.Event()
        lock = threading.Lock()
        active = 0
        maximum = 0
        requests: list[tuple[str, str]] = []
        errors: list[Exception] = []

        def respond(url: str, cancel_event: threading.Event, deadline: float) -> dict[str, object]:
            nonlocal active, maximum
            if "radar-bench-binding.json" in url:
                return self.respond(url, cancel_event, deadline)
            query = codex_usage_bridge.urllib.parse.parse_qs(codex_usage_bridge.urllib.parse.urlparse(url).query)
            key = (query["model"][0], query["effort"][0])
            with lock:
                requests.append(key)
                active += 1
                maximum = max(maximum, active)
                ordinal = len(requests)
            try:
                if ordinal > 1:
                    blocked.set()
                    release.wait(2)
                return self.respond(url, cancel_event, deadline)
            finally:
                with lock:
                    active -= 1

        def refresh_until_cancelled() -> None:
            try:
                self.reader.refresh(cancelled)
            except RuntimeError as error:
                errors.append(error)

        with mock.patch.object(self.reader, "_read_json", side_effect=respond):
            worker = threading.Thread(target=refresh_until_cancelled)
            worker.start()
            try:
                self.assertTrue(blocked.wait(2))
                # Wait until the first accepted entry is visible, not merely
                # until its HTTP response has been produced.
                until = codex_usage_bridge.time.monotonic() + 2
                while not self.reader._entries and codex_usage_bridge.time.monotonic() < until:
                    cancelled.wait(0.005)
                self.assertTrue(self.reader._entries)
                accepted = set(self.reader._entries)
                before_cancel = len(requests)
                cancelled.set()
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIn("cancelled", str(errors[0]))
        self.assertLessEqual(maximum, 3)
        self.assertEqual(set(self.reader._entries), accepted)
        self.assertEqual(len(requests), before_cancel)
        self.assertFalse(self.cache_path.exists())

        resumed: list[tuple[str, str]] = []

        def resume(url: str, cancel_event: threading.Event, deadline: float) -> dict[str, object]:
            if "radar-bench-binding.json" not in url:
                query = codex_usage_bridge.urllib.parse.parse_qs(codex_usage_bridge.urllib.parse.urlparse(url).query)
                resumed.append((query["model"][0], query["effort"][0]))
            return self.respond(url, cancel_event, deadline)

        with mock.patch.object(self.reader, "_read_json", side_effect=resume):
            complete = self.reader.refresh(threading.Event())
        self.assertEqual(len(json.loads(self.cache_path.read_text())["entries"]), 34)
        self.assertFalse(accepted.intersection(resumed))
        self.assertEqual(len(complete.models), 6)
        self.assertTrue(self.cache_path.exists())

    def test_reads_gzip_compressed_bench_json(self) -> None:
        body = gzip.compress(json.dumps(self.binding).encode("utf-8"))
        with mock.patch.object(
            codex_usage_bridge.urllib.request, "urlopen",
            return_value=FakeResponse(body, {"Content-Encoding": "gzip"}),
        ) as opened:
            result = self.reader._read_json(
                "https://example.test/binding", threading.Event(),
                codex_usage_bridge.time.monotonic() + 5,
            )
        self.assertEqual(result, self.binding)
        self.assertEqual(opened.call_args.args[0].get_header("Accept-encoding"), "gzip")


class CodexRateLimitTests(unittest.TestCase):
    def test_does_not_borrow_model_specific_five_hour_window(self) -> None:
        result = {
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 2,
                    "windowDurationMins": 10080,
                    "resetsAt": 1788483429,
                },
                "secondary": None,
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "usedPercent": 2,
                        "windowDurationMins": 10080,
                        "resetsAt": 1788483429,
                    },
                    "secondary": None,
                },
                "codex_bengalfox": {
                    "primary": {
                        "usedPercent": 25,
                        "windowDurationMins": 300,
                        "resetsAt": 1787897794,
                    },
                    "secondary": {
                        "usedPercent": 10,
                        "windowDurationMins": 10080,
                        "resetsAt": 1788484594,
                    },
                },
            },
        }

        limits = codex_usage_bridge.CodexAppServer._parse_rate_limits(result)

        self.assertEqual(limits, (98, 2, 1788483429, -1, 0))

    def test_unused_five_hour_window_preserves_reported_reset(self) -> None:
        result = {
            "rateLimits": {
                "primary": {
                    "usedPercent": 4,
                    "windowDurationMins": 10080,
                    "resetsAt": 1788483429,
                },
                "secondary": {
                    "usedPercent": 0,
                    "windowDurationMins": 300,
                    "resetsAt": 1787899925,
                },
            },
        }

        limits = codex_usage_bridge.CodexAppServer._parse_rate_limits(result)

        self.assertEqual(limits, (96, 4, 1788483429, 100, 1787899925))

    def test_prefers_named_codex_bucket_over_legacy_snapshot(self) -> None:
        result = {
            "rateLimits": {
                "primary": {"usedPercent": 80, "windowDurationMins": 10080, "resetsAt": 111},
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"usedPercent": 10, "windowDurationMins": 10080, "resetsAt": 222},
                    "secondary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 333},
                },
            },
        }

        self.assertEqual(
            codex_usage_bridge.CodexAppServer._parse_rate_limits(result),
            (90, 10, 222, 75, 333),
        )

    def test_missing_or_null_percent_is_unknown_not_full(self) -> None:
        for percent in ({}, {"usedPercent": None}):
            with self.subTest(percent=percent):
                result = {
                    "rateLimits": {
                        "primary": {**percent, "windowDurationMins": 10080, "resetsAt": 222},
                        "secondary": {**percent, "windowDurationMins": 300, "resetsAt": 333},
                    },
                }
                limits = codex_usage_bridge.CodexAppServer._parse_rate_limits(result)
                self.assertEqual(limits[0:2], (-1, -1))
                self.assertEqual(limits[3], -1)

    def test_five_hour_only_window_is_not_weekly_quota(self) -> None:
        result = {
            "rateLimits": {
                "primary": {"usedPercent": 30, "windowDurationMins": 300, "resetsAt": 333},
                "secondary": {"usedPercent": 90, "windowDurationMins": 1440, "resetsAt": 444},
            },
        }

        self.assertEqual(
            codex_usage_bridge.CodexAppServer._parse_rate_limits(result),
            (-1, -1, 0, 70, 333),
        )

    def test_usage6_payload_preserves_source_epochs_and_appends_radar_cache_flag(self) -> None:
        snapshot = codex_usage_bridge.UsageSnapshot(
            host="Test Mac",
            fetched_epoch=1787890000,
            quota_5h_remaining=75,
            quota_5h_reset_epoch=1787897794,
            quota_remaining=98,
            quota_used=2,
            quota_reset_epoch=1788483429,
            pending_mask=16,
            codex_fetched_epoch=1787890000,
            vibe24_fetched_epoch=1787890001,
            vibe7_fetched_epoch=1787890002,
            vibe30_fetched_epoch=1787890003,
            radar_fetched_epoch=1787889000,
            radar=codex_usage_bridge.RadarUsage(
                updated="08/28 09:00",
                models=(codex_usage_bridge.RadarModel("Sol high", 912),),
                fetched_epoch=1787889000,
                cached=True,
            ),
        )

        fields = snapshot.wire_payload().split(",")

        self.assertEqual(fields[0], "USAGE6")
        self.assertEqual(fields[-9:-7], ["75", "1787897794"])
        self.assertEqual(fields[-7:], ["16", "1787890000", "1787890001", "1787890002", "1787890003", "1787889000", "1"])


class MetricsCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cache_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache_dir.cleanup)
        self.reader = codex_usage_bridge.RadarBenchReader(
            Path(self.cache_dir.name) / "radar-cache.json"
        )
        reader_factory = mock.patch.object(
            codex_usage_bridge, "RadarBenchReader", return_value=self.reader
        )
        reader_factory.start()
        self.addCleanup(reader_factory.stop)

    def test_finds_bundled_cli_when_path_has_no_codex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "ChatGPT.app/Contents/Resources/nested/codex"
            candidate.parent.mkdir(parents=True)
            candidate.write_text("#!/bin/sh\n", encoding="utf-8")
            candidate.chmod(0o755)
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(codex_usage_bridge.shutil, "which", return_value=None),
                mock.patch.object(codex_usage_bridge, "CODEX_BIN_CANDIDATES", (candidate,)),
            ):
                self.assertEqual(codex_usage_bridge.MetricsCollector._find_codex(), str(candidate))

    def test_failed_sources_clear_cached_values_and_keep_successful_sources(self) -> None:
        collector = codex_usage_bridge.MetricsCollector()
        collector.codex = mock.Mock()
        collector.codex.read_usage_limits.side_effect = RuntimeError("quota unavailable")
        previous = codex_usage_bridge.UsageSnapshot(
            host="old-host",
            fetched_epoch=100,
            quota_remaining=61,
            quota_used=39,
            quota_reset_epoch=1000,
            quota_5h_remaining=75,
            quota_5h_reset_epoch=500,
            usage_24h=codex_usage_bridge.PeriodUsage(100, 1000),
            usage_7d=codex_usage_bridge.PeriodUsage(200, 2000),
            usage_30d=codex_usage_bridge.PeriodUsage(300, 3000),
            radar=codex_usage_bridge.RadarUsage(
                updated="07/21 12:11",
                models=(codex_usage_bridge.RadarModel("Old model", 900),),
            ),
        )
        fresh_period = codex_usage_bridge.PeriodUsage(400, 4000)

        def read_period(days: int) -> codex_usage_bridge.PeriodUsage:
            if days == 7:
                return fresh_period
            raise RuntimeError(f"{days}d unavailable")

        with (
            mock.patch.object(collector, "_vibe_config", return_value={"hostname": "test-host"}),
            mock.patch.object(
                collector, "_read_vibe_period",
                side_effect=read_period,
            ),
            mock.patch.object(collector, "_read_codex_radar", side_effect=RuntimeError("radar unavailable")),
            mock.patch.object(codex_usage_bridge.time, "time", return_value=200),
            mock.patch("builtins.print"),
        ):
            snapshot = collector.collect(previous)

        self.assertEqual(snapshot.host, "test-host")
        self.assertEqual(snapshot.fetched_epoch, 200)
        self.assertEqual(
            (snapshot.quota_remaining, snapshot.quota_used, snapshot.quota_reset_epoch,
             snapshot.quota_5h_remaining, snapshot.quota_5h_reset_epoch),
            (-1, -1, 0, -1, 0),
        )
        self.assertEqual(snapshot.usage_24h, codex_usage_bridge.PeriodUsage())
        self.assertEqual(snapshot.usage_7d, fresh_period)
        self.assertEqual(snapshot.usage_30d, codex_usage_bridge.PeriodUsage())
        self.assertEqual(snapshot.radar, codex_usage_bridge.RadarUsage())
        self.assertEqual(snapshot.pending_mask, 0)
        self.assertEqual(
            (snapshot.codex_fetched_epoch, snapshot.vibe24_fetched_epoch,
             snapshot.vibe7_fetched_epoch, snapshot.vibe30_fetched_epoch, snapshot.radar_fetched_epoch),
            (0, 0, 200, 0, 0),
        )

    def test_fast_sources_are_published_while_radar_is_still_blocked(self) -> None:
        collector = codex_usage_bridge.MetricsCollector()
        collector.codex = mock.Mock()
        collector.codex.read_usage_limits.return_value = (90, 10, 999, -1, 0)
        release_radar = threading.Event()
        core_ready = threading.Event()
        progress: list[codex_usage_bridge.UsageSnapshot] = []
        completed: list[codex_usage_bridge.UsageSnapshot] = []

        def read_radar(cancel_event: threading.Event) -> codex_usage_bridge.RadarUsage:
            if not release_radar.wait(2):
                raise RuntimeError("test did not release radar")
            return codex_usage_bridge.RadarUsage(
                "10/22 12:00", (codex_usage_bridge.RadarModel("Sol", 900),),
                fetched_epoch=200,
            )

        def publish(snapshot: codex_usage_bridge.UsageSnapshot) -> None:
            progress.append(snapshot)
            if snapshot.pending_mask == 16:
                core_ready.set()

        with (
            mock.patch.object(collector, "_vibe_config", return_value={"hostname": "test-host"}),
            mock.patch.object(collector, "_read_vibe_period", return_value=codex_usage_bridge.PeriodUsage(100, 1000)),
            mock.patch.object(collector, "_read_codex_radar", side_effect=read_radar),
            mock.patch.object(codex_usage_bridge.time, "time", return_value=200),
            mock.patch("builtins.print"),
        ):
            worker = threading.Thread(target=lambda: completed.append(collector.collect(on_progress=publish)))
            worker.start()
            try:
                self.assertTrue(core_ready.wait(2))
                self.assertTrue(worker.is_alive())
                self.assertEqual(progress[0].pending_mask, 31)
                self.assertEqual(progress[0].quota_remaining, -1)
                self.assertEqual(progress[-1].quota_remaining, 90)
                self.assertEqual(progress[-1].codex_fetched_epoch, 200)
                self.assertEqual(progress[-1].vibe30_fetched_epoch, 200)
                self.assertEqual(progress[-1].radar_fetched_epoch, 0)
            finally:
                release_radar.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(completed[0].pending_mask, 0)
        self.assertEqual(completed[0].radar_fetched_epoch, 200)

    def test_full_radar_cache_is_published_immediately_without_radar_pending_bit(self) -> None:
        collector = codex_usage_bridge.MetricsCollector()
        collector.codex = mock.Mock()
        collector.codex.read_usage_limits.return_value = (90, 10, 999, -1, 0)
        cached = codex_usage_bridge.RadarUsage(
            "10/21 10:00", (codex_usage_bridge.RadarModel("6.1 Sol high", 1000),),
            fetched_epoch=100, cached=True,
        )
        core_ready = threading.Event()
        release_radar = threading.Event()
        progress: list[codex_usage_bridge.UsageSnapshot] = []

        def read_radar(cancel_event: threading.Event) -> codex_usage_bridge.RadarUsage:
            release_radar.wait(2)
            return cached

        def publish(snapshot: codex_usage_bridge.UsageSnapshot) -> None:
            progress.append(snapshot)
            if snapshot.pending_mask == 0:
                core_ready.set()

        with (
            mock.patch.object(collector, "_vibe_config", return_value={"hostname": "test-host"}),
            mock.patch.object(collector, "_read_vibe_period", return_value=codex_usage_bridge.PeriodUsage(1, 10)),
            mock.patch.object(self.reader, "cached_usage", return_value=cached),
            mock.patch.object(collector, "_read_codex_radar", side_effect=read_radar),
            mock.patch.object(codex_usage_bridge.time, "time", return_value=200),
            mock.patch("builtins.print"),
        ):
            worker = threading.Thread(target=lambda: collector.collect(on_progress=publish))
            worker.start()
            try:
                self.assertTrue(core_ready.wait(2))
                self.assertEqual(progress[0].pending_mask, 15)
                self.assertEqual(progress[0].radar, cached)
                self.assertEqual(progress[0].radar_fetched_epoch, 100)
                self.assertTrue(all(not snapshot.pending_mask & 16 for snapshot in progress))
                self.assertTrue(all(snapshot.radar_fetched_epoch == 100 for snapshot in progress))
            finally:
                release_radar.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_cancelled_refresh_drops_late_radar_and_returns_no_cached_values(self) -> None:
        collector = codex_usage_bridge.MetricsCollector()
        collector.codex = mock.Mock()
        collector.codex.read_usage_limits.return_value = (90, 10, 999, -1, 0)
        cancelled = threading.Event()
        progress: list[codex_usage_bridge.UsageSnapshot] = []

        def read_radar(cancel_event: threading.Event) -> codex_usage_bridge.RadarUsage:
            cancel_event.wait(2)
            return codex_usage_bridge.RadarUsage("old", (codex_usage_bridge.RadarModel("Old", 900),))

        def publish(snapshot: codex_usage_bridge.UsageSnapshot) -> None:
            progress.append(snapshot)
            if snapshot.pending_mask == 16:
                cancelled.set()

        with (
            mock.patch.object(collector, "_vibe_config", return_value={"hostname": "test-host"}),
            mock.patch.object(collector, "_read_vibe_period", return_value=codex_usage_bridge.PeriodUsage(100, 1000)),
            mock.patch.object(collector, "_read_codex_radar", side_effect=read_radar),
            mock.patch("builtins.print"),
        ):
            result = collector.collect(on_progress=publish, cancel_event=cancelled)

        self.assertTrue(cancelled.is_set())
        self.assertTrue(all(snapshot.radar_fetched_epoch == 0 for snapshot in progress))
        self.assertEqual(result.quota_remaining, -1)
        self.assertEqual(result.codex_fetched_epoch, 0)
        self.assertEqual(result.usage_24h, codex_usage_bridge.PeriodUsage())
        self.assertEqual(result.radar, codex_usage_bridge.RadarUsage())


class UsbRefreshTests(unittest.TestCase):
    def test_refresh_request_does_not_send_cached_payload_or_early_success(self) -> None:
        manager = codex_usage_bridge.UsbHotplugManager()
        manager.device = mock.Mock()
        manager.device.read_lines.return_value = ["STICKS3_REFRESH"]
        manager.identified = True
        manager.screen_awake = True
        cached_payload = "USAGE4,cached-payload"

        with mock.patch("builtins.print"):
            self.assertTrue(manager.tick(cached_payload))

        manager.device.write_line.assert_not_called()


class WifiRefreshTests(unittest.TestCase):
    def test_retry_at_collection_completion_delivers_result_before_recollecting(self) -> None:
        snapshot = codex_usage_bridge.UsageSnapshot(host="test-host", fetched_epoch=200)
        collector = mock.Mock()
        collector.collect.return_value = snapshot
        wifi = mock.Mock()
        wifi.screen_awake = True
        wifi.poll.return_value = True

        def start_thread(*, target: object, daemon: bool) -> mock.Mock:
            thread = mock.Mock()
            thread.start.side_effect = target
            # The first iteration has an in-flight collection. Its queued
            # result is consumed at the start of the second iteration.
            thread.is_alive.return_value = True
            return thread

        with (
            mock.patch("sys.argv", ["codex_usage_bridge.py", "--no-usb"]),
            mock.patch.object(codex_usage_bridge, "MetricsCollector", return_value=collector),
            mock.patch.object(codex_usage_bridge, "WifiBroadcaster", return_value=wifi),
            mock.patch.object(codex_usage_bridge.threading, "Thread", side_effect=start_thread),
            mock.patch.object(codex_usage_bridge.time, "monotonic", return_value=100),
            mock.patch.object(codex_usage_bridge.time, "sleep", side_effect=[None, KeyboardInterrupt]),
        ):
            self.assertEqual(codex_usage_bridge.main(), 0)

        collector.collect.assert_called_once()
        self.assertEqual(collector.collect.call_args.args, (None,))
        self.assertTrue(callable(collector.collect.call_args.kwargs["on_progress"]))
        wifi.send.assert_called_once_with(snapshot.wire_payload())

    def test_unknown_device_state_does_not_start_background_collection(self) -> None:
        collector = mock.Mock()
        wifi = mock.Mock()
        wifi.screen_awake = None
        wifi.poll.return_value = False
        with (
            mock.patch("sys.argv", ["codex_usage_bridge.py", "--no-usb"]),
            mock.patch.object(codex_usage_bridge, "MetricsCollector", return_value=collector),
            mock.patch.object(codex_usage_bridge, "WifiBroadcaster", return_value=wifi),
            mock.patch.object(codex_usage_bridge.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            self.assertEqual(codex_usage_bridge.main(), 0)

        collector.collect.assert_not_called()
        wifi.send.assert_not_called()

    def test_sleep_packet_discards_queued_results_before_transmission(self) -> None:
        snapshot = codex_usage_bridge.UsageSnapshot(host="test-host", fetched_epoch=200)
        collector = mock.Mock()

        def collect(previous: object, on_progress: object, cancel_event: threading.Event) -> codex_usage_bridge.UsageSnapshot:
            on_progress(snapshot)
            return snapshot

        collector.collect.side_effect = collect
        wifi = mock.Mock()
        wifi.screen_awake = True
        polls = 0

        def poll(host: str) -> bool:
            nonlocal polls
            polls += 1
            if polls >= 2:
                wifi.screen_awake = False
            return polls == 1

        wifi.poll.side_effect = poll

        def start_thread(*, target: object, daemon: bool) -> mock.Mock:
            thread = mock.Mock()
            thread.start.side_effect = target
            thread.is_alive.return_value = True
            return thread

        with (
            mock.patch("sys.argv", ["codex_usage_bridge.py", "--no-usb"]),
            mock.patch.object(codex_usage_bridge, "MetricsCollector", return_value=collector),
            mock.patch.object(codex_usage_bridge, "WifiBroadcaster", return_value=wifi),
            mock.patch.object(codex_usage_bridge.threading, "Thread", side_effect=start_thread),
            mock.patch.object(codex_usage_bridge.time, "monotonic", return_value=100),
            mock.patch.object(codex_usage_bridge.time, "sleep", side_effect=[None, KeyboardInterrupt]),
        ):
            self.assertEqual(codex_usage_bridge.main(), 0)

        collector.collect.assert_called_once()
        self.assertTrue(collector.collect.call_args.kwargs["cancel_event"].is_set())
        wifi.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
