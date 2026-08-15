"""チャネル別 Net ADR の算定.

掲出ADRの最適化だけでは意思決定を誤る。OTAの手数料に加え、
ポイント原資・クーポン・会員割引・タイムセールは『見えない値引き』であり、
これらを控除した Net ADR（施設手取り）で比較して初めて、
直販シフトや販促参加の可否を定量判断できる。
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings


@dataclass
class ChannelEconomics:
    channel: str
    label: str
    gross_rate: float
    total_cost_rate: float
    net_rate: float
    gap_vs_direct: float


def evaluate(settings: Settings, gross_rate: float) -> list[ChannelEconomics]:
    out: list[ChannelEconomics] = []
    channels = {k: v for k, v in settings.property["channels"].items()
                if not k.startswith("_")}
    direct = channels["direct"]
    direct_net = gross_rate * (1 - float(direct["commission"]) - float(direct.get("point_cost", 0)))

    for key, cfg in channels.items():
        cost = float(cfg["commission"]) + float(cfg.get("point_cost", 0))
        net = gross_rate * (1 - cost)
        out.append(ChannelEconomics(key, cfg["label"], gross_rate, cost, net, net - direct_net))
    return sorted(out, key=lambda c: -c.net_rate)


def direct_shift_value(settings: Settings, annual_room_revenue: float,
                       current_direct_share: float, target_direct_share: float) -> dict[str, float]:
    """直販比率のシフトによる手取り増を試算する."""
    channels = {k: v for k, v in settings.property["channels"].items()
                if not k.startswith("_")}
    ota_keys = [k for k in channels if k != "direct"]
    ota_cost = sum(float(channels[k]["commission"]) + float(channels[k].get("point_cost", 0))
                   for k in ota_keys) / len(ota_keys)
    direct_cost = float(channels["direct"]["commission"])
    shift = max(0.0, target_direct_share - current_direct_share)
    gain = annual_room_revenue * shift * (ota_cost - direct_cost)
    return {
        "avg_ota_cost_rate": ota_cost,
        "direct_cost_rate": direct_cost,
        "shift_points": shift,
        "annual_gain_yen": gain,
    }
