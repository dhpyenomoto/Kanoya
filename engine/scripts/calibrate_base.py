"""基準価格の四半期校正レポート.

    python3 scripts/calibrate_base.py --position 1.15 --revpar 62000 --occ 0.72
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import compset  # noqa: E402
from kanoya_rm.calibrate import calibrate  # noqa: E402
from kanoya_rm.config import Settings, load_csv, parse_date  # noqa: E402


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--position", type=float, default=1.15,
                        help="競合NAR中央値に対する目標ポジション（倍）")
    parser.add_argument("--revpar", type=float, default=62000, help="年間RevPAR目標（円）")
    parser.add_argument("--occ", type=float, default=0.72, help="想定年間稼働率")
    args = parser.parse_args()

    settings = Settings.load(root / "config")
    rows = load_csv(root / "data" / "comp_rates.csv")
    snapshot = max(parse_date(r["snapshot_date"]) for r in rows)

    by_stay = defaultdict(list)
    for row in rows:
        if parse_date(row["snapshot_date"]) == snapshot:
            by_stay[parse_date(row["stay_date"])].append(row)
    snapshots = {d: compset.build_snapshot(settings, d, rs) for d, rs in by_stay.items()}

    result = calibrate(settings, snapshots, snapshot,
                       target_position=args.position,
                       target_revpar=args.revpar,
                       assumed_occupancy=args.occ)

    print("=" * 74)
    print("  基準価格（アンカー）校正レポート")
    print("=" * 74)
    print(f"現行アンカー                : {result.current_anchor:>10,.0f} 円")
    print(f"競合NAR中央値（全期間中央値）: {result.comp_median_overall:>10,.0f} 円")
    print(f"現行の含意ポジション        : {result.implied_position:>10.2f} 倍")
    print("-" * 74)
    print(f"条件A 市場整合アンカー      : {result.market_anchor:>10,.0f} 円"
          f"（目標ポジション {args.position:.2f} 倍）")
    print(f"条件B 予算整合アンカー      : {result.budget_anchor:>10,.0f} 円"
          f"（RevPAR {args.revpar:,.0f} 円 / 稼働 {args.occ:.0%}）")
    print(f"乖離（予算 ÷ 市場 − 1）     : {result.gap_pct:>+10.1%}")
    print("-" * 74)
    print(f"▶ 推奨アンカー              : {result.recommended_anchor:>10,.0f} 円")
    print(f"  判定: {result.verdict}")
    print("\n※ 本値を config/property.json の base.anchor_room_rate に反映し、")
    print("   変更理由・変更者・適用日を意思決定ログに残すこと（四半期に1回）。")


if __name__ == "__main__":
    main()
