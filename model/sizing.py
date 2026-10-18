"""Size a trade against the session rails.

The stake is deposit + pool PnL, and the server halts at the floor and the ceiling. A trade is
sized so that (a) one filled take-profit can reach the ceiling and (b) one stop-out cannot cross the floor.
All money is integer cents, all size is lots (0.001 BTC), matching docs/model-api.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TAKER_BPS = 5.0
MAKER_BPS = 2.0
SLIPPAGE_BPS = 0.5      # entry and stop crossing allowance beyond the quoted touch


@dataclass(frozen=True)
class Plan:
    lots: int
    take_profit_bps: float
    stop_bps: float
    net_gain_cents: int       # stake change if the take-profit fills
    max_loss_cents: int       # stake change if the stop fires


def lot_notional_cents(mid_cents: int) -> float:
    return mid_cents / 1000.0


def plan_trade(
    stake_cents: int,
    floor_cents: int,
    ceiling_cents: int,
    mid_cents: int,
    room_lots: int,
    take_profit_bps: float,
    stop_bps: float,
    buffer_cents: int = 1,
) -> Plan | None:
    """Lots for one bracket trade, or None when the rails leave no room."""
    gain_bps = take_profit_bps - TAKER_BPS - MAKER_BPS - SLIPPAGE_BPS
    loss_bps = stop_bps + 2 * TAKER_BPS + 2 * SLIPPAGE_BPS
    if gain_bps <= 0 or room_lots < 1:
        return None
    per_lot = lot_notional_cents(mid_cents) / 1e4
    need = ceiling_cents - stake_cents
    margin = stake_cents - floor_cents - buffer_cents
    if need <= 0 or margin <= 0:
        return None
    by_target = math.ceil(need / (per_lot * gain_bps))
    by_floor = math.floor(margin / (per_lot * loss_bps))
    lots = min(room_lots, by_target, by_floor)
    if lots < 1:
        return None
    return Plan(
        lots=lots,
        take_profit_bps=take_profit_bps,
        stop_bps=stop_bps,
        net_gain_cents=int(lots * per_lot * gain_bps),
        max_loss_cents=int(lots * per_lot * loss_bps),
    )
