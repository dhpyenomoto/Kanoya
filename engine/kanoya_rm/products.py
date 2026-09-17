"""商品形態 — 部屋代から4形態の販売価格を導く.

**なぜ部屋代を最適化単位にするのか（2026-09 の設計変更）**

以前は「1室2名1泊2食」の総額を最適化していた。しかし実売の内訳は

    素泊まり 55% ／ 朝食のみ 31% ／ 2食付き 12% ／ 夕食のみ 2%

であり、総額基準の出力は実売の12%にしか当たらない。実測でも
エンジン推奨の中央値75,000円に対し、2食付きの実績中央値は80,340円
（差7%以内・較正は妥当）だが、素泊まりの実績中央値は41,300円で
1.8倍の乖離があった。較正が悪いのではなく、単位が商品と噛み合っていない。

加えて、OTAの管理画面に打ち込むのは部屋代である。総額を出されても
現場ではそのまま使えない。

そこで最適化対象を「1室2名1泊の部屋代」に変え、食事は固定加算として扱う。
食事単価は原価にほぼ固定され、レベニューマネジメントでは動かないためである。

**競合との比較について**

競合価格は依然「1室2名1泊2食・税サ込」へ正規化されている（NAR）。
自社が部屋代基準になったので、突き合わせるときは必ず
two_meal_total() で2食付き相当へ戻すこと。素の部屋代とNARを直接比べると、
食事2名分（44,000円）のぶんだけ自社が安く見える。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 実売の多い順。出力もこの順で並べる。
FORMS = ("room_only", "breakfast", "dinner", "two_meals")
LABELS = {
    "room_only": "素泊まり",
    "breakfast": "朝食のみ",
    "dinner": "夕食のみ",
    "two_meals": "2食付き",
}

DEFAULT_OCCUPANCY = 2


@dataclass(frozen=True)
class Floor:
    value: float
    provisional: bool
    note: str = ""


@dataclass
class ProductPricing:
    """部屋代 → 4形態の販売価格、および形態別フロア."""

    room_per_person: float
    dinner_per_person: float
    breakfast_per_person: float
    occupancy: int = DEFAULT_OCCUPANCY
    floors: dict[str, Floor] = field(default_factory=dict)
    ceiling: float = 0.0
    legacy_two_meal_anchor: float = 0.0
    anchor_room_rate: float = 0.0

    # ---- 食事加算（1室あたり） ------------------------------------

    def meal_add(self, form: str) -> float:
        per = 0.0
        if form in ("dinner", "two_meals"):
            per += self.dinner_per_person
        if form in ("breakfast", "two_meals"):
            per += self.breakfast_per_person
        return per * self.occupancy

    @property
    def all_meals(self) -> float:
        return self.meal_add("two_meals")

    # ---- 部屋代 → 販売価格 ----------------------------------------

    def price(self, form: str, room_rate: float) -> float:
        return room_rate + self.meal_add(form)

    def prices(self, room_rate: float) -> dict[str, float]:
        return {f: self.price(f, room_rate) for f in FORMS}

    def two_meal_total(self, room_rate: float) -> float:
        """競合NAR（1室2名2食）と突き合わせるための換算."""
        return self.price("two_meals", room_rate)

    # ---- フロア ----------------------------------------------------

    def room_floor(self) -> tuple[float, str, bool]:
        """全形態が自分のフロアを満たすために必要な部屋代の下限.

        形態ごとにフロアがあるが、最適化するのは部屋代1本なので、
        「どの形態でもフロアを割らない」部屋代を採る。
        つまり各形態の (フロア − 食事加算) の最大値。

        暫定値の扱いに注意が要る。現在の暫定フロアは「2食付きフロア −
        含まれない食事の売価」で置いてあるため、4形態すべてが同じ部屋代下限
        （14,000円）に帰着する。同値のときに確定値の形態を返すと、
        暫定フロアが等しく効いている事実が出力から消える。
        そのため同値なら暫定側を優先して返す。

        返り値: (下限, 拘束している形態, その形態が暫定値か)
        """
        if not self.floors:
            return 0.0, "", False
        # (下限, 暫定か) の順で最大を採る → 同値なら暫定側が勝つ
        needs = [(self.floors[f].value - self.meal_add(f), self.floors[f].provisional, f)
                 for f in self.floors]
        value, provisional, form = max(needs)
        return max(0.0, value), form, provisional

    def binding_forms(self) -> list[str]:
        """部屋代下限を決めている形態（同値なら複数）."""
        if not self.floors:
            return []
        needs = {f: self.floors[f].value - self.meal_add(f) for f in self.floors}
        top = max(needs.values())
        return [f for f in FORMS if f in needs and abs(needs[f] - top) < 1e-9]

    def provisional_forms(self) -> list[str]:
        return [f for f in FORMS if f in self.floors and self.floors[f].provisional]

    # ---- 整合性 ----------------------------------------------------

    def anchor_consistency(self) -> tuple[bool, float, float]:
        """部屋代アンカー + 食事加算 が、移行前の総額アンカーと一致するか.

        返り値: (±5%以内か, 換算値, 参照値)
        """
        if not self.legacy_two_meal_anchor:
            return True, 0.0, 0.0
        converted = self.anchor_room_rate + self.all_meals
        ok = abs(converted / self.legacy_two_meal_anchor - 1.0) <= 0.05
        return ok, converted, self.legacy_two_meal_anchor


def load(settings) -> ProductPricing:
    prop = settings.property
    occupancy = int(prop["property"].get("standard_occupancy", DEFAULT_OCCUPANCY)) or DEFAULT_OCCUPANCY
    comps = prop.get("rate_components") or {}
    guards = prop.get("guardrails", {})
    base = prop.get("base", {})

    anchor = float(base.get("anchor_room_rate", 0.0))
    room = comps.get("room_per_person")
    if room is None:
        room = anchor / occupancy if occupancy else 0.0

    floors: dict[str, Floor] = {}
    raw = guards.get("floors")
    if isinstance(raw, dict):
        for form, spec in raw.items():
            if form not in FORMS:
                continue
            if isinstance(spec, dict):
                floors[form] = Floor(float(spec.get("value", 0.0)),
                                     bool(spec.get("provisional", False)),
                                     str(spec.get("note", "")))
            else:
                floors[form] = Floor(float(spec), False)

    return ProductPricing(
        room_per_person=float(room),
        dinner_per_person=float(comps.get("dinner_per_person", 0.0)),
        breakfast_per_person=float(comps.get("breakfast_per_person", 0.0)),
        occupancy=occupancy,
        floors=floors,
        ceiling=float(guards.get("ceiling_room_rate", 0.0)),
        legacy_two_meal_anchor=float(base.get("legacy_two_meal_anchor", 0.0)),
        anchor_room_rate=anchor,
    )


def warnings_for(pricing: ProductPricing) -> list[str]:
    """起動時に出す警告（設定の取り違えを黙って通さないため）."""
    out: list[str] = []

    provisional = pricing.provisional_forms()
    if provisional:
        names = "・".join(LABELS[f] for f in provisional)
        out.append(
            f"暫定フロアが設定されています（{names}）。\n"
            f"  これらは『2食付きフロア − 含まれない食事の売価』を引いただけの仮値で、\n"
            f"  食事に乗った利益ぶん過小です。貢献利益フロアとしては正しくありません。\n"
            f"  変動費の内訳（食材原価・リネン・清掃・アメニティ・OTA手数料率）から\n"
            f"  再算出してください。仮値のまま本番配信に使わないこと。")

    ok, converted, legacy = pricing.anchor_consistency()
    if not ok:
        out.append(
            f"部屋代アンカーと移行前の総額アンカーが整合しません。\n"
            f"  部屋代 {pricing.anchor_room_rate:,.0f} + 食事 {pricing.all_meals:,.0f}"
            f" = {converted:,.0f} 円 ／ 参照値 {legacy:,.0f} 円"
            f"（乖離 {converted / legacy - 1:+.1%}・許容 ±5%）\n"
            f"  rate_components か base.anchor_room_rate のどちらかが古い可能性があります。")

    if pricing.anchor_room_rate and pricing.room_per_person:
        expected = pricing.room_per_person * pricing.occupancy
        if abs(expected - pricing.anchor_room_rate) > 1.0:
            out.append(
                f"rate_components.room_per_person × {pricing.occupancy} "
                f"({expected:,.0f}) が base.anchor_room_rate "
                f"({pricing.anchor_room_rate:,.0f}) と一致しません。")
    return out
