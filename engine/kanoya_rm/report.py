"""出力レポート生成（意思決定ログ／承認キュー／週次ミーティング資料）."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from . import capacity
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


def capacity_summary(settings, start: date, days: int, *,
                     sold_room_nights: float | None = None,
                     as_of: date | None = None) -> str:
    """稼働率の前提となる営業日数を、分母つきで示す.

    例外営業で営業日数が後から変わるため、分母を書かない稼働率は
    前回の数字とも他施設の数字とも比較できない（capacity.py 参照）。
    """
    cap = capacity.measure(settings, start, days, as_of=as_of)
    lines = [
        f"対象期間の暦日       : {cap.calendar_days} 日"
        f"（{cap.start} 〜 {cap.end}）",
        f"稼働率の分母         : {cap.denominator()}",
    ]
    if not cap.settled:
        lines.append(
            f"　　　　　　　　　　   将来の閉館日 {cap.future_closed_days} 日は"
            f"例外営業が未確定のため、発生率 {cap.exception_rate:.1%} で上限側を見込む")
    if sold_room_nights is not None:
        lines.append(f"稼働率               : {cap.occupancy_text(sold_room_nights)}")
    return "\n".join(lines)


def max_active_lead(settings, season: str, limit: int = 121) -> int:
    """そのシーズンで内部需要が有効になる最大リード日数（無ければ -1）."""
    from . import pace as pace_mod          # 遅延importで循環を避ける
    rooms = int(settings.property["property"]["rooms"])
    occupancy = pace_mod.expected_final_occupancy(settings, season)
    threshold = pace_mod._demand_config(settings)["min_expected"]
    probe = date(2027, 3, 15)               # 進捗率はシーズンに依存しない
    best = -1
    for lead in range(limit):
        ratio = pace_mod.expected_ratio(settings, probe, lead)
        if rooms * occupancy * ratio >= threshold:
            best = lead
    return best


def demand_coverage(settings, paces: dict) -> str:
    """内部需要シグナルが効いている日数と、**効かない理由**.

    min_expected_rooms は期待室数が1室に届かないリード帯を「無効化」する。
    無効化された日は z_demand = 0 となり、価格は競合・イベント・曜日季節
    だけで決まる。これは設計どおりだが、**出力を見ても分からない**。
    寄与が0円と表示されるだけで、「進捗が想定どおりだった」のか
    「そもそも見ていない」のかが区別できない。

    日数だけを出すと今度は故障と誤解される。そこで理由まで書く。
    効かないのは実装の欠陥ではなく、5室 × 稼働28% という規模の帰結である。
    最終的に見込む予約が1.4室しかない日の「進捗」は統計的に読み取れない。

    稼働が上がれば expected_final_occupancy が上がり、期待室数が増え、
    有効帯は**自動的に広がる**。閾値を下げて無理に効かせないこと
    （docs/08 のスイープ結果を参照）。
    """
    if not paces:
        return ""
    total = len(paces)
    off = sum(1 for p in paces.values() if p.below_min_expected)
    if not off:
        return ""
    from . import pace as pace_mod          # 遅延importで循環を避ける
    active = sorted(d for d, p in paces.items() if not p.below_min_expected)
    where = (f"{active[0]} 〜 {active[-1]}" if active else "なし")
    rooms = int(settings.property["property"]["rooms"])
    threshold = pace_mod._demand_config(settings)["min_expected"]

    seasons: dict[str, int] = {}
    for day in paces:
        season, _label = settings.season_of(day)
        seasons[season] = seasons.get(season, 0) + 1
    lines = [
        f"内部需要の有効日数    : {total - off} / {total} 日"
        f"（{off}日は期待室数が min_expected_rooms={threshold:g}室 未満のため不使用）",
        f"　　　　　　　　　　    有効なのは {where}。"
        f"それ以外の日は競合・イベント・曜日季節だけで価格を決めている",
        "　　　　　　　　　　    ── 効かない理由（故障ではありません） ──",
    ]
    for season, days in sorted(seasons.items(), key=lambda kv: -kv[1]):
        occupancy = pace_mod.expected_final_occupancy(settings, season)
        best = max_active_lead(settings, season)
        band = f"リード0〜{best}日" if best >= 0 else "なし"
        lines.append(
            f"　　　　　　　　　　    {season:<9}最終稼働の見込み {occupancy:.2f}"
            f" × {rooms}室 = 最終期待 {occupancy * rooms:.2f}室"
            f" → {threshold:g}室に届くのは {band}（対象{days}日）")
    lines.append(
        "　　　　　　　　　　    最終的に見込む予約が数室しかない日の『進捗』は、"
        "実OTBが整数しか取れないため読み取れません。")
    lines.append(
        "　　　　　　　　　　    **稼働が上がれば有効帯は自動的に広がります。** "
        "実績から expected_final_occupancy を再算出すると、")
    lines.append(
        "　　　　　　　　　　    期待室数が増えて閾値に届くリードが伸びます"
        "（実測カーブでは稼働40%で0〜5日、60%で0〜10日、80%で0〜15日）。")
    lines.append(
        "　　　　　　　　　　    閾値を下げて無理に効かせないこと。"
        "docs/08 のスイープ結果を参照。")
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
