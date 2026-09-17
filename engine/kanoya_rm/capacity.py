"""営業日数と室夜数 — 稼働率・RevPAR の分母.

**なぜ分母を必ず書くのか**

鹿のやの定休日は固定ではない。火曜・水曜を閉館日としているが、需要に応じた
例外営業が通年で発生する（2026年2〜9月の実績で閉館日64日のうち8日・12.5%）。
つまり同じ期間でも、例外営業が1日増えれば営業日数が変わり、稼働率の分母が
変わる。分母を書かない稼働率は、前回の数字とも他施設の数字とも比較できない。

  暦日基準   224日 × 5室 = 1,120室夜 → 稼働 21.2%
  曜日ルール 160日 × 5室 =   800室夜 → 稼働 29.6%
  実績       168日 × 5室 =   840室夜 → 稼働 28.2%

同じ実売室夜を指しているのに3つの数字が出る。どれが正しいかではなく、
どの分母で割ったかを書かなければ意味が定まらない、というのが要点である。

**将来期間を単一値で出さない理由**

将来の例外営業はまだ決まっていない。曜日ルールだけで数えた営業日数は
**下限**であり、実際にはそこから増える。単一値で出すと、あとから例外営業が
入るたびに過去に報告した稼働率が静かに古くなる。そのため将来を含む期間は
幅で出す。上限は closed_days.exception_rate（既定0.125・実績由来）で見積もる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Capacity:
    """ある期間の営業日数と室夜数.

    open_days は確定分（曜日ルール＋登録済みの例外営業）。
    open_days_max は、まだ決まっていない将来の例外営業を見込んだ上限。
    期間がすべて過去なら両者は一致する（settled が True）。
    """

    start: date
    end: date
    rooms: int
    calendar_days: int
    open_days: int
    open_days_max: int
    future_closed_days: int     # 例外営業が入りうる将来の閉館日
    exception_rate: float

    @property
    def settled(self) -> bool:
        """営業日数が確定しているか（将来の閉館日が無い）."""
        return self.open_days_max == self.open_days

    @property
    def room_nights(self) -> int:
        return self.open_days * self.rooms

    @property
    def room_nights_max(self) -> int:
        return self.open_days_max * self.rooms

    def denominator(self) -> str:
        """分母を明記した1行。稼働率・RevPAR には必ずこれを添える."""
        if self.settled:
            return (f"営業{self.open_days}日 × {self.rooms}室 "
                    f"= {self.room_nights:,}室夜")
        return (f"営業{self.open_days}〜{self.open_days_max}日 × {self.rooms}室 "
                f"= {self.room_nights:,}〜{self.room_nights_max:,}室夜")

    def occupancy(self, sold_room_nights: float) -> tuple[float, float]:
        """稼働率を (下限, 上限) で返す.

        営業日数が増えるほど分母が大きくなるので、営業日数の上限が
        稼働率の**下限**を与える。確定期間では両者が一致する。
        """
        if self.room_nights <= 0:
            return 0.0, 0.0
        high = sold_room_nights / self.room_nights
        low = sold_room_nights / self.room_nights_max
        return low, high

    def occupancy_text(self, sold_room_nights: float) -> str:
        low, high = self.occupancy(sold_room_nights)
        value = f"{high:.1%}" if self.settled else f"{low:.1%}〜{high:.1%}"
        return (f"{value}（実売 {sold_room_nights:,.0f}室夜 ÷ {self.denominator()}）")

    def revpar(self, room_revenue: float) -> tuple[float, float]:
        if self.room_nights <= 0:
            return 0.0, 0.0
        return room_revenue / self.room_nights_max, room_revenue / self.room_nights

    def revpar_text(self, room_revenue: float) -> str:
        low, high = self.revpar(room_revenue)
        value = f"{high:,.0f}円" if self.settled else f"{low:,.0f}〜{high:,.0f}円"
        return f"{value}（部屋代収入 {room_revenue:,.0f}円 ÷ {self.denominator()}）"


def measure(settings, start: date, days: int, *,
            as_of: date | None = None) -> Capacity:
    """start から days 日分の営業日数を数える.

    as_of より後の閉館日は「まだ例外営業が決まっていない日」として、
    exception_rate ぶんだけ上限側へ積む。as_of を渡さなければ全期間を
    確定扱いにする（実績の集計はこちら）。
    """
    rooms = int(settings.property["property"]["rooms"])
    rate = settings.exception_rate()
    span = [start + timedelta(days=i) for i in range(max(0, days))]
    open_count = sum(1 for d in span if settings.is_open(d))
    future_closed = 0
    if as_of is not None:
        # 曜日ルールで閉館、かつ例外営業として登録されていない将来の日だけが
        # 「これから開くかもしれない日」。登録済みの日はすでに open_count にある。
        future_closed = sum(1 for d in span
                            if d > as_of and settings.is_closed(d)
                            and settings.closed_by_weekday(d))
    return Capacity(
        start=span[0] if span else start,
        end=span[-1] if span else start,
        rooms=rooms,
        calendar_days=len(span),
        open_days=open_count,
        open_days_max=open_count + round(future_closed * rate),
        future_closed_days=future_closed,
        exception_rate=rate,
    )
