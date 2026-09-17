"""設定ファイルの読み込みとカレンダー解決."""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

DOW = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def benchmark_provenance_warning(calendar: dict[str, Any]) -> str | None:
    """ブッキングカーブの出所が不明なら警告文を返す（問題なければ None）.

    擬似データ由来の想定値と実測値が、設定ファイル上で区別できなかった。
    実際にこれで事故が起きている: 擬似データの想定カーブ（120日前に8%が
    入っている前提）を実運用の値と取り違えたまま動かしており、
    実測（0.5%）とかけ離れていたため進捗が常に「大幅な遅れ」と判定され、
    リード14日以遠のほぼ全日で価格を押し下げていた。
    値が妥当に見えるぶん、内部からは気づけない種類の誤りである。

    そのため出所（_source）の記載を必須とし、無ければ起動時に警告する。
    エラーにはしない。出所不明でも検証や試作は回せるべきで、
    ここで止めるとブートストラップができなくなる。
    """
    bench = calendar.get("pace_benchmark")
    if not isinstance(bench, dict):
        return "pace_benchmark がありません。予約進捗の評価ができません。"
    source = str(bench.get("_source") or "").strip()
    if not source:
        return (
            "pace_benchmark に _source がありません（出所不明のベンチマーク）。\n"
            "  擬似データ由来の想定値と実測値が区別できない状態です。\n"
            "  実測から復元した値であれば、期間・母数・除外条件を _source に書いてください。\n"
            "  例: \"実予約明細から復元（2026-02〜09）。火曜・水曜は閉館日のため除外。\"\n"
            "  出所不明のカーブは、進捗判定を系統的に狂わせたまま正常値に見えます。"
        )
    return None


def warn_if_benchmark_provenance_unknown(calendar: dict[str, Any], *,
                                         source: str = "") -> None:
    message = benchmark_provenance_warning(calendar)
    if message:
        where = f"（{source}）" if source else ""
        print(f"⚠️  出所不明のベンチマーク{where}\n  {message}", file=sys.stderr)


# 旧構造 → 新構造。旧キーのまま読むと閉館日が静かに消え、販売していない日に
# 価格が出て稼働率の分母も狂う。黙って落とさず、移行先を名指しで知らせる。
LEGACY_CLOSED_DAY_KEYS = {"weekdays": "closed_weekdays",
                          "open_dates": "extra_open",
                          "closed_dates": "extra_closed"}


def closed_days_migration_warning(calendar: dict[str, Any]) -> str | None:
    """閉館日の設定が旧構造のままなら、移行先を示す警告文を返す."""
    cfg = calendar.get("closed_days")
    if not isinstance(cfg, dict):
        return None
    stale = [k for k in LEGACY_CLOSED_DAY_KEYS if cfg.get(k)]
    if not stale:
        return None
    moves = "、".join(f"{k} → {LEGACY_CLOSED_DAY_KEYS[k]}" for k in stale)
    return (
        f"closed_days が旧構造のままです（{moves}）。\n"
        "  例外営業の出所（actual / planned）を持てないため、稼働率の分母を\n"
        "  あとから検証できません。今回は旧キーを読んで動かしますが、移行してください。"
    )


def _migrate_closed_days(calendar: dict[str, Any], *, source: str = "") -> None:
    """旧キーを新キーへ読み替える（警告つき）.

    エラーにはしない。設定ファイルが旧構造のまま残っている施設でも
    価格計算は回るべきで、ここで止めると移行の途中で何も動かせなくなる。
    """
    message = closed_days_migration_warning(calendar)
    if not message:
        return
    where = f"（{source}）" if source else ""
    print(f"⚠️  閉館日の設定が旧構造です{where}\n  {message}", file=sys.stderr)
    cfg = calendar["closed_days"]
    for legacy, current in LEGACY_CLOSED_DAY_KEYS.items():
        if cfg.get(legacy) and not cfg.get(current):
            cfg[current] = cfg[legacy]


