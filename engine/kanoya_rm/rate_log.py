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

**「行が無い日」の曖昧さと、実行記録**

変更履歴だけを持つと、行が無い日が2つのことを同時に意味してしまう。

  (a) 人が確認したうえで「変わっていない」
  (b) 誰も見ていない（実行していない）

(b) を (a) と読むと、**確認していない日に「価格を据え置いた」という
事実でない記録が残る**。しかもその誤りは、あとから区別する手がかりが無い。

そこで実行そのものを別ファイル（rate_log_runs.csv）に残す。1行は
「この日に、この宿泊日範囲を確認した」という意味を持つ。価格が変わったか
どうかとは独立で、変更が無くても1行増える。

解決のときは**実行記録に裏付けられた値だけ**を使う。裏付けの無い期間は
「記録あり」として扱わず、据え置きも仮定しない。分からないことを
分かっているように見せるほうが、値が無いことより危ない。

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
RUN_COLUMNS = ("run_date", "stay_from", "stay_to", "product_type",
               "channel", "note")

# rate_log_runs.csv の冒頭に必ず書き出す注記。
# CSVを直接開いた人が最初に読む位置に置く。docs だけに書いても、
# 表計算ソフトでファイルを開いた人には届かない。
# append_run はファイルを書き直すので、ここで毎回書き戻している。
RUNS_NOTE = (
    "# 実行記録（rate_log_runs.csv）",
    "# この記録が保証しているのは「担当者がスクリプトを実行し、変更なしと申告した」",
    "# 事実である。OTA管理画面との突合を保証するものではない。",
    "# サイトコントローラー連携が入るまで、この記録の信頼性は担当者の運用に依存する。",
    "# 連携後は申告ベースから実測ベースへ移行でき、その時点でこの注記は不要になる。",
)

# 最後に確認してからこの日数を超えたら、その値はもう裏付けが無いとみなす。
# 設定（sources.json の rate_log.max_unconfirmed_days）で上書きできる。
DEFAULT_MAX_UNCONFIRMED_DAYS = 3

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


def _rows(path: Path):
    """CSVを読む。先頭の # 行は注記として読み飛ばす.

    注記をファイル冒頭に置くため。DictReader にそのまま渡すと、注記行を
    ヘッダとして読んでしまい、以降の全行が静かに捨てられる。
    """
    with path.open(encoding="utf-8", newline="") as fh:
        lines = [line for line in fh if not line.lstrip().startswith("#")]
    return csv.DictReader(lines)


@dataclass(frozen=True)
class Run:
    """「この日に、この宿泊日範囲を確認した」という記録.

    価格が変わったかどうかとは独立している。変更が無くても1行増える。
    product_type / channel が空なら「すべて」を意味する。
    """

    run_date: date
    stay_from: date
    stay_to: date
    product_type: str = ""
    channel: str = ""
    note: str = ""

    def covers(self, stay: date, *, product_type: str = "",
               channel: str = "") -> bool:
        if not (self.stay_from <= stay <= self.stay_to):
            return False
        if self.product_type and product_type and self.product_type != product_type:
            return False
        if self.channel and channel and self.channel != channel:
            return False
        return True

    def as_row(self) -> dict[str, str]:
        return {
            "run_date": self.run_date.isoformat(),
            "stay_from": self.stay_from.isoformat(),
            "stay_to": self.stay_to.isoformat(),
            "product_type": self.product_type,
            "channel": self.channel,
            "note": self.note,
        }


def runs_path_for(log_path: Path) -> Path:
    """価格の記録に対応する実行記録の場所（同じディレクトリに置く）."""
    return log_path.with_name(log_path.stem + "_runs.csv")


def read_runs(path: Path) -> list[Run]:
    if not path.exists():
        return []
    out: list[Run] = []
    for row in _rows(path):
        if not (row.get("run_date") or "").strip():
            continue
        try:
            out.append(Run(
                run_date=parse_date(row["run_date"]),
                stay_from=parse_date(row["stay_from"]),
                stay_to=parse_date(row["stay_to"]),
                product_type=(row.get("product_type") or "").strip(),
                channel=(row.get("channel") or "").strip(),
                note=(row.get("note") or "").strip(),
            ))
        except (KeyError, ValueError):
            continue            # 壊れた行で運用を止めない
    return out


