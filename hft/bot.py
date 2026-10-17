"""Join-the-touch baseline for Binance USDS-M BTCUSDT.

Posts one post-only bid at the best bid and one post-only ask at the best ask.
Size stays at one minimum lot so the no-impact replay assumption stays plausible.
"""

import numpy as np
from numba import njit

from hftbacktest import BUY, GTX, LIMIT, SELL

ORDER_QTY = 0.001
MAX_POSITION = 0.01
STEP_NS = 100_000_000


@njit
def join_the_touch(hbt, recorder):
    asset_no = 0
    tick_size = hbt.depth(asset_no).tick_size

    while hbt.elapse(STEP_NS) == 0:
        hbt.clear_inactive_orders(asset_no)

        depth = hbt.depth(asset_no)
        position = hbt.position(asset_no)
        orders = hbt.orders(asset_no)
        best_bid = depth.best_bid
        best_ask = depth.best_ask

        book_ready = (
            np.isfinite(best_bid)
            and np.isfinite(best_ask)
            and best_bid > 0.0
            and best_ask > best_bid
        )
        if book_ready:
            bid_tick = np.uint64(np.round(best_bid / tick_size))
            ask_tick = np.uint64(np.round(best_ask / tick_size))

            values = orders.values()
            while values.has_next():
                order = values.get()
                if not order.cancellable:
                    continue
                if order.side == BUY and (order.order_id != bid_tick or position >= MAX_POSITION):
                    hbt.cancel(asset_no, order.order_id, False)
                elif order.side == SELL and (order.order_id != ask_tick or position <= -MAX_POSITION):
                    hbt.cancel(asset_no, order.order_id, False)

            if position < MAX_POSITION and bid_tick not in orders:
                hbt.submit_buy_order(
                    asset_no, bid_tick, best_bid, ORDER_QTY, GTX, LIMIT, False
                )
            if position > -MAX_POSITION and ask_tick not in orders:
                hbt.submit_sell_order(
                    asset_no, ask_tick, best_ask, ORDER_QTY, GTX, LIMIT, False
                )

        recorder.record(hbt)

    return True
