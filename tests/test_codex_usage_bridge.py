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
    def test_reads_gzip_compressed_radar_response(self) -> None:
        data = {
            "source_updated_at": "2026-10-22T02:00:00Z",
            "points": [{"model": "gpt-6-sol", "effort": "high", "iq": 98.6}],
        }
        body = gzip.compress(json.dumps(data).encode("utf-8"))
        response = FakeResponse(body, {"Content-Encoding": "gzip"})
        with mock.patch.object(codex_usage_bridge.urllib.request, "urlopen", return_value=response) as opened:
            radar = codex_usage_bridge.MetricsCollector._read_codex_radar()

        self.assertEqual(radar.models[0].score_tenths, 986)
        self.assertEqual(opened.call_args.args[0].get_header("Accept-encoding"), "gzip")

    def test_cancellation_stops_http_fallbacks(self) -> None:
        cancelled = threading.Event()

        def urlopen(request: object, timeout: float) -> FakeResponse:
            cancelled.set()
            raise OSError("live endpoint failed")

        with mock.patch.object(codex_usage_bridge.urllib.request, "urlopen", side_effect=urlopen) as opened:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                codex_usage_bridge.MetricsCollector._read_codex_radar(cancelled)

        self.assertEqual(opened.call_count, 1)

    def read_radar(
        self,
        page: str,
        data: dict[str, object] | None = None,
        live_data: dict[str, object] | None = None,
        now: str = "2026-07-22T06:00:00+00:00",
    ) -> codex_usage_bridge.RadarUsage:
        self.requested_urls: list[str] = []

        def urlopen(request: object, timeout: int) -> FakeResponse:
            url = getattr(request, "full_url", "")
            self.requested_urls.append(url)
            if url == codex_usage_bridge.CODEX_RADAR_LIVE_URL:
                if live_data is None:
                    raise OSError("live endpoint unavailable")
                return FakeResponse(json.dumps(live_data))
            if url == codex_usage_bridge.CODEX_RADAR_DATA_URL:
                if data is None:
                    raise OSError("current data endpoint unavailable")
                return FakeResponse(json.dumps(data))
            return FakeResponse(page)

        with (
            mock.patch.object(
                codex_usage_bridge.urllib.request,
                "urlopen",
                side_effect=urlopen,
            ),
            mock.patch.object(
                codex_usage_bridge.time, "time",
                return_value=codex_usage_bridge.datetime.fromisoformat(now).timestamp(),
            ),
        ):
            return codex_usage_bridge.MetricsCollector._read_codex_radar()

    def test_reads_current_intelligence_efficiency_data(self) -> None:
        data = {
            "source_updated_at": "2026-07-22T05:29:00+00:00",
            "points": [
                {"model": "gpt-5.6-sol", "effort": "ultra", "iq": 101.8},
                {"model": "gpt-5.6-terra", "effort": "max", "iq": 103.1},
            ],
        }

        radar = self.read_radar("", data)

        self.assertEqual(radar.updated, "07/22 13:29")
        self.assertEqual(
            radar.models,
            (
                codex_usage_bridge.RadarModel(name="Terra max", score_tenths=1031),
                codex_usage_bridge.RadarModel(name="Sol ultra", score_tenths=1018),
            ),
        )

    def test_prefers_live_data_and_uses_latest_source_timestamp(self) -> None:
        live_data = {
            "schema": 3,
            "benchmark_id": "deep-swe",
            "source_updated_at": {
                "codex": "2026-10-21T02:00:00Z",
                "other": "2026-10-20T06:00:00Z",
            },
            "points": [{"model": "gpt-6-sol", "effort": "high", "iq": 98.6}],
        }

        radar = self.read_radar(
            "", live_data=live_data, now="2026-10-21T03:00:00+00:00",
        )

        self.assertEqual(radar.updated, "10/21 10:00")
        self.assertEqual(radar.models[0], codex_usage_bridge.RadarModel("6 Sol high", 986))
        self.assertEqual(self.requested_urls, [codex_usage_bridge.CODEX_RADAR_LIVE_URL])

    def test_marks_old_published_fallback_and_preserves_marker_on_wire(self) -> None:
        data = {
            "schema": 2,
            "source_updated_at": "2026-10-01T23:00:27+08:00",
            "points": [{"model": "gpt-5.6-sol", "effort": "high", "iq": 96.4}],
        }

        radar = self.read_radar("", data, now="2026-10-21T03:00:00+00:00")

        self.assertEqual(radar.updated, "10/01 23:00 OLD")
        self.assertEqual(
            self.requested_urls,
            [codex_usage_bridge.CODEX_RADAR_LIVE_URL, codex_usage_bridge.CODEX_RADAR_DATA_URL],
        )
        payload = codex_usage_bridge.UsageSnapshot(
            host="Test", fetched_epoch=1, radar=radar,
        ).wire_payload()
        self.assertIn(",10/01 23:00 OLD,", payload)

    def test_generated_time_is_used_when_source_time_is_missing(self) -> None:
        data = {
            "generated_at": "2026-10-21T02:00:00Z",
            "points": [{"model": "gpt-6-sol", "effort": "high", "iq": 98.6}],
        }

        radar = self.read_radar("", live_data=data, now="2026-10-21T03:00:00+00:00")

        self.assertEqual(radar.updated, "10/21 10:00")

    def test_rejects_different_benchmark_before_published_fallback(self) -> None:
        live_data = {
            "benchmark_id": "other-benchmark",
            "source_updated_at": "2026-10-21T02:00:00Z",
            "points": [{"model": "gpt-6-sol", "effort": "high", "iq": 1}],
        }
        data = {
            "source_updated_at": "2026-10-21T01:00:00Z",
            "points": [{"model": "gpt-6-sol", "effort": "high", "iq": 98.6}],
        }

        radar = self.read_radar(
            "", data=data, live_data=live_data, now="2026-10-21T03:00:00+00:00",
        )

        self.assertEqual(radar.models[0].score_tenths, 986)

    def test_keeps_support_for_legacy_degradation_radar_markup(self) -> None:
        page = """
        <section class="model-iq model-iq-green">
          <h2>降智雷达 <span>7月21日12:11更新</span></h2>
          <button class="model-iq-score-chip">
            <span>GPT-5 high</span>
            <div class="model-iq-score-metrics"><strong>91.2</strong></div>
          </button>
        </section>
        """

        radar = self.read_radar(page)

        self.assertEqual(radar.updated, "07/21 12:11")
        self.assertEqual(
            radar.models,
            (codex_usage_bridge.RadarModel(name="5 high", score_tenths=912),),
        )


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

    def test_usage5_payload_appends_source_status_after_legacy_five_hour_fields(self) -> None:
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
            radar=codex_usage_bridge.RadarUsage(
                updated="08/28 09:00",
                models=(codex_usage_bridge.RadarModel("Sol high", 912),),
            ),
        )

        fields = snapshot.wire_payload().split(",")

        self.assertEqual(fields[0], "USAGE5")
        self.assertEqual(fields[-8:-6], ["75", "1787897794"])
        self.assertEqual(fields[-6:], ["16", "1787890000", "1787890001", "1787890002", "1787890003", "0"])


class MetricsCollectionTests(unittest.TestCase):
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
            return codex_usage_bridge.RadarUsage("10/22 12:00", (codex_usage_bridge.RadarModel("Sol", 900),))

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
