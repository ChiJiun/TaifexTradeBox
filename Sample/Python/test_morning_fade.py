import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from strategy_core import Fill, PositionLedger, Quote, StrategyConfig
from strategies.morning_fade import MorningFadeStrategy


def tick(clock, last, t):
    return Quote(symbol="TMFK6", bid=last - 1, ask=last + 1, bid_qty=5, ask_qty=5, last=last, last_qty=1,
                 total_volume=int(t), exchange_time=clock, received_at=t)


class MorningFadeTests(unittest.TestCase):
    def make(self, **settings):
        config = StrategyConfig(symbol="TMFK6", point_value=10, fee_per_fill=50, tax_rate=0.00002,
                                base_qty=1, max_order_qty=1, max_inventory=1)
        self.ledger = PositionLedger(config)
        return MorningFadeStrategy(config, self.ledger, **settings)

    def test_sells_into_a_morning_rally_at_ten(self):
        strategy = self.make()
        strategy.decide(tick("08:45:00", 46500, 1.0))
        self.assertEqual(strategy.decide(tick("09:59:59", 46600, 2.0)).intents, ())
        (intent,) = strategy.decide(tick("10:00:00", 46600, 3.0)).intents
        self.assertEqual((intent.side, intent.role, intent.price), ("sell", "entry", 46599 - 2))

    def test_buys_after_a_morning_selloff(self):
        strategy = self.make()
        strategy.decide(tick("08:45:01", 46500, 1.0))
        (intent,) = strategy.decide(tick("10:01:00", 46420, 2.0)).intents
        self.assertEqual(intent.side, "buy")

    def test_no_trade_without_seeing_the_open(self):
        strategy = self.make()
        strategy.decide(tick("09:30:00", 46500, 1.0))
        self.assertEqual(strategy.decide(tick("10:00:00", 46600, 2.0)).intents, ())

    def test_entry_window_closes(self):
        strategy = self.make()
        strategy.decide(tick("08:45:00", 46500, 1.0))
        self.assertEqual(strategy.decide(tick("10:05:00", 46600, 2.0)).intents, ())

    def test_stop_crosses_and_holds_its_price(self):
        strategy = self.make()
        strategy.decide(tick("08:45:00", 46500, 1.0))
        self.ledger.on_fill(Fill("sell", 46600, 1, "A1", 2.0))
        self.assertEqual(strategy.decide(tick("11:00:00", 46700, 3.0)).intents, ())
        (stop,) = strategy.decide(tick("11:30:00", 46750, 4.0)).intents
        self.assertEqual((stop.side, stop.role, stop.price), ("buy", "risk_exit", 46751 + 2))
        self.assertEqual(strategy.decide(tick("11:30:02", 46760, 6.0)).intents, (stop,))

    def test_one_trade_a_day_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            state = str(Path(folder) / "fade.json")
            strategy = self.make(state_path=state)
            strategy.decide(tick("08:45:00", 46500, 1.0))
            self.ledger.on_fill(Fill("sell", 46600, 1, "A1", 2.0))
            strategy.decide(tick("10:00:01", 46600, 3.0))
            self.ledger.on_fill(Fill("buy", 46751, 1, "A2", 4.0))
            # Restarted bot: same day, flat, inside the window again.
            again = self.make(state_path=state)
            self.assertEqual(again.decide(tick("10:02:00", 46700, 5.0)).intents, ())
            self.assertEqual(again.open_price, 46500)

    def test_restart_before_ten_keeps_the_open(self):
        with tempfile.TemporaryDirectory() as folder:
            state = str(Path(folder) / "fade.json")
            self.make(state_path=state).decide(tick("08:45:00", 46500, 1.0))
            again = self.make(state_path=state)
            (intent,) = again.decide(tick("10:00:00", 46400, 2.0)).intents
            self.assertEqual(intent.side, "buy")

    def test_a_new_day_in_the_same_process_starts_over(self):
        strategy = self.make()
        days = iter(["2026-10-29", "2026-10-30"])
        today = next(days)
        strategy._today = lambda: today
        strategy.decide(tick("08:45:00", 46800, 1.0))
        strategy.decide(tick("10:00:00", 47100, 2.0))
        today = next(days)
        strategy.decide(tick("08:45:00", 47000, 3.0))
        # Day two falls from its own open, so the fade buys.
        (intent,) = strategy.decide(tick("10:00:00", 46900, 4.0)).intents
        self.assertEqual(intent.side, "buy")

    def test_a_late_first_quote_is_not_taken_as_the_open(self):
        strategy = self.make()
        strategy.decide(tick("08:48:00", 46500, 1.0))
        self.assertEqual(strategy.decide(tick("10:00:00", 46600, 2.0)).intents, ())

    def test_requote_keeps_the_first_side(self):
        strategy = self.make()
        strategy.decide(tick("08:45:00", 46500, 1.0))
        (first,) = strategy.decide(tick("10:00:00", 46501, 2.0)).intents
        (second,) = strategy.decide(tick("10:00:07", 46490, 9.0)).intents
        self.assertEqual((first.side, second.side, second.price), ("sell", "sell", 46489 - 2))

    def test_restart_after_the_entry_was_sent_does_not_enter_again(self):
        with tempfile.TemporaryDirectory() as folder:
            state = str(Path(folder) / "fade.json")
            strategy = self.make(state_path=state)
            strategy.decide(tick("08:45:00", 46500, 1.0))
            self.assertEqual(len(strategy.decide(tick("10:00:00", 46600, 2.0)).intents), 1)
            again = self.make(state_path=state)
            self.assertEqual(again.decide(tick("10:01:00", 46600, 3.0)).intents, ())

    def test_a_locked_state_file_does_not_stop_trading(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "fade.json"
            strategy = self.make(state_path=str(state))
            locked = mock.patch("strategies.morning_fade.os.replace", side_effect=PermissionError(5, "denied"))
            with locked, mock.patch("strategies.morning_fade.time.sleep"), self.assertLogs("strategies.morning_fade"):
                strategy.decide(tick("08:45:00", 46500, 1.0))
                self.assertEqual(len(strategy.decide(tick("10:00:00", 46600, 2.0)).intents), 1)
            self.assertFalse(state.exists())
            strategy.decide(tick("10:00:01", 46600, 3.0))
            self.assertTrue(json.loads(state.read_text(encoding="utf-8"))["traded"])

    def test_competition_template_parameters_build_the_strategy(self):
        template = Path(__file__).resolve().parent / "automation" / "config.competition.example.json"
        config = json.loads(template.read_text(encoding="utf-8"))
        args = config["extraArgs"]
        portfolio = json.loads(args[args.index("--strategy-params-json") + 1])
        (fade,) = [m for m in portfolio["members"] if m["module"] == "strategies.morning_fade:MorningFadeStrategy"]
        self.assertEqual(self.make(**fade["params"]).settings.stop_ticks, 150)


if __name__ == "__main__":
    unittest.main()
