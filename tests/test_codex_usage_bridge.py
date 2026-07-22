from __future__ import annotations

import json
import unittest
from unittest import mock

from tools import codex_usage_bridge


class FakeResponse:
    def __init__(self, body: str) -> None:
        self.body = body.encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class CodexRadarTests(unittest.TestCase):
    def read_radar(
        self,
        page: str,
        data: dict[str, object] | None = None,
    ) -> codex_usage_bridge.RadarUsage:
        def urlopen(request: object, timeout: int) -> FakeResponse:
            if getattr(request, "full_url", "") == codex_usage_bridge.CODEX_RADAR_DATA_URL:
                if data is None:
                    raise OSError("current data endpoint unavailable")
                return FakeResponse(json.dumps(data))
            return FakeResponse(page)

        with mock.patch.object(
            codex_usage_bridge.urllib.request,
            "urlopen",
            side_effect=urlopen,
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


if __name__ == "__main__":
    unittest.main()
