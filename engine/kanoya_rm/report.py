"""出力レポート生成（意思決定ログ／承認キュー／週次ミーティング資料）."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from .pricing import Recommendation

ACTION_LABEL = {
    "AUTO_APPLY": "自動配信",
    "APPROVAL_REQUIRED": "要承認",
    "REJECTED_ANOMALY": "自動棄却",
}


def write_recommendations_csv(recs: dict[date, Recommendation], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "宿泊日", "曜日", "日カテゴリ", "シーズン", "リードタイム",
            "現行価格", "推奨部屋代", "変動率", "判定", "残室", "MLOS",
            "競合中央値(NAR)", "対競合ポジション", "イベント", "イベントスコア",
            "基準価格", "内部需要寄与", "競合寄与", "イベント寄与", "リード寄与",
            "素泊まり", "朝食のみ", "夕食のみ", "2食付き", "暫定フロア適用",
            "ガードレール",
        ])
        for day in sorted(recs):
            r = recs[day]
            c = {x.factor: x.yen for x in r.contributions}
            writer.writerow([
                day.isoformat(), r.dow, r.day_class, r.season_label, r.lead_days,
                round(r.current_rate), round(r.recommended_rate), f"{r.delta_pct:+.1%}",
                ACTION_LABEL[r.action], r.remaining, r.mlos,
                round(r.comp_median), f"{r.comp_position:.2f}" if r.comp_position else "",
                r.event_label, f"{r.event_score:.2f}",
                round(r.base_rate),
                round(c.get("demand", 0)), round(c.get("comp", 0)), round(c.get("event", 0)),
                round(c.get("lead", 0)),
                *[round(r.product_prices.get(f, 0)) for f in
                  ("room_only", "breakfast", "dinner", "two_meals")],
                1 if r.floor_provisional else 0,
                " / ".join(r.guardrail_notes),
            ])


def product_lines(rec: Recommendation) -> list[str]:
    """部屋代から導いた4形態の販売価格.

    OTAの管理画面に打ち込むのはこちら。recommended_rate は部屋代で、
    そのまま掲出する値ではない。
    """
    from .products import FORMS, LABELS
    if not rec.product_prices:
        return []
    out = ["  商品形態別の販売価格（1室2名1泊・税サ込）"]
    for form in FORMS:
        if form not in rec.product_prices:
            continue
        mark = " ←フロア" if rec.floor_form == form else ""
        out.append(f"    {LABELS[form]:<10}{rec.product_prices[form]:>10,.0f} 円{mark}")
    if rec.floor_provisional:
        out.append("    ※ 暫定フロアが適用されています。変動費から再算出するまで本番配信に使わないこと。")
    return out


def explain(rec: Recommendation) -> str:
    """1日分の『なぜこの価格か』を人間可読なウォーターフォールで返す."""
    lines = [
        f"■ {rec.stay_date.isoformat()}（{rec.dow}）{rec.season_label} / {rec.day_class}",
        f"  リードタイム {rec.lead_days}日 ／ 残室 {rec.remaining}室 ／ MLOS {rec.mlos}泊"
        + ("  ※gap night" if rec.gap_night else ""),
        f"  基準価格（部屋代）                 {rec.base_rate:>10,.0f} 円",
    ]
    running = rec.base_rate
    for c in rec.contributions:
        running += c.yen
        lines.append(
            f"   {c.note:<24} {c.yen:>+10,.0f} 円  (z={c.z:+.2f}) → {running:>9,.0f} 円"
        )
    lines.append(f"  モデル出力                        {rec.raw_price:>10,.0f} 円")
    for note in rec.guardrail_notes:
        lines.append(f"   ガードレール: {note}")
    lines.append(f"  ▶ 推奨 部屋代                     {rec.recommended_rate:>10,.0f} 円"
                 f"（現行 {rec.current_rate:,.0f} 円 / {rec.delta_pct:+.1%} / {ACTION_LABEL[rec.action]}）")
    lines.extend(product_lines(rec))
    if rec.comp_median:
        lines.append(f"  競合NAR中央値 {rec.comp_median:,.0f} 円 に対し {rec.comp_position:.2f} 倍"
                     + (f" ／ {rec.event_label}" if rec.event_label else ""))
        lines.append("  ※ 競合は『1室2名2食』基準のため、2食付き換算 "
                     f"{rec.two_meal_total:,.0f} 円 との比較。"
                     "競合の食事条件は未実測のため、この倍率は目安にとどめること。")
    return "\n".join(lines)


def summary(recs: dict[date, Recommendation]) -> str:
    if not recs:
        return "（推奨なし）"
    days = sorted(recs)
    total = len(days)
    counts: dict[str, int] = {}
    for r in recs.values():
        counts[r.action] = counts.get(r.action, 0) + 1
    avg_rec = sum(r.recommended_rate for r in recs.values()) / total
    avg_cur = sum(r.current_rate for r in recs.values()) / total
    up = sum(1 for r in recs.values() if r.recommended_rate > r.current_rate)
    down = sum(1 for r in recs.values() if r.recommended_rate < r.current_rate)
    mlos = sum(1 for r in recs.values() if r.mlos > 1)
    gaps = sum(1 for r in recs.values() if r.gap_night)

    lines = [
        f"対象期間            : {days[0]} 〜 {days[-1]}（{total}日）",
        f"現行 平均掲出価格   : {avg_cur:,.0f} 円",
        f"推奨 平均掲出価格   : {avg_rec:,.0f} 円（{avg_rec / avg_cur - 1:+.1%}）",
        f"値上げ / 値下げ 日数: {up} 日 / {down} 日",
        "判定内訳            : " + " ／ ".join(
            f"{ACTION_LABEL[k]} {v}日" for k, v in sorted(counts.items())
        ),
        f"MLOS設定日          : {mlos} 日",
        f"gap night検知       : {gaps} 日",
    ]
    return "\n".join(lines)
