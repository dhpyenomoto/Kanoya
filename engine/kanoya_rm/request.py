"""調査リクエスト — 「いつ時点で」「どの宿泊日を」調べるかの唯一の入力口.

本モジュール以前、日付の入力は3つのスクリプトに散らばり、
同じ「本日」を指すのに --run-date と --snapshot の2つの名前があった。
対象期間にいたっては入口が無く、常に「先120日」固定だった。

ここで2つの概念を明示的に分ける。

  調査基準日 as_of   … いつ時点で見た市場か（データの取得日）
  調査対象期間 start〜end … どの宿泊日を調べたいか

この2つは独立している。「今日の時点で、11月の紅葉期だけを調べる」
「先週時点のデータで、年末年始を振り返る」はどちらも正当な要求である。

日付トークンは絶対日付と相対指定の両方を受け付ける。
設定ファイルに固定日を書くと毎日書き換えることになるため、
"today" や "+90d" を書けることが運用上は重要になる。
"""

from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

RELATIVE = re.compile(r"^([+-])\s*(\d+)\s*([dwmy])$", re.IGNORECASE)
TODAY_WORDS = {"today", "now", "本日", "今日", "きょう"}


class RequestError(ValueError):
    """入力が矛盾しており、既定値では埋められない場合に送出する."""


def resolve_token(token: str | date | None, base: date, *,
                  month_end: bool = False) -> date | None:
    """日付トークンを解決する.

    受け付ける形式:
      "today" / "本日"     基準日そのもの
      "2026-11-01"        絶対日付
      "2026-11"           月指定（month_end=True なら月末、既定は月初）
      "+90d" "+12w"       基準日からの相対（d=日 w=週 m=月 y=年）
      "-7d"               過去方向
    """
    if token is None:
        return None
    if isinstance(token, date):
        return token

    text = str(token).strip()
    if not text:
        return None
    if text.lower() in TODAY_WORDS or text in TODAY_WORDS:
        return base

    match = RELATIVE.match(text)
    if match:
        sign = -1 if match.group(1) == "-" else 1
        amount = int(match.group(2)) * sign
        unit = match.group(3).lower()
        if unit == "d":
            return base + timedelta(days=amount)
        if unit == "w":
            return base + timedelta(weeks=amount)
        if unit == "m":
            return _add_months(base, amount)
        return _add_months(base, amount * 12)

    if re.fullmatch(r"\d{4}-\d{2}", text):            # 月指定
        year, month = (int(p) for p in text.split("-"))
        day = calendar.monthrange(year, month)[1] if month_end else 1
        return date(year, month, day)

    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise RequestError(
            f"日付として解釈できません: {token!r}\n"
            f"  使える形式: 'today' / '2026-11-01' / '2026-11' / '+90d' / '+3m'"
        ) from exc


def _is_month_token(token: str | None) -> bool:
    """「2026-11」形式か（月まるごとの指定）."""
    return bool(token) and bool(re.fullmatch(r"\d{4}-\d{2}", str(token).strip()))


def _month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _add_months(base: date, months: int) -> date:
    total = base.month - 1 + months
    year = base.year + total // 12
    month = total % 12 + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


@dataclass
class SurveyRequest:
    as_of: date                  # 調査基準日（「本日」）
    start: date                  # 調査対象期間の開始（宿泊日）
    end: date                    # 調査対象期間の終了（宿泊日、当日を含む）
    adults: int = 2
    los: int = 1
    target_position: float = 1.15
    explicit_window: bool = False   # 期間が明示指定されたか（既定期間ではないか）
    warnings: list[str] = field(default_factory=list)
    source_path: Path | None = None

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def min_lead(self) -> int:
        return (self.start - self.as_of).days

    @property
    def max_lead(self) -> int:
        return (self.end - self.as_of).days

    def contains(self, stay: date) -> bool:
        return self.start <= stay <= self.end

    def describe(self) -> str:
        window = f"{self.start.isoformat()} 〜 {self.end.isoformat()}"
        return (
            f"  調査基準日（本日）: {self.as_of.isoformat()}\n"
            f"  調査対象期間      : {window}  （{self.days}泊分 / "
            f"リードタイム {self.min_lead}〜{self.max_lead}日）\n"
            f"  条件              : {self.adults}名1室 {self.los}泊 ／ "
            f"目標ポジション {self.target_position:.2f}倍"
        )


DEFAULTS = {
    "as_of": "today",
    "target": {"from": "today", "to": "+120d"},
    "conditions": {"adults": 2, "los": 1, "target_position": 1.15},
}

CONFIG_NAME = "survey_request.json"


def add_arguments(parser, *, include_window: bool = True) -> None:
    """全スクリプトで共通の日付引数を登録する（名前の揺れを防ぐ）."""
    parser.add_argument("--as-of", default=None, metavar="DATE",
                        help="調査基準日（『本日』）。today / 2026-08-15 / -7d")
    parser.add_argument("--request", default=f"config/{CONFIG_NAME}",
                        help=f"調査リクエスト設定（既定 config/{CONFIG_NAME}）")
    if include_window:
        parser.add_argument("--from", dest="start", default=None, metavar="DATE",
                            help="調査対象期間の開始（宿泊日）。today / 2026-11-01 / 2026-11 / +1m")
        parser.add_argument("--to", dest="end", default=None, metavar="DATE",
                            help="調査対象期間の終了（宿泊日）。2026-11-30 / +90d")
        parser.add_argument("--days", type=int, default=None,
                            help="開始日からの日数（--to の代わり）")
    # 旧名。同じ概念に2つの名前があった状態からの移行用。
    parser.add_argument("--run-date", dest="legacy_run_date", default=None,
                        help=argparse_suppress())
    parser.add_argument("--snapshot", dest="legacy_snapshot", default=None,
                        help=argparse_suppress())


