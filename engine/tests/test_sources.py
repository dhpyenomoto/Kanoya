"""データ収集パイプラインの回帰テスト.

    cd engine && python3 -m unittest discover -s tests -v

ネットワークには一切出ない（HTTP層はスタブ、価格はフィクスチャ）。
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import collect, discovery, schedule, survey  # noqa: E402
from kanoya_rm.cli import build_context  # noqa: E402
from kanoya_rm.config import Competitor, Settings  # noqa: E402
from kanoya_rm.sources import http as http_mod  # noqa: E402
from kanoya_rm.sources.base import PlaceRecord, haversine_km, parse_money  # noqa: E402
from kanoya_rm.sources.fixture import FixturePlacesSource, FixtureRatesSource  # noqa: E402
from kanoya_rm.sources.http import HttpClient, RateLimiter, SourceError  # noqa: E402
from kanoya_rm.sources.places import _to_record as parse_place  # noqa: E402
from kanoya_rm.sources.serpapi_hotels import _to_record as parse_rate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RUN_DATE = date(2026, 8, 15)


def _comp(cid: str, name: str, **kw) -> Competitor:
    base = dict(tier="PRIMARY", weight=1.0, rooms=10, distance_km=1.0,
                pricing_basis="per_room", meal_included="none",
                dinner_uplift=0.0, breakfast_uplift=0.0)
    base.update(kw)
    return Competitor(id=cid, name=name, **base)


class ApiParserTest(unittest.TestCase):
    """実APIのレスポンス形をそのまま食わせる（形が変わったら落ちる）."""

    def test_places_api_new_shape(self) -> None:
        payload = {
            "id": "ChIJxxxx",
            "displayName": {"text": "奈良ホテル", "languageCode": "ja"},
            "formattedAddress": "日本、〒630-8301 奈良県奈良市高畑町1096",
            "location": {"latitude": 34.6790, "longitude": 135.8318},
            "types": ["hotel", "lodging"],
            "rating": 4.4,
            "userRatingCount": 2400,
            "priceLevel": "PRICE_LEVEL_VERY_EXPENSIVE",
        }
        rec = parse_place(payload, "google_places")
        self.assertEqual(rec.name, "奈良ホテル")
        self.assertEqual(rec.review_count, 2400)
        self.assertEqual(rec.price_level, "PRICE_LEVEL_VERY_EXPENSIVE")
        self.assertAlmostEqual(rec.latitude, 34.6790)

    def test_places_missing_optional_fields(self) -> None:
        """priceLevel / rating は返らないことがある（落ちてはいけない）."""
        rec = parse_place({"id": "x", "displayName": {"text": "宿"}}, "google_places")
        self.assertEqual(rec.price_level, "")
        self.assertIsNone(rec.rating)
        self.assertEqual(rec.review_count, 0)

    def test_serpapi_google_hotels_shape(self) -> None:
        payload = {
            "name": "ANDO HOTEL 奈良若草山",
            "property_token": "ChkI...",
            "gps_coordinates": {"latitude": 34.6883, "longitude": 135.8480},
            "rate_per_night": {
                "lowest": "¥63,000", "extracted_lowest": 63000,
                "before_taxes_fees": "¥57,273", "extracted_before_taxes_fees": 57273,
            },
            "total_rate": {"lowest": "¥63,000", "extracted_lowest": 63000},
            "overall_rating": 4.4, "reviews": 610, "extracted_hotel_class": 4,
        }
        rec = parse_rate(payload, RUN_DATE, 2, 1, "serpapi")
        self.assertEqual(rec.raw_rate, 63000)
        self.assertTrue(rec.available)
        self.assertTrue(rec.tax_included)   # lowest > before_taxes_fees
        self.assertEqual(rec.review_count, 610)

    def test_serpapi_property_without_rate_is_unavailable(self) -> None:
        """価格が返らない＝その日は売止。欠測と混同してはいけない."""
        rec = parse_rate({"name": "満室の宿"}, RUN_DATE, 2, 1, "serpapi")
        self.assertFalse(rec.available)
        self.assertIsNone(rec.raw_rate)

    def test_parse_money_variants(self) -> None:
        self.assertEqual(parse_money("¥45,000"), 45000.0)
        self.assertEqual(parse_money(45000), 45000.0)
        self.assertIsNone(parse_money(None))
        self.assertIsNone(parse_money("—"))


class NameMatchingTest(unittest.TestCase):
    def test_common_words_are_not_stripped(self) -> None:
        """『奈良』『ホテル』まで落とすと奈良ホテルが空文字になり突合不能になる（実際に踏んだ不具合）."""
        self.assertNotEqual(collect.normalize_name("奈良ホテル"), "")
        self.assertNotEqual(
            collect.normalize_name("奈良ホテル"),
            collect.normalize_name("春日ホテル"),
        )

    def test_normalization_ignores_punctuation_and_width(self) -> None:
        self.assertEqual(
            collect.normalize_name("ＡＮＤＯ ＨＯＴＥＬ　奈良若草山"),
            collect.normalize_name("ANDO HOTEL 奈良若草山"),
        )

    def test_matches_despite_subtitle(self) -> None:
        comps = {"c1": _comp("c1", "ANDO HOTEL 奈良若草山")}
        rec = parse_rate({"name": "ANDO HOTEL 奈良若草山〜DLIGHT LIFE & HOTELS〜",
                          "rate_per_night": {"extracted_lowest": 60000}},
                         RUN_DATE, 2, 1, "s")
        result = collect.match_records([rec], comps)
        self.assertEqual([cid for cid, _ in result.matched], ["c1"])

    def test_unrelated_property_is_reported_not_dropped(self) -> None:
        """コンペセット外の施設は捨てず unmatched に残す（新規開業の兆候になる）."""
        comps = {"c1": _comp("c1", "江戸三")}
        rec = parse_rate({"name": "全然ちがう宿泊施設XYZ",
                          "rate_per_night": {"extracted_lowest": 20000}},
                         RUN_DATE, 2, 1, "s")
        result = collect.match_records([rec], comps)
        self.assertEqual(result.matched, [])
        self.assertEqual(len(result.unmatched), 1)

    def test_coordinates_break_ties(self) -> None:
        near = _comp("near", "春日の宿", latitude=34.6815, longitude=135.8462)
        far = _comp("far", "春日の宿別館", latitude=35.0, longitude=136.5)
        rec = parse_rate({"name": "春日の宿",
                          "gps_coordinates": {"latitude": 34.6816, "longitude": 135.8463},
                          "rate_per_night": {"extracted_lowest": 50000}},
                         RUN_DATE, 2, 1, "s")
        result = collect.match_records([rec], {"near": near, "far": far})
        self.assertEqual(result.matched[0][0], "near")


class ScheduleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings.load(ROOT / "config")
        self.config = json.loads(
            (ROOT / "config" / "sources.json").read_text(encoding="utf-8")
        )

    def test_near_term_collected_daily(self) -> None:
        plan = schedule.build_plan(self.config, self.settings, RUN_DATE)
        near_leads = {t.lead_days for t in plan.tasks if t.lead_days <= 14}
        self.assertEqual(near_leads, set(range(15)))

    def test_every_stay_date_refreshed_within_its_cadence(self) -> None:
        """スロット方式が機能しているか — far帯の各日が7日以内に必ず1回は拾われる."""
        seen: dict[date, int] = {}
        for offset in range(7):
            run = RUN_DATE + timedelta(days=offset)
            for task in schedule.build_plan(self.config, self.settings, run).tasks:
                seen[task.stay_date] = seen.get(task.stay_date, 0) + 1
        far_dates = [
            RUN_DATE + timedelta(days=lead) for lead in range(60, 100)
        ]
        missed = [d for d in far_dates if d not in seen]
        self.assertEqual(missed, [], f"7日間で一度も収集されない日がある: {missed[:5]}")

    def test_boost_does_not_force_daily_far_out(self) -> None:
        """遠い日付の週末・イベントを毎日取りに行くのは無駄（実際に踏んだ設定ミス）."""
        plan = schedule.build_plan(self.config, self.settings, RUN_DATE)
        far = [t for t in plan.tasks if t.lead_days > 60]
        horizon = int(self.config["collection"]["horizon_days"])
        self.assertLess(len(far), (horizon - 60) * 0.5,
                        "リード60日超の収集が多すぎる（boost の上限が効いていない）")

    def test_estimate_reports_all_three_baselines(self) -> None:
        est = schedule.estimate_monthly(self.config, self.settings, RUN_DATE,
                                        competitor_count=12)
        self.assertGreater(est["requests_per_property"], est["requests_area_daily"])
        self.assertGreater(est["requests_area_daily"], est["requests"])
        self.assertGreater(est["compression_vs_area"], 1.0)
        self.assertTrue(est["within_cap"])


class DiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads(
            (ROOT / "config" / "sources.json").read_text(encoding="utf-8")
        )
        self.origin = (34.6815, 135.8462)

    def _place(self, name, lat, lon, level, rating, reviews) -> PlaceRecord:
        return PlaceRecord(place_id=name, name=name, latitude=lat, longitude=lon,
                           price_level=level, rating=rating, review_count=reviews)

    def test_excludes_by_business_type_and_thin_reviews(self) -> None:
        places = [
            self._place("高級旅館", 34.682, 135.846, "PRICE_LEVEL_EXPENSIVE", 4.5, 200),
            self._place("奈良ゲストハウス", 34.682, 135.846, "PRICE_LEVEL_INEXPENSIVE", 4.5, 200),
            self._place("口コミ僅少の宿", 34.682, 135.846, "PRICE_LEVEL_EXPENSIVE", 4.9, 3),
        ]
        names = {c.name for c in discovery.score_candidates(places, self.origin, self.config)}
        self.assertEqual(names, {"高級旅館"})

    def test_small_and_close_outranks_large_and_far(self) -> None:
        places = [
            self._place("近隣小規模", 34.6820, 135.8465, "PRICE_LEVEL_VERY_EXPENSIVE", 4.6, 200),
            self._place("遠方大規模", 34.6600, 135.8100, "PRICE_LEVEL_VERY_EXPENSIVE", 4.6, 200),
        ]
        rooms = {"近隣小規模": 8, "遠方大規模": 300}
        ranked = discovery.score_candidates(places, self.origin, self.config, rooms)
        self.assertEqual(ranked[0].name, "近隣小規模")

    def test_unknown_attributes_are_flagged_not_guessed(self) -> None:
        places = [self._place("価格帯不明の宿", 34.682, 135.846, "", 4.4, 200)]
        cand = discovery.score_candidates(places, self.origin, self.config)[0]
        self.assertIn("price_level_unknown", cand.flags)
        self.assertIn("rooms_unknown", cand.flags)

    def test_generated_config_marks_meal_data_for_human_review(self) -> None:
        places = [self._place("宿A", 34.682, 135.846, "PRICE_LEVEL_EXPENSIVE", 4.5, 200)]
        cands = discovery.assign_tiers(
            discovery.score_candidates(places, self.origin, self.config), self.config
        )
        generated = discovery.to_compset_config(cands, self.origin, {}, overrides={})
        entry = generated["competitors"][0]
        self.assertIn("meal_and_uplift_unset", entry["needs_review"])

    def test_overrides_survive_regeneration(self) -> None:
        """再発見のたびに人手の知見が消えてはいけない."""
        places = [self._place("宿A", 34.682, 135.846, "PRICE_LEVEL_EXPENSIVE", 4.5, 200)]
        cands = discovery.assign_tiers(
            discovery.score_candidates(places, self.origin, self.config), self.config
        )
        overrides = {"宿A": {"rooms": 12, "meal_included": "dinner_breakfast",
                            "dinner_uplift": 0, "breakfast_uplift": 0, "confirmed": True}}
        entry = discovery.to_compset_config(cands, self.origin, {}, overrides)["competitors"][0]
        self.assertEqual(entry["meal_included"], "dinner_breakfast")
        self.assertEqual(entry["rooms"], 12)
        self.assertNotIn("meal_and_uplift_unset", entry["needs_review"])


class HttpClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self._tmp.name)
        self._real_urlopen = urllib.request.urlopen
        self.calls = 0

    def tearDown(self) -> None:
        urllib.request.urlopen = self._real_urlopen
        self._tmp.cleanup()

    def _stub(self, payload: dict, status: int = 200):
        outer = self

        class _Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None, context=None):
            outer.calls += 1
            if status >= 400:
                raise urllib.error.HTTPError(
                    req.full_url, status, "err", {},
                    io.BytesIO(json.dumps(payload).encode()),
                )
            return _Resp(json.dumps(payload).encode())

        urllib.request.urlopen = fake_urlopen

    def _client(self, **kw) -> HttpClient:
        return HttpClient(source="test", raw_dir=self.raw,
                          rate_limiter=RateLimiter(per_second=1000.0), **kw)

    def test_second_call_hits_cache_and_skips_network(self) -> None:
        self._stub({"ok": True})
        client = self._client()
        first = client.request_json("https://example.test/x", run_date=RUN_DATE,
                                    params={"q": "a"})
        second = client.request_json("https://example.test/x", run_date=RUN_DATE,
                                     params={"q": "a"})
        self.assertEqual(first, second)
        self.assertEqual(self.calls, 1)
        self.assertEqual(client.network_calls, 1)
        self.assertEqual(client.cache_hits, 1)

    def test_api_key_is_not_written_to_cache(self) -> None:
        self._stub({"ok": True})
        self._client().request_json("https://example.test/x", run_date=RUN_DATE,
                                    params={"q": "a", "api_key": "SECRET123"})
        blob = "".join(p.read_text(encoding="utf-8")
                       for p in self.raw.rglob("*.json"))
        self.assertNotIn("SECRET123", blob)

    def test_client_error_is_not_retried(self) -> None:
        self._stub({"error": "bad request"}, status=400)
        with self.assertRaises(SourceError):
            self._client().request_json("https://example.test/x", run_date=RUN_DATE)
        self.assertEqual(self.calls, 1)   # 4xx を4回叩かない

    def test_offline_mode_fails_loudly_on_cache_miss(self) -> None:
        with self.assertRaises(SourceError):
            self._client(offline=True).request_json(
                "https://example.test/x", run_date=RUN_DATE
            )


class FixturePipelineTest(unittest.TestCase):
    """フィクスチャは実コネクタと同じパーサを通る（形の乖離を防ぐ）."""

    def setUp(self) -> None:
        self.dir = ROOT / "data" / "fixtures"
        if not (self.dir / "places_nearby.json").exists():
            self.skipTest("フィクスチャ未生成（scripts/make_fixtures.py）")

    def test_places_fixture_respects_radius(self) -> None:
        source = FixturePlacesSource(self.dir)
        wide = source.nearby_lodging(34.6815, 135.8462, 2500, RUN_DATE)
        narrow = source.nearby_lodging(34.6815, 135.8462, 800, RUN_DATE)
        self.assertGreater(len(wide), len(narrow))
        self.assertTrue(all(r.distance_km(34.6815, 135.8462) <= 0.8 for r in narrow))

    def test_rate_fixture_is_flagged(self) -> None:
        records = FixtureRatesSource(self.dir).rates_for_date("q", RUN_DATE, RUN_DATE)
        self.assertTrue(records)
        self.assertTrue(all(r.is_fixture for r in records))


class SurveyTest(unittest.TestCase):
    def _pos(self, **kw) -> survey.DayPosition:
        base = dict(stay_date=RUN_DATE, dow="SAT", season_label="秋",
                    day_class="PEAK:WEEKEND", lead_days=30, comp_n=6)
        base.update(kw)
        return survey.DayPosition(**base)

    def test_thin_sample_refuses_to_judge(self) -> None:
        pos = self._pos(comp_n=2)
        survey._classify(pos, 1.15, 0.4, 0.15)
        self.assertEqual(pos.signal, "DATA_THIN")

    def test_single_signal_does_not_move_price(self) -> None:
        """5室では単一シグナルは1件の予約で裏切られる。慎重側に倒す設計."""
        pos = self._pos(position=1.00, soldout_ratio=0.0, pace_z=0.0, remaining=3)
        survey._classify(pos, 1.15, 0.4, 0.15)
        self.assertEqual(pos.signal, "HOLD")

    def test_two_signals_trigger_raise(self) -> None:
        pos = self._pos(position=1.00, soldout_ratio=0.6, pace_z=0.0, remaining=3)
        survey._classify(pos, 1.15, 0.4, 0.15)
        self.assertEqual(pos.signal, "RAISE")

    def test_sold_out_property_cannot_be_raised(self) -> None:
        pos = self._pos(position=1.00, soldout_ratio=0.8, pace_z=0.5, remaining=0)
        survey._classify(pos, 1.15, 0.4, 0.15)
        self.assertNotEqual(pos.signal, "RAISE")

    def test_expensive_and_soft_market_triggers_lower(self) -> None:
        pos = self._pos(position=1.60, soldout_ratio=0.0, pace_z=-0.5, remaining=4)
        survey._classify(pos, 1.15, 0.4, 0.15)
        self.assertEqual(pos.signal, "LOWER")


class EndToEndCollectedTest(unittest.TestCase):
    """収集済みCSV → 推奨 → サーベイ の一気通貫."""

    def setUp(self) -> None:
        self.rates = ROOT / "data" / "comp_rates_collected.csv"
        self.compset = ROOT / "config" / "compset.generated.json"
        if not self.rates.exists() or not self.compset.exists():
            self.skipTest("収集済みデータ未生成（discover_compset.py → collect_rates.py）")

    def test_uses_latest_reading_per_stay_date(self) -> None:
        """階層化収集では毎日は更新されない。当日分だけ見ると大半が欠測になる（実際に踏んだ不具合）."""
        ctx = build_context(ROOT, RUN_DATE, 120,
                            compset_file=self.compset, rates_file=self.rates)
        with_data = [d for d, s in ctx.comp_snapshots.items() if s.sample_size >= 3]
        self.assertGreater(len(with_data), 60,
                           "最新観測の引き当てが効いていない（欠測が多すぎる）")
        self.assertTrue(all(age >= 0 for age in ctx.data_age.values()))

    def test_survey_flags_conflicts_between_market_and_engine(self) -> None:
        ctx = build_context(ROOT, RUN_DATE, 120,
                            compset_file=self.compset, rates_file=self.rates)
        report = survey.build(ctx.settings, ctx, fixture=True)
        self.assertEqual(len(report.days), len(ctx.recommendations))
        for day in report.days:
            if day.signal == "LOWER" and day.our_recommended > day.our_current * 1.02:
                self.assertTrue(day.conflict)

    def test_fixture_data_is_always_labelled(self) -> None:
        ctx = build_context(ROOT, RUN_DATE, 30,
                            compset_file=self.compset, rates_file=self.rates)
        rendered = survey.render(survey.build(ctx.settings, ctx, fixture=True))
        self.assertIn("フィクスチャ", rendered)


class GeoTest(unittest.TestCase):
    def test_haversine_known_distance(self) -> None:
        # 春日大社付近 → 近鉄奈良駅付近 は約 1.6km
        km = haversine_km(34.6815, 135.8462, 34.6836, 135.8290)
        self.assertGreater(km, 1.2)
        self.assertLess(km, 2.0)


if __name__ == "__main__":
    unittest.main()