def expected_occupancy_provenance_warning(prop: dict[str, Any]) -> str | None:
    """最終稼働の見込みの出所が不明なら警告文を返す（問題なければ None）.

    pace_benchmark と同じ扱いにする。この値は
    expected_rooms = 客室数 × この値 × 進捗率 として「あるべきOTB」を作るので、
    実測か願望かで内部需要シグナルの符号ごと変わる。しかも値そのものは
    どちらでも妥当に見えるため、内部からは区別がつかない。

    実際にこれで事故が起きている: 経営目標（PEAK 0.95 など）が予測値の
    位置に直書きされており、実績（PEAK 0.49 など）から全シーズンで
    40〜59ポイント上振れしていた。実勢どおりに埋まった日でも常に
    進捗不足と判定され、価格を約20%押し下げていた。
    """
    table = prop.get("expected_final_occupancy")
    if not isinstance(table, dict):
        return ("expected_final_occupancy がありません。"
                "最終稼働の見込みが既定値に落ちています。")
    seasons = [k for k in table if not k.startswith("_")]
    if not seasons:
        return "expected_final_occupancy にシーズンの値がありません。"
    if not str(table.get("_source") or "").strip():
        return (
            "expected_final_occupancy に _source がありません（出所不明の稼働見込み）。\n"
            "  経営目標と実測値が区別できない状態です。\n"
            "  実績から算出した値であれば、期間・営業日数・室夜数を _source に書いてください。\n"
            "  例: \"実予約明細から算出（2026-02-06〜09-17）。営業168日 × 5室 = 840室夜。\"\n"
            "  目標値をここに置くと、実勢どおりに埋まった日でも進捗不足と判定され、\n"
            "  全日が値下げ方向へ押されます（scripts/build_expected_occupancy.py で再算出できます）。"
        )
    return None


def warn_if_expected_occupancy_provenance_unknown(prop: dict[str, Any], *,
                                                  source: str = "") -> None:
    message = expected_occupancy_provenance_warning(prop)
    if message:
        where = f"（{source}）" if source else ""
        print(f"⚠️  出所不明の稼働見込み{where}\n  {message}", file=sys.stderr)


# 競合の識別情報のうち、対応表から取り込む項目。
# ここに無い項目（tier / weight / pricing_basis / meal_included など）は
# 価格計算に必要なので compset.json 側に残っている。
IDENTITY_FIELDS = ("name", "place_id", "latitude", "longitude",
                   "distance_km", "rating", "review_count")


def private_data_paths(root: Path, sources: dict[str, Any], key: str = "path",
                       fallback_key: str = "fallback_paths") -> list[Path]:
    """Private 側データの探索先。engine/ からの相対、または絶対パス.

    先に書いたものから順に探し、最初に見つかったものを使う。
    末尾には本体に同梱した架空サンプルを置いてあり、Private 側が
    無い環境でもパイプラインは最後まで通る。
    """
    block = sources.get("compset_names")
    if not isinstance(block, dict):
        return []
    engine_root = root.parent          # root は engine/config
    candidates = [block.get(key, "")] + list(block.get(fallback_key) or [])
    out: list[Path] = []
    for value in candidates:
        if not str(value).strip():
            continue
        path = Path(value)
        out.append(path if path.is_absolute() else (engine_root / path))
    return out


def resolve_private_json(root: Path, sources: dict[str, Any], key: str,
                         fallback_key: str) -> tuple[dict, Path | None]:
    """Private 側 JSON を読む。無ければ (空, None)."""
    for path in private_data_paths(root, sources, key, fallback_key):
        if path.exists():
            return _load_json(path), path
    return {}, None


def _name_table_paths(root: Path, sources: dict[str, Any]) -> list[Path]:
    return private_data_paths(root, sources)


def load_competitor_names(root: Path, sources: dict[str, Any]
                          ) -> tuple[dict[str, dict], Path | None]:
    """comp_id → 識別情報 の対応表を読む（無ければ空）.

    **突き合わせは comp_id で行う。表示名では行わない。**
    表示名を鍵にすると、施設が改名した瞬間に静かに外れ、その施設だけが
    匿名表示へ戻るうえ、誰も気づかない。comp_id は発見時に採番した
    内部IDなので、改名しても動かない。
    """
    for path in _name_table_paths(root, sources):
        if not path.exists():
            continue
        table = _load_json(path)
        entries = table.get("competitors")
        if isinstance(entries, dict):
            return {str(k): dict(v) for k, v in entries.items()}, path
        return {}, path
    return {}, None


def _apply_competitor_names(compset: dict[str, Any], table: dict[str, dict],
                            table_path: Path | None) -> None:
    """対応表を競合の定義へ流し込む（破壊的・読み込み時に1回だけ）.

    対応表が無い、あるいは comp_id が載っていない競合は、表示名を
    comp_id のままにする。落とさない: 価格計算に必要な項目は
    compset.json 側に揃っているので、匿名のままでも推奨価格は出せる。
    """
    competitors = compset.get("competitors") or []
    if not competitors:
        return
    missing: list[str] = []
    for comp in competitors:
        comp_id = comp.get("id", "")
        entry = table.get(comp_id)
        if not entry:
            comp.setdefault("name", comp_id)
            if not str(comp.get("name") or "").strip():
                comp["name"] = comp_id
            if table:
                missing.append(comp_id)
            continue
        for field_name in IDENTITY_FIELDS:
            if field_name in entry:
                comp[field_name] = entry[field_name]
        if not str(comp.get("name") or "").strip():
            comp["name"] = comp_id

    if table_path is None:
        print(f"ℹ️  競合名の対応表が見つかりません（{len(competitors)}施設）。\n"
              "  競合は comp_id（auto01 など）で表示します。"
              "価格計算に必要な項目は compset.json 側にあるため、推奨価格は変わりません。\n"
              "  実名で表示するには Kanoya-data を取得し、"
              "sources.json の compset_names.path を合わせてください。",
              file=sys.stderr)
    elif missing:
        head = ", ".join(missing[:5])
        more = f" 他{len(missing) - 5}件" if len(missing) > 5 else ""
        print(f"ℹ️  対応表に無い競合が {len(missing)}件 あります（{head}{more}）。\n"
              f"  対応表: {table_path}\n"
              "  該当分だけ comp_id のまま表示します。"
              "発見（discover_compset.py）で増えた競合は対応表にも追記してください。",
              file=sys.stderr)