def argparse_suppress() -> str:
    import argparse
    return argparse.SUPPRESS


def from_args(args, root: Path, *, max_horizon: int | None = None,
              today: date | None = None) -> SurveyRequest:
    """CLI引数と設定ファイルから調査リクエストを組み立てる."""
    as_of = (getattr(args, "as_of", None)
             or getattr(args, "legacy_run_date", None)
             or getattr(args, "legacy_snapshot", None))
    request = load(
        root / getattr(args, "request", f"config/{CONFIG_NAME}"),
        as_of=as_of,
        start=getattr(args, "start", None),
        end=getattr(args, "end", None),
        days=getattr(args, "days", None),
        target_position=getattr(args, "target_position", None),
        today=today,
        max_horizon=max_horizon,
    )
    if getattr(args, "legacy_run_date", None) or getattr(args, "legacy_snapshot", None):
        request.warnings.append(
            "--run-date / --snapshot は --as-of へ統合されました（当面は動作します）"
        )
    return request


def render_input_panel(request: SurveyRequest) -> str:
    """解決後の入力値を可視化する（何を調べているのかを毎回明示する）."""
    lines = ["── 調査条件 " + "─" * 65, request.describe()]
    if request.source_path:
        lines.append(f"  入力元            : {request.source_path.name}"
                     f"（CLI引数があればそちらが優先）")
    for warning in request.warnings:
        lines.append(f"  ⚠ {warning}")
    return "\n".join(lines)


def load(path: str | Path | None = None, *,
         as_of: str | None = None,
         start: str | None = None,
         end: str | None = None,
         days: int | None = None,
         target_position: float | None = None,
         today: date | None = None,
         max_horizon: int | None = None) -> SurveyRequest:
    """設定ファイル＋CLI引数から調査リクエストを組み立てて検証する.

    優先順位は CLI引数 > 設定ファイル > 既定値。
    """
    config = dict(DEFAULTS)
    source_path = None
    if path is not None and Path(path).exists():
        source_path = Path(path)
        loaded = json.loads(source_path.read_text(encoding="utf-8"))
        config = {**DEFAULTS, **{k: v for k, v in loaded.items() if not k.startswith("_")}}

    base = today or date.today()
    warnings: list[str] = []

    resolved_as_of = resolve_token(as_of or config.get("as_of"), base) or base

    target = config.get("target") or {}
    cfg_start, cfg_end = target.get("from"), target.get("to")
    explicit = any(v is not None for v in (start, end, days))

    start_token = start if start is not None else cfg_start
    resolved_start = resolve_token(start_token, resolved_as_of) or resolved_as_of

    # 終了日の決定順序。CLIの指定は設定ファイルより常に優先する。
    # 「--from 2026-11」だけを指定したときに設定ファイルの to="+120d" が
    # 勝ってしまい、11月まるごとにならない不具合を避けるため、
    # 月ショートハンドは同じ優先度の end/days より弱く、下位の設定より強い。
    if end is not None:
        resolved_end = resolve_token(end, resolved_as_of, month_end=True)
    elif days is not None:
        resolved_end = resolved_start + timedelta(days=days - 1)
    elif _is_month_token(start):
        resolved_end = _month_end(resolved_start)
    elif cfg_end is not None:
        resolved_end = resolve_token(cfg_end, resolved_as_of, month_end=True)
    elif _is_month_token(cfg_start):
        resolved_end = _month_end(resolved_start)
    else:
        resolved_end = resolved_start + timedelta(days=119)

    if resolved_end is None:
        resolved_end = resolved_start + timedelta(days=119)

    # ---- 検証 ----
    if resolved_end < resolved_start:
        raise RequestError(
            f"調査対象期間が逆転しています: {resolved_start} 〜 {resolved_end}\n"
            f"  終了日は開始日以降を指定してください。"
        )

    if resolved_start < resolved_as_of:
        # 過去日は販売できない。黙って含めると「判断不可」の山になるだけ。
        clipped = resolved_as_of
        warnings.append(
            f"開始日 {resolved_start.isoformat()} が基準日より過去のため "
            f"{clipped.isoformat()} へ切り上げました（過去日は販売できません）"
        )
        resolved_start = clipped
        if resolved_end < resolved_start:
            raise RequestError(
                f"調査対象期間が全て基準日より過去です"
                f"（基準日 {resolved_as_of.isoformat()}）。"
            )

    if resolved_as_of > base:
        warnings.append(
            f"調査基準日 {resolved_as_of.isoformat()} が実際の今日より未来です"
            f"（再現・検証用途以外では意図しない設定の可能性）"
        )

    conditions = config.get("conditions") or {}
    request = SurveyRequest(
        as_of=resolved_as_of,
        start=resolved_start,
        end=resolved_end,
        adults=int(conditions.get("adults", 2)),
        los=int(conditions.get("los", 1)),
        target_position=float(
            target_position if target_position is not None
            else conditions.get("target_position", 1.15)
        ),
        explicit_window=explicit or bool(target.get("from") or target.get("to")),
        warnings=warnings,
        source_path=source_path,
    )

    if max_horizon is not None and request.max_lead > max_horizon:
        request.warnings.append(
            f"対象期間の末尾がリードタイム {request.max_lead}日 で、"
            f"収集ホライズン {max_horizon}日 を超えています。"
            f"超過分は収集対象外のためデータが存在しません "
            f"（sources.json > collection.horizon_days を延ばすか、期間を狭めてください）"
        )

    return request
