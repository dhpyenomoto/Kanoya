"""提示価格の日次記録 — 「その日その宿泊日をいくらで出していたか」.

**なぜ要るか**

実績データには成約価格しか残っていない。いくらで出して売れなかったのかが
分からないため、値付けの良否が測れない。実際にこれで損をしている:
2026年6〜7月に意図的な値上げ実験が行われたが、提示価格が残っていないため
定量評価ができなかった。リードタイム構造から「早期54,910円で9件、
直前37,824円で20件」と推定するのが限界だった。

もう一つ、承認区分が機能しない。delta_pct は推奨と現行掲出価格の比なので、
current_public_rate が0だと全日が AUTO_APPLY になる。実データ検証で
自動配信100%と出たのは較正が良いからではなく、比べる相手が無いからである。

**なぜ変更履歴（差分）として持つか**

5室 × 4形態 × 120日を毎日全部書くと年17万行になる。しかも実際には
ほとんどの日は前日と同じ値であり、同じ値を毎日書き写す運用は続かない。
そこで **値が変わったときだけ1行足す** 形にする。ある行は、より新しい行に
上書きされるまで有効であり続ける。

その代償として「記録していない」と「変わっていない」が区別できなくなる。
これは最新行の日付で見る（latest_entry_date）。何日も止まっていれば
それは運用が止まっているのであって、価格が動いていないのではない。

**単位**

posted_rate は **その商品形態の販売価格**（1室2名1泊・税サ込）。
エンジンの current_public_rate は部屋代なので、食事付きの形態で
記録されていれば食事加算を引いて部屋代へ戻す（P15 の単位変更）。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import parse_date

COLUMNS = ("stay_date", "snapshot_date", "product_type",
           "posted_rate", "channel", "note")

# 部屋代へ戻すときの優先順。素泊まりがあればそれが一番素直（加算ゼロ）。
FORM_PREFERENCE = ("room_only", "breakfast", "dinner", "two_meals")


@dataclass(frozen=True)
class Entry:
    stay_date: date
    snapshot_date: date
    product_type: str
    posted_rate: float
    channel: str = ""
    note: str = ""

    def as_row(self) -> dict[str, str]:
        return {
            "stay_date": self.stay_date.isoformat(),
            "snapshot_date": self.snapshot_date.isoformat(),
            "product_type": self.product_type,
            "posted_rate": f"{self.posted_rate:.0f}",
            "channel": self.channel,
            "note": self.note,
        }


def read(path: Path) -> list[Entry]:
    """記録を読む。無ければ空（新規導入時はこれが正常）."""
    if not path.exists():
        return []
    out: list[Entry] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if not (row.get("stay_date") or "").strip():
                continue
            try:
                out.append(Entry(
                    stay_date=parse_date(row["stay_date"]),
                    snapshot_date=parse_date(row["snapshot_date"]),
                    product_type=(row.get("product_type") or "").strip(),
                    posted_rate=float(row.get("posted_rate") or 0),
                    channel=(row.get("channel") or "").strip(),
                    note=(row.get("note") or "").strip(),
                ))
            except (KeyError, ValueError):
                continue        # 壊れた行で運用を止めない。読める行だけ使う
    return out


def write(path: Path, entries: list[Entry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(entries, key=lambda e: (e.snapshot_date, e.stay_date,
                                             e.product_type, e.channel))
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(COLUMNS))
        writer.writeheader()
        for entry in ordered:
            writer.writerow(entry.as_row())


def append(path: Path, entries: list[Entry]) -> None:
    """既存の記録に足す（履歴なので上書きしない）."""
    if not entries:
        return
    write(path, read(path) + list(entries))


def latest_entry_date(entries: list[Entry]) -> date | None:
    """最後に記録した日。運用が止まっていないかを見るために使う."""
    return max((e.snapshot_date for e in entries), default=None)


def posted_as_of(entries: list[Entry], stay: date, as_of: date, *,
                 channel: str = "") -> dict[str, float]:
    """基準日時点で有効な提示価格（商品形態 → 販売価格）.

    変更履歴なので、as_of 以前で最も新しい行がその時点の値になる。
    channel を指定すると、その販路の記録だけを見る。
    """
    best: dict[str, tuple[date, float]] = {}
    for entry in entries:
        if entry.stay_date != stay or entry.snapshot_date > as_of:
            continue
        if channel and entry.channel and entry.channel != channel:
            continue
        if entry.posted_rate <= 0:
            continue
        current = best.get(entry.product_type)
        if current is None or entry.snapshot_date >= current[0]:
            best[entry.product_type] = (entry.snapshot_date, entry.posted_rate)
    return {form: rate for form, (_taken, rate) in best.items()}


def room_rate_as_of(entries: list[Entry], stay: date, as_of: date, products,
                    *, channel: str = "") -> float:
    """基準日時点の提示価格を**部屋代**へ戻す（0なら記録なし）.

    エンジンが比べる current_public_rate は部屋代（1室2名1泊・食事抜き）。
    記録が食事付きの形態しかない場合は、その形態の食事加算を引いて戻す。
    """
    posted = posted_as_of(entries, stay, as_of, channel=channel)
    if not posted:
        return 0.0
    for form in FORM_PREFERENCE:
        if form in posted:
            return max(0.0, posted[form] - products.meal_add(form))
    # 設定に無い形態名しか無い場合は、加算を引けないのでそのまま返す。
    # 勝手に推測して引くと、部屋代が実際より安く見え、値下げ方向へ働く。
    return max(posted.values())


@dataclass
class Coverage:
    """提示価格の記録がどれだけ埋まっているか."""

    covered: int
    total: int
    latest: date | None
    stale_days: int | None

    @property
    def missing(self) -> int:
        return self.total - self.covered


def coverage(entries: list[Entry], days: list[date], as_of: date, products,
             *, channel: str = "") -> Coverage:
    covered = sum(1 for day in days
                  if room_rate_as_of(entries, day, as_of, products,
                                     channel=channel) > 0)
    latest = latest_entry_date([e for e in entries if e.snapshot_date <= as_of])
    stale = (as_of - latest).days if latest else None
    return Coverage(covered=covered, total=len(days), latest=latest,
                    stale_days=stale)