def _warn_about_product_pricing(settings) -> None:
    """商品形態別フロアと部屋代アンカーの整合を起動時に確認する.

    暫定フロアを確定値と取り違えたまま本番配信すると、
    貢献利益を割った価格を出し続けることになる。黙って通さない。
    """
    from . import products          # 遅延importで循環を避ける
    try:
        pricing = products.load(settings)
    except Exception:
        return
    for message in products.warnings_for(pricing):
        print(f"⚠️  {message}", file=sys.stderr)


@dataclass
class Competitor:
    id: str
    name: str
    tier: str
    weight: float
    rooms: int
    distance_km: float
    pricing_basis: str
    meal_included: str
    dinner_uplift: float
    breakfast_uplift: float
    place_id: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    needs_review: list[str] = field(default_factory=list)


@dataclass
class Settings:
    root: Path
    property: dict[str, Any]
    compset: dict[str, Any]
    calendar: dict[str, Any]
    competitors: dict[str, Competitor] = field(default_factory=dict)

    @classmethod
    def load(cls, config_dir: str | Path, *,
             compset_file: str | Path | None = None) -> "Settings":
        """設定を読み込む.

        compset_file を指定すると、既定の compset.json ではなく任意の
        コンペセット定義（discover_compset.py の生成物など）を使用する。
        """
        root = Path(config_dir)
        prop = _load_json(root / "property.json")
        compset_path = Path(compset_file) if compset_file else root / "compset.json"
        if not compset_path.is_absolute() and compset_file:
            compset_path = Path(compset_file)
        if compset_path.exists():
            compset = _load_json(compset_path)
        else:
            # コンペセットはまだ存在しないことがある（初回の発見前、
            # あるいはカレンダーだけを使う検証データ生成時）。
            # ここで落とすとブートストラップができなくなるため、空で続行する。
            compset = {"competitors": [], "tiers": {}, "_missing": str(compset_path)}
        # 競合の識別情報は Private 側（Kanoya-data）にある。無くても動く。
        sources_path = root / "sources.json"
        sources = _load_json(sources_path) if sources_path.exists() else {}
        names, names_path = load_competitor_names(root, sources)
        _apply_competitor_names(compset, names, names_path)
        calendar = _load_json(root / "calendar.json")
        warn_if_benchmark_provenance_unknown(calendar, source=str(root / "calendar.json"))
        _migrate_closed_days(calendar, source=str(root / "calendar.json"))
        warn_if_expected_occupancy_provenance_unknown(
            prop, source=str(root / "property.json"))
        competitors = {
            c["id"]: Competitor(
                id=c["id"],
                name=c.get("name") or c["id"],
                tier=c["tier"],
                weight=float(c["weight"]),
                rooms=int(c.get("rooms") or 0),
                distance_km=float(c.get("distance_km") or 0.0),
                pricing_basis=c["pricing_basis"],
                meal_included=c["meal_included"],
                dinner_uplift=float(c.get("dinner_uplift", 0)),
                breakfast_uplift=float(c.get("breakfast_uplift", 0)),
                place_id=c.get("place_id", ""),
                latitude=float(c.get("latitude") or 0.0),
                longitude=float(c.get("longitude") or 0.0),
                needs_review=list(c.get("needs_review") or []),
            )
            for c in compset["competitors"]
        }
        settings = cls(root=root, property=prop, compset=compset, calendar=calendar,
                       competitors=competitors)
        _warn_about_product_pricing(settings)
        return settings

    # ---- カレンダー解決 -------------------------------------------------

    def season_of(self, day: date) -> tuple[str, str]:
        """該当日のシーズン区分とラベルを返す（年跨ぎのレンジにも対応）."""
        key = day.strftime("%m-%d")
        for entry in self.calendar["seasons"]:
            lo, hi = entry["from"], entry["to"]
            hit = lo <= key <= hi if lo <= hi else (key >= lo or key <= hi)
            if hit:
                return entry["season"], entry["label"]
        return self.calendar.get("default_season", "SHOULDER"), "標準期"

    def event_score_of(self, day: date) -> tuple[float, str]:
        """カレンダー登録済みイベントのスコア（最大値）とラベル."""
        best, label = 0.0, ""
        for entry in self.calendar.get("events", []):
            if parse_date(entry["from"]) <= day <= parse_date(entry["to"]):
                if float(entry["score"]) > best:
                    best, label = float(entry["score"]), entry["label"]
        return best, label

    def is_holiday(self, day: date) -> bool:
        return day.strftime("%Y-%m-%d") in set(self.calendar.get("holidays_2026", []))

    def is_holiday_eve(self, day: date) -> bool:
        """翌日が休日（祝日 or 土日）で、当日が平日扱いなら『連休前夜』."""
        nxt = day + timedelta(days=1)
        next_is_off = self.is_holiday(nxt) or nxt.weekday() >= 5
        today_is_weekday = day.weekday() <= 3 and not self.is_holiday(day)
        return next_is_off and today_is_weekday

    def dow_of(self, day: date) -> str:
        return DOW[day.weekday()]

    # ---- 閉館日（定休日） -----------------------------------------------

    def closed_day_config(self) -> dict:
        """閉館日の設定。無い施設でも動くよう、常に辞書を返す."""
        cfg = self.calendar.get("closed_days")
        return cfg if isinstance(cfg, dict) else {}

    def closed_weekdays(self) -> set[str]:
        return set(self.closed_day_config().get("closed_weekdays") or [])

    def exception_days(self, key: str, source: str | None = None) -> set[date]:
        """extra_open / extra_closed の日付を集める.

        source を指定すると "actual"（実績で確認した日）か
        "planned"（人が入れた予定）で絞り込める。両者が混ざったまま
        稼働率を出すと、あとから分母を検証できなくなるため分けている。

        source を書き忘れたエントリは "planned" として扱う。実績だと
        言い切れないものを実績側へ入れるほうが危ないため、安全側へ倒す。
        """
        out: set[date] = set()
        for entry in self.closed_day_config().get(key) or []:
            if isinstance(entry, str):          # 日付だけの簡易記法
                value, entry_source = entry, "planned"
            else:
                value = entry.get("date", "")
                entry_source = (entry.get("source") or "planned").lower()
            if not value:
                continue
            if source is not None and entry_source != source:
                continue
            out.add(parse_date(value))
        return out

    def exception_rate(self) -> float:
        """閉館日のうち例外営業になる割合（将来期間の営業日数の上振れ分）."""
        try:
            rate = float(self.closed_day_config().get("exception_rate", 0.0))
        except (TypeError, ValueError):
            return 0.0
        return min(1.0, max(0.0, rate))

    def is_closed(self, day: date) -> bool:
        """当日が閉館日（販売しない日）か.

        曜日指定と日付単位の指定を併用でき、**日付指定が曜日指定を上書きする**。
        定休日は固定ではなく、需要に応じた例外営業が通年で発生するため
        （2026年2〜9月の実績で閉館日64日のうち8日）、曜日だけでは表現できない。

        closed_days が無い設定、あるいは closed_weekdays が空の設定でも動く。
        定休日を持たない施設にもエンジンをそのまま適用できるようにするため。
        """
        cfg = self.closed_day_config()
        if not cfg:
            return False
        if day in self.exception_days("extra_open"):
            return False          # 日付指定の営業日が最優先
        if day in self.exception_days("extra_closed"):
            return True
        return self.dow_of(day) in self.closed_weekdays()

    def is_open(self, day: date) -> bool:
        return not self.is_closed(day)

    def closed_by_weekday(self, day: date) -> bool:
        """曜日ルールだけで見たときに閉館日か（例外営業の見込みを出す母数）."""
        return self.dow_of(day) in self.closed_weekdays()

    def open_days(self, start: date, days: int) -> list[date]:
        """start から days 日分のうち、営業日だけを返す（稼働率の分母）."""
        return [d for d in (start + timedelta(days=i) for i in range(days))
                if self.is_open(d)]

    def day_class(self, day: date) -> str:
        """シーズン×曜日タイプの『日カテゴリ』。5室規模での統計プーリング単位."""
        season, _ = self.season_of(day)
        dow = self.dow_of(day)
        if dow in ("FRI", "SAT") or self.is_holiday_eve(day):
            bucket = "WEEKEND"
        elif dow == "SUN":
            bucket = "SUNDAY"
        else:
            bucket = "WEEKDAY"
        return f"{season}:{bucket}"
