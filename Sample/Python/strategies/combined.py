from __future__ import annotations

from .competition_pnl import CompetitionPnlShaping
from .order_flow_maker import OrderFlowGatedMaker


class CombinedCompetitionMaker(CompetitionPnlShaping, OrderFlowGatedMaker):
    """Strategies 01, 03 and 04 stacked through the MRO.

    The 04 daily equity limits decide first, the 03 order-flow gate filters
    entries next, and 01's sticky one-tick quotes generate the intents.
    """
