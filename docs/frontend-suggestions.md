# Optional page changes

The paper bot does not need any of these. It trades through the model API while the page keeps using the client API. No frontend file was changed.

The live session payload on `WS /v1/watch` already carries the fields below. The trading popup still has a placeholder where the graph would go, and the setup chart is still seeded history.

- Draw the stake curve from `session.stakeHistory`. Replace the whole list on each snapshot. The timestamps on those points are the ones to show.
- Show `midCents`, `priceSource`, and `feed`. `tape` means the BTC recording. `binance`, `jupiter`, or `coinbase` means the public SOL mid. `feed: "stale"` means that mid is being held, not that the session halted, and not that a new price was invented.
- Show `bids` and `asks` best first, `openOrders`, and `fills`. Replace the fill list from the snapshot instead of appending.
- Keep the demo label that fills are fake. The bot never sends an order to an exchange.

`GET /v1/instruments` remains the picker. Symbols that are not in that response are not tradable.