def write_runs(path: Path, runs: list[Run]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(runs, key=lambda r: (r.run_date, r.stay_from, r.stay_to))
    with path.open("w", encoding="utf-8", newline="") as fh:
        for line in RUNS_NOTE:          # 注記はヘッダ行の直前に置く
            fh.write(line + "\n")
        writer = csv.DictWriter(fh, fieldnames=list(RUN_COLUMNS))
        writer.writeheader()
        for run in ordered:
            writer.writerow(run.as_row())


def append_run(path: Path, run: Run) -> None:
    write_runs(path, read_runs(path) + [run])


def last_confirmed(runs: list[Run], stay: date, as_of: date, *,
                   product_type: str = "", channel: str = "") -> date | None:
    """その宿泊日を最後に確認した実行日（無ければ None）."""
    dates = [r.run_date for r in runs
             if r.run_date <= as_of
             and r.covers(stay, product_type=product_type, channel=channel)]
    return max(dates, default=None)


def unconfirmed_days(runs: list[Run], as_of: date) -> int | None:
    """最後の実行から何日空いているか（一度も実行していなければ None）."""
    dates = [r.run_date for r in runs if r.run_date <= as_of]
    if not dates:
        return None
    return (as_of - max(dates)).days


def read(path: Path) -> list[Entry]:
    """記録を読む。無ければ空（新規導入時はこれが正常）."""
    if not path.exists():
        return []
    out: list[Entry] = []
    for row in _rows(path):
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
            continue            # 壊れた行で運用を止めない。読める行だけ使う
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


def confirmed_as_of(entries: list[Entry], runs: list[Run], stay: date,
                    as_of: date, *, channel: str = "",
                    max_unconfirmed_days: int | None = None
                    ) -> tuple[dict[str, float], date | None]:
    """**実行記録に裏付けられた**提示価格と、その裏付け日を返す.

    裏付けが無ければ空を返す。値を返さないほうが、確認していない値を
    「据え置き」として返すより安全である。前者は「分からない」と分かるが、
    後者は間違った値が正しい顔で通ってしまう。

    max_unconfirmed_days に負値を渡すと裏付けを問わず、記録をそのまま
    信用する。実行記録を導入する前のデータを読むための移行用であって、
    常用するものではない（「誰も見ていない日」が「据え置き」に化ける）。
    """
    limit = (DEFAULT_MAX_UNCONFIRMED_DAYS if max_unconfirmed_days is None
             else max_unconfirmed_days)
    if limit < 0:
        return posted_as_of(entries, stay, as_of, channel=channel), None
    confirmed = last_confirmed(runs, stay, as_of, channel=channel)
    if confirmed is None:
        return {}, None                 # 一度も確認していない
    if (as_of - confirmed).days > limit:
        return {}, confirmed            # 未実行の期間。据え置きを仮定しない
    return posted_as_of(entries, stay, confirmed, channel=channel), confirmed


def room_rate_as_of(entries: list[Entry], stay: date, as_of: date, products,
                    *, channel: str = "", runs: list[Run] | None = None,
                    max_unconfirmed_days: int | None = None) -> float:
    """基準日時点の提示価格を**部屋代**へ戻す（0なら記録なし）.

    エンジンが比べる current_public_rate は部屋代（1室2名1泊・食事抜き）。
    記録が食事付きの形態しかない場合は、その形態の食事加算を引いて戻す。

    runs を渡すと、実行記録に裏付けられた値だけを返す。裏付けの無い
    期間は0（記録なし扱い）になる。据え置きを仮定しないため。
    """
    posted, _confirmed = confirmed_as_of(
        entries, runs or [], stay, as_of, channel=channel,
        max_unconfirmed_days=max_unconfirmed_days)
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
    """提示価格の記録がどれだけ埋まっているか.

    unconfirmed は「記録はあるが、実行記録の裏付けが無いので使わない日」。
    never_recorded は「そもそも一度も記録されていない日」。
    この2つを足したものが missing になる。混ぜると、運用が止まっているのか
    まだ始まっていないのかが読めない。
    """

    covered: int
    total: int
    latest: date | None
    stale_days: int | None
    unconfirmed: int = 0
    never_recorded: int = 0
    last_run: date | None = None
    days_since_run: int | None = None

    @property
    def missing(self) -> int:
        return self.total - self.covered


def coverage(entries: list[Entry], days: list[date], as_of: date, products,
             *, channel: str = "", runs: list[Run] | None = None,
             max_unconfirmed_days: int | None = None) -> Coverage:
    runs = runs or []
    covered = unconfirmed = never = 0
    for day in days:
        rate = room_rate_as_of(entries, day, as_of, products, channel=channel,
                               runs=runs,
                               max_unconfirmed_days=max_unconfirmed_days)
        if rate > 0:
            covered += 1
            continue
        raw = posted_as_of(entries, day, as_of, channel=channel)
        if raw:
            unconfirmed += 1      # 値はあるが裏付けが無い
        else:
            never += 1            # 一度も記録されていない
    latest = latest_entry_date([e for e in entries if e.snapshot_date <= as_of])
    stale = (as_of - latest).days if latest else None
    run_dates = [r.run_date for r in runs if r.run_date <= as_of]
    last_run = max(run_dates, default=None)
    return Coverage(covered=covered, total=len(days), latest=latest,
                    stale_days=stale, unconfirmed=unconfirmed,
                    never_recorded=never, last_run=last_run,
                    days_since_run=(as_of - last_run).days if last_run else None)
