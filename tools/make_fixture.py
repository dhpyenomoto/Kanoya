"""検証用フィクスチャの生成。

実データ（Places API の日次スナップショット＋自社PMS）が無い環境でも、
パイプライン全体を動かして数字の筋が通っているか確認できるようにする。

やっていること：
  1. 施設ごとに「真の」日次販売室数を作る（催事・曜日・水準で変動）
  2. 販売室数 × 真の投稿率 でレビューを発生させ、平均10日遅れて投稿させる
  3. それを累計に積み上げて userRatingCount のスナップショット列にする
  4. 自社ぶんだけ PMS 実績 CSV としても書き出す

推定器は 3 と 4 だけを見て 1 を復元する。復元値が真値に近ければ手法は妥当。
このスクリプトは本番パイプラインからは呼ばれない。

NOTE: レビューの発生はポアソン過程なので、5室規模では 90日で 60件程度しか出ず、
相対標準誤差は 13% 前後になる。つまり乱数の引き次第で推定稼働は真値から ±10pt
ぶれる。これは手法の性質であってバグではない。同梱フィクスチャは「たまたま最悪を
引いた図」ではなく代表的な実現値を見せたいので、SEED を固定して選んである。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TRUE_RATE = 0.105          # 1販売室あたりの真のレビュー投稿率
MEAN_LAG = 10              # 滞在から投稿までの平均日数
SEED = 71                  # NOTE 参照。代表的な実現値になる種を選んである

# name, place_id, rooms, restaurant_share, rating, (今期, 前期, 前々期) の目標稼働
PROPERTIES = [
    ("奈良春日 鹿のや",                    "ChIJ_SAMPLE_KANOYA_0001", 5,  0.45, 4.53, (0.754, 0.684, 0.700)),
    ("競合A（奈良公園・小規模ラグジュアリー）", "ChIJ_SAMPLE_COMP_A_0002", 8,  0.00, 4.70, (0.763, 0.823, 0.810)),
    ("競合B（旅館・1泊2食）",               "ChIJ_SAMPLE_COMP_B_0003", 12, 0.15, 4.50, (0.719, 0.699, 0.705)),
    ("競合C（町家一棟貸し）",               "ChIJ_SAMPLE_COMP_C_0004", 4,  0.00, 4.60, (0.633, 0.433, 0.400)),
    ("競合D（フルサービスホテル）",          "ChIJ_SAMPLE_COMP_D_0005", 40, 0.30, 4.20, (0.744, 0.764, 0.755)),
    ("競合E（デザイン系ブティック）",        "ChIJ_SAMPLE_COMP_E_0006", 15, 0.00, 4.60, (0.775, 0.765, 0.760)),
]

# エリア母集団の残り（コンプセット外。エリア需要の分母になる）
OTHERS = [
    ("ならまち ゲストハウス鹿声", 4, 4.40), ("東大寺前 旅館 月ヶ瀬", 6, 4.30),
    ("三条通 ホステル奈良", 5, 4.10),       ("高畑 町家宿 柿の木", 3, 4.60),
    ("猿沢 ビジネスイン",       6, 3.90),   ("春日野 旅籠",         4, 4.50),
    ("きたまち 一棟貸し 藤",    2, 4.70),   ("奈良町 ゲストハウス燈", 4, 4.40),
    ("大宮通 ホテル",           5, 4.00),   ("元林院 町家 灯り",     2, 4.60),
    ("紀寺 ゲストハウス",       3, 4.30),   ("船橋商店街 宿",        3, 4.20),
    ("般若寺 参道宿",           2, 4.50),
]

EVENTS = [("2026-03-01", 25), ("2026-04-05", 20), ("2025-11-20", 25), ("2025-10-25", 30)]


def seasonal(d: dt.date) -> float:
    """催事の山＋年周期＋曜日。エリア共通の需要形状。"""
    s = 1.0
    for iso, weight in EVENTS:
        ev = dt.date.fromisoformat(iso)
        gap = (d - ev).days
        s += (weight / 100.0) * math.exp(-((gap / 16.0) ** 2))
    # 夏枯れと冬枯れをゆるく入れる
    s *= 1.0 + 0.06 * math.sin(2 * math.pi * (d.timetuple().tm_yday - 100) / 365.0)
    s *= 1.10 if d.weekday() in (4, 5) else (0.94 if d.weekday() in (0, 1, 2) else 1.0)
    return s


def level_curve(targets: tuple[float, float, float], anchors: list[float], offset: int) -> float:
    """offset（窓末からの日数）に応じた水準。窓中心をアンカーに線形補間。"""
    xs = [45.0, 135.0, 225.0]
    if offset <= xs[0]:
        return anchors[0]
    if offset >= xs[2]:
        return anchors[2]
    for i in range(2):
        if xs[i] <= offset <= xs[i + 1]:
            t = (offset - xs[i]) / (xs[i + 1] - xs[i])
            return anchors[i] * (1 - t) + anchors[i + 1] * t
    return anchors[2]


def build_sold(rooms: int, targets: tuple[float, float, float], dates: list[dt.date], end: dt.date) -> dict[dt.date, int]:
    """目標稼働に収束するまで水準を調整し、日次販売室数を作る。"""
    anchors = list(targets)
    windows = [(0, 89), (90, 179), (180, 269)]

    def occ_of(d: dt.date) -> float:
        off = (end - d).days
        return min(0.99, max(0.03, level_curve(targets, anchors, off) * seasonal(d)))

    for _ in range(60):
        for i, (lo, hi) in enumerate(windows):
            days = [d for d in dates if lo <= (end - d).days <= hi]
            if not days:
                continue
            realized = sum(occ_of(d) for d in days) / len(days)
            if realized > 0:
                anchors[i] *= targets[i] / realized

    # 端数を持ち越して合計を保つ丸め
    sold: dict[dt.date, int] = {}
    acc = 0.0
    emitted = 0
    for d in dates:
        acc += rooms * occ_of(d)
        n = int(math.floor(acc + 0.5)) - emitted
        n = max(0, min(rooms, n))
        emitted += n
        sold[d] = n
    return sold


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def posted_counts(rng: random.Random, sold: dict[dt.date, int], share: float) -> dict[dt.date, int]:
    """滞在日の販売室数から、投稿日ベースのレビュー件数を作る。"""
    out: dict[dt.date, int] = {}
    for stay, n in sold.items():
        lam = n * TRUE_RATE / (1.0 - share)   # 外来客ぶんを含む見かけの件数
        for _ in range(poisson(rng, lam)):
            lag = max(2, min(28, int(round(rng.gauss(MEAN_LAG, 4.0)))))
            out[stay + dt.timedelta(days=lag)] = out.get(stay + dt.timedelta(days=lag), 0) + 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="検証用スナップショット／PMSフィクスチャを生成する")
    ap.add_argument("--asof", default="2026-08-15")
    ap.add_argument("--history-days", type=int, default=288)
    args = ap.parse_args()

    rng = random.Random(SEED)
    asof = dt.date.fromisoformat(args.asof)
    first = asof - dt.timedelta(days=args.history_days - 1)
    snap_dates = [first + dt.timedelta(days=i) for i in range(args.history_days)]

    # 滞在日は投稿日より前にあるので、生成範囲を前後に広げておく
    stay_dates = [first - dt.timedelta(days=30) + dt.timedelta(days=i) for i in range(args.history_days + 30)]
    window_end = asof - dt.timedelta(days=10)      # 不安定な尾を除いた集計窓の末日

    specs = [(n, pid, rooms, share, rating, tg) for n, pid, rooms, share, rating, tg in PROPERTIES]
    for i, (name, rooms, rating) in enumerate(OTHERS):
        base = 0.70 + 0.02 * ((i % 5) - 2)
        specs.append((name, f"ChIJ_SAMPLE_AREA_{i:04d}", rooms, 0.0, rating, (base, base - 0.01, base)))

    lines: list[str] = []
    area_places: list[dict] = []
    own_sold: dict[dt.date, int] = {}

    for name, pid, rooms, share, rating, targets in specs:
        sold = build_sold(rooms, targets, stay_dates, window_end)
        posts = posted_counts(rng, sold, share)

        total = 300 + rooms * 11          # 開業以来の累計（初期値）
        for d in snap_dates:
            total += posts.get(d, 0)
            lines.append(
                json.dumps(
                    {
                        "place_id": pid,
                        "date": d.isoformat(),
                        "user_rating_count": total,
                        "rating": rating,
                    },
                    ensure_ascii=False,
                )
            )
        area_places.append({"place_id": pid, "name": name, "primary_type": "japanese_inn"})
        if pid.startswith("ChIJ_SAMPLE_KANOYA"):
            own_sold = sold

    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "snapshots.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (ROOT / "data" / "area_places.json").write_text(
        json.dumps(area_places, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # 自社PMS：突合期間ぶん（261日）だけ書き出す
    pms_start = window_end - dt.timedelta(days=260)
    rows = ["date,rooms_sold,rooms_available"]
    truth = []
    for d in stay_dates:
        if pms_start <= d <= window_end:
            rows.append(f"{d.isoformat()},{own_sold[d]},5")
            truth.append(own_sold[d])
    (ROOT / "data" / "pms_daily.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    win90 = [d for d in stay_dates if 0 <= (window_end - d).days <= 89]
    print(f"生成: 施設 {len(specs)} / スナップショット {len(lines)} 行 / PMS {len(truth)} 日")
    print(f"真値（自社・直近90日）: 販売 {sum(own_sold[d] for d in win90)}室 "
          f"稼働 {sum(own_sold[d] for d in win90) / (5 * 90) * 100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
