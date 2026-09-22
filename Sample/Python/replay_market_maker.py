"""Deterministic research replay. Input is chronological Quote fields in CSV."""
import argparse
import csv
import json
from dataclasses import fields

from strategy_core import Quote, StrategyConfig, PositionLedger, MicropriceMarketMaker, IndependentFillSimulator


def replay(rows, config):
    ledger = PositionLedger(config)
    strategy = MicropriceMarketMaker(config, ledger)
    simulator = IndependentFillSimulator()
    previous_time = float("-inf")
    count = 0
    quote = None
    peak = 0.0
    drawdown = 0.0
    for row in rows:
        values = {f.name: row[f.name] for f in fields(Quote)}
        for key in values:
            if key not in ("symbol", "exchange_time"):
                values[key] = float(values[key])
        values["last_qty"] = int(values["last_qty"])
        quote = Quote(**values)
        if not quote.is_valid() or quote.symbol.upper() != config.symbol.upper() or quote.received_at < previous_time:
            raise ValueError("Replay requires valid chronological quotes for one symbol")
        previous_time = quote.received_at
        for fill in simulator.on_quote(quote):
            ledger.on_fill(fill)
        decision = strategy.decide(quote)
        simulator.sync(decision.intents, quote.received_at)
        equity = ledger.mark_to_market(quote.bid if ledger.position > 0 else quote.ask)
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        count += 1
    return {"quotes": count, "filled_contracts": ledger.fill_count,
            "realized_net": ledger.realized_pnl, "fees_tax": ledger.fees_and_tax,
            "open_position": ledger.position,
            "marked_equity": ledger.mark_to_market(quote.bid if ledger.position > 0 else quote.ask) if quote else 0,
            "max_marked_drawdown": drawdown, "halt_reason": strategy.halt_reason,
            "model": "later unique trade; shared volume; limit-price fills; no queue model",
            "note": "Open inventory is marked, not assumed liquidated. No profitability claim."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file")
    parser.add_argument("--symbol", required=True)
    args = parser.parse_args()
    config = StrategyConfig(symbol=args.symbol, fee_per_fill=100, tax_rate=.00002,
                            target_ticks=8, max_order_qty=1, max_inventory=1,
                            recovery_fraction=0, daily_loss_limit=10000)
    with open(args.csv_file, encoding="utf-8-sig", newline="") as stream:
        print(json.dumps(replay(csv.DictReader(stream), config), ensure_ascii=False, indent=2))
