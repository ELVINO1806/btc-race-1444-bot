
"""SMA30 dip bot for cbBTC/USDC, 1-minute bars (agent-fun/bars-replay/v1).


Strategy, exactly as specified:
- Track the simple moving average of the last 30 closes (SMA30).
  Trade only once at least 30 closes have been seen.
- ENTRY (flat, cash > 0): latest close < 98.8% of SMA30 -> buy with
  FULL cash (100% of equity) at the next open, max_slippage_bps 100.
- EXIT (holding): sell the whole position when the latest close is
  >= entry_price * 1.015 (take profit +1.5%) or <= entry_price * 0.98
  (stop loss -2%). Re-entry is allowed after an exit.
- entry_price is the actual fill price reported for the buy in the
  following batch, not the close the decision was made on.
- Long only, at most 1 open position, at most 1 order per reply.
- Never order on the last batch (orders there are rejected) or
  during warm-up. Deterministic, standard library only, no clock,
  no randomness, no network, no files outside the repo.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

SYMBOL = "CBBTC"
SMA_WINDOW = 30
TOTAL_BARS = 1440  # both the dev window and the race window are 1440 bars

# Entry: close < 988/1000 * SMA30.
# Exit:  close >= 1015/1000 * entry_price (TP) or close <= 98/100 * entry_price (SL).
# All comparisons are done in exact integer arithmetic so that the
# result is identical on every run.

closes = []  # closes of CBBTC, in arrival order (price-scale integers)
entry_price = None  # fill price of the open buy, or None when flat


def _holding(state):
    holding = state.positions.get(SYMBOL)
    if holding is None or holding.qty <= 0:
        return None
    return holding


def _fallback_entry_price(state, holding):
    """Entry price implied by the position's cost, used only if the buy
    fill was somehow never seen (it normally always is)."""
    symbol_info = state.symbols.get(SYMBOL)
    if symbol_info is None or holding.qty <= 0:
        return None
    quote_scale = state.hello["capital"]["quote_scale"]
    price = (
        holding.cost
        * 10 ** (symbol_info["qty_scale"] + symbol_info["price_scale"])
        // (holding.qty * 10**quote_scale)
    )
    return price if price > 0 else None


def on_batch(state, batch):
    global entry_price

    # Record this batch's close first, so the SMA includes it.
    close = state.close(SYMBOL)
    if close is not None:
        closes.append(close)

    # Learn the actual fill price of any buy, and clear it on a sell.
    for fill in batch.get("fills", []):
        if fill.get("symbol") != SYMBOL:
            continue
        if fill.get("status") != "filled":
            continue
        if fill.get("side") == "buy":
            entry_price = int(fill["price"])
        elif fill.get("side") == "sell":
            entry_price = None

    # No trading during warm-up, and never on the last batch: an order
    # decided there is rejected (after_end) and the window then flattens
    # any open position at the last close anyway.
    if state.warming_up:
        return []
    if state.seq >= state.warmup_bars + TOTAL_BARS - 1:
        return []

    if close is None or len(closes) < SMA_WINDOW:
        return []

    holding = _holding(state)

    if holding is not None:
        # Holding: check the exit rules against the entry fill price.
        if entry_price is None:
            entry_price = _fallback_entry_price(state, holding)
        if entry_price is None:
            return []
        if close * 1000 >= entry_price * 1015 or close * 100 <= entry_price * 98:
            return [
                {
                    "side": "sell",
                    "symbol": SYMBOL,
                    "qty": holding.qty,
                    "max_slippage_bps": 100,
                }
            ]
        return []

    # Flat: check the entry rule against SMA30 of the last 30 closes.
    if state.cash <= 0:
        return []
    sma_sum = sum(closes[-SMA_WINDOW:])
    # close < 0.988 * (sma_sum / 30)  <=>  close * 30000 < 988 * sma_sum
    if close * 30000 < 988 * sma_sum:
        # Full cash, but never above the weight cap: when flat cash
        # equals equity so the cap does not bind, yet this keeps the
        # order valid even if a stray position were ever present.
        notional = min(state.cash, state.weight_room(SYMBOL))
        if notional <= 0:
            return []
        return [
            {
                "side": "buy",
                "symbol": SYMBOL,
                "notional_quote_units": notional,
                "max_slippage_bps": 100,
            }
        ]
    return []


makefun_bars.run(on_batch)
