"""
╔══════════════════════════════════════════════════════════════════════════╗
║  BTC-15M-Oracle — Autonomous Polymarket BTC Prediction Bot              ║
║                                                                          ║
║  Clock-synced entries at :59 / :14 / :29 / :44                          ║
║  Live trading via py-clob-client SDK                                     ║
║  BTC 15-minute UP/DOWN binary markets ONLY                               ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import signal
import sys
import time
import logging
import json
import datetime
from pathlib import Path

from config.settings import BotConfig, MarketDirection
from oracles.price_feed import OracleEngine
from strategies.signal_engine import StrategyEngine
from core.polymarket_client import PolymarketClient
from core.risk_manager import RiskManager
from core.trade_logger import TradeLogger
from core.edge import EdgeEngine
from core.dashboard_server import DashboardServer, build_dashboard_state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot")


class BTCPredictionBot:
    def __init__(self, config: BotConfig, dashboard: bool = False):
        self.config = config
        self.running = False
        self.trade_logger = TradeLogger(config.logging)
        self.oracle = OracleEngine(config)
        self.strategy = StrategyEngine(config.strategy)
        self.polymarket = PolymarketClient(config)
        self.risk_manager = RiskManager(config.risk, capital=config.bankroll)
        self.edge = EdgeEngine(config.edge)
        self.dashboard = DashboardServer() if dashboard else None
        self._cycle_count = 0
        self._start_time = 0
        self._traded_this_window = False
        self._last_consensus = None
        self._last_anchor = None
        self._last_decision = None

    # ── Trading Cycle ───────────────────────────────────────────

    async def _trading_cycle(self):
        self._cycle_count += 1

        try:
            # 1. Capture window opening price (Chainlink — resolution oracle)
            anchor = await self.oracle.capture_window_open()
            open_price = anchor.open_price if anchor else None
            self._last_anchor = anchor

            # 2. Get current price
            consensus = await self.oracle.get_price()
            self._last_consensus = consensus
            self.trade_logger.log_oracle({
                "price": consensus.price, "chainlink": consensus.chainlink_price,
                "sources": consensus.sources, "spread_pct": consensus.spread_pct,
                "window_open": open_price,
            })

            # 3. Candles
            candles = await self.oracle.get_candles("15m", limit=100)
            if len(candles) < 30:
                logger.warning(f"Only {len(candles)} candles — skipping")
                return

            # 4. Strategy (anchored to window open price)
            decision = self.strategy.analyze(candles, consensus.price, open_price=open_price)
            self._last_decision = decision
            self.trade_logger.log_strategy({
                "direction": decision.direction.value,
                "confidence": decision.confidence,
                "should_trade": decision.should_trade,
                "drift_pct": decision.drift_pct,
                "open_price": open_price,
                "signals": {s.name: {"dir": s.direction.value, "str": round(s.strength, 3)} for s in decision.signals},
                "btc_price": consensus.price,
            })

            if not decision.should_trade:
                logger.info(f"Cycle {self._cycle_count}: HOLD — {decision.reason}")
                return

            # 5. Risk
            can_trade, reason = self.risk_manager.can_trade()
            if not can_trade:
                logger.info(f"Cycle {self._cycle_count}: BLOCKED — {reason}")
                return

            # 6. Markets
            markets = await self.polymarket.discover_markets()
            tradeable = [m for m in markets if m.is_tradeable and m.liquidity >= self.config.polymarket.min_liquidity_usd]

            if not tradeable:
                logger.info(f"Cycle {self._cycle_count}: No tradeable markets")
                return

            market = max(tradeable, key=lambda m: m.liquidity)

            # 6a. Arbitrage scan (if enabled)
            arb_opps = self.edge.scan_arb(tradeable)
            for opp in arb_opps:
                arb_market = next((m for m in tradeable if m.condition_id == opp.market_condition_id), None)
                if arb_market:
                    # Buy UP side
                    await self.polymarket.place_order(
                        market=arb_market, direction="up", size_usd=opp.size_per_side,
                        oracle_price=consensus.price, confidence=1.0,
                    )
                    # Buy DOWN side
                    await self.polymarket.place_order(
                        market=arb_market, direction="down", size_usd=opp.size_per_side,
                        oracle_price=consensus.price, confidence=1.0,
                    )
                    self.trade_logger.log_trade({
                        "type": "arb", "edge_pct": opp.edge_pct,
                        "size_per_side": opp.size_per_side, "profit": opp.guaranteed_profit,
                        "market": opp.question[:80],
                    })

            # 6b. Hedge check (if enabled)
            open_trades = self.polymarket.get_trade_records()
            hedges = self.edge.check_hedge(
                open_trades=open_trades,
                current_direction=direction,
                current_confidence=decision.confidence,
                markets=self.polymarket._active_markets,
            )
            for h in hedges:
                hedge_market = next(
                    (m for m in tradeable if m.condition_id ==
                     next((t.market_condition_id for t in open_trades if t.trade_id == h.original_trade_id), "")),
                    None
                )
                if hedge_market:
                    trade = await self.polymarket.place_order(
                        market=hedge_market, direction=h.hedge_direction,
                        size_usd=h.size_usd, oracle_price=consensus.price,
                        confidence=decision.confidence,
                    )
                    if trade:
                        self.edge.mark_hedged(h.original_trade_id)
                        self.trade_logger.log_trade({
                            "type": "hedge", "original": h.original_trade_id,
                            "hedge_dir": h.hedge_direction, "locked_profit": h.locked_profit,
                        })

            # 7. Execute directional trade
            direction = decision.direction.value
            size = self.risk_manager.calculate_position_size(decision.confidence)
            if size <= 0:
                return

            trade = await self.polymarket.place_order(
                market=market, direction=direction, size_usd=size,
                oracle_price=consensus.price, confidence=decision.confidence,
            )

            if trade:
                self.trade_logger.log_trade({
                    "trade_id": trade.trade_id, "direction": trade.direction,
                    "size_usd": trade.size_usd, "confidence": trade.confidence,
                    "oracle_price": trade.oracle_price_at_entry,
                    "order_id": trade.order_id,
                    "market": market.question[:80],
                })

            # 8. Resolutions
            resolved = await self.polymarket.check_resolutions()
            for r in resolved:
                self.risk_manager.record_trade(r.pnl)
                self.trade_logger.log_resolution({"trade_id": r.trade_id, "outcome": r.outcome, "pnl": r.pnl})

            # 8. Status
            stats = self.polymarket.get_stats()
            self.trade_logger.save_performance({
                "cycle": self._cycle_count, "btc_price": consensus.price,
                **stats, **self.risk_manager.get_status(),
            })

            logger.info(
                f"Cycle {self._cycle_count} | BTC=${consensus.price:,.2f} | "
                f"{direction.upper()} conf={decision.confidence:.2f} | "
                f"W/R={stats.get('win_rate', 0):.0f}%"
            )

        except Exception as e:
            logger.error(f"Cycle {self._cycle_count} error: {e}", exc_info=True)

        finally:
            # Broadcast state to dashboard (even on error/hold)
            if self.dashboard and self.dashboard.is_running:
                try:
                    state = build_dashboard_state(
                        cycle=self._cycle_count,
                        consensus=self._last_consensus,
                        anchor=self._last_anchor,
                        decision=self._last_decision,
                        risk_manager=self.risk_manager,
                        polymarket_client=self.polymarket,
                        edge_config=self.config.edge,
                        config=self.config,
                    )
                    await self.dashboard.broadcast(state)
                except Exception as e:
                    logger.warning(f"Dashboard broadcast failed: {e}")

    # ── Clock Sync ──────────────────────────────────────────────

    @staticmethod
    def _next_boundary() -> float:
        now = time.time()
        dt = datetime.datetime.fromtimestamp(now)
        next_min = ((dt.minute // 15) + 1) * 15
        if next_min >= 60:
            b = dt.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
        else:
            b = dt.replace(minute=next_min, second=0, microsecond=0)
        return b.timestamp()

    def _seconds_until_entry(self) -> float:
        return self._next_boundary() - self.config.entry_lead_secs - time.time()

    def _is_in_entry_window(self) -> bool:
        secs = self._seconds_until_entry()
        return -self.config.entry_window_secs <= secs <= 0

    def _format_next_entry(self) -> str:
        entry_ts = self._next_boundary() - self.config.entry_lead_secs
        entry_dt = datetime.datetime.fromtimestamp(entry_ts)
        boundary_dt = datetime.datetime.fromtimestamp(self._next_boundary())
        return f"{entry_dt.strftime('%H:%M:%S')} (→ {boundary_dt.strftime('%H:%M')})"

    # ── Main Loop ───────────────────────────────────────────────

    async def run(self):
        print()
        print("=" * 60)
        print("  BTC-15M-Oracle — LIVE")
        print(f"  Bankroll: ${self.config.bankroll:,.2f}")
        print(f"  Arb: {'ON' if self.config.edge.enable_arb else 'off'}  |  Hedge: {'ON' if self.config.edge.enable_hedge else 'off'}")
        print(f"  Entry: {self.config.entry_lead_secs}s before :00/:15/:30/:45")
        print(f"  Next: {self._format_next_entry()}")
        if self.dashboard:
            print(f"  Dashboard: ws://localhost:8765")
        print("=" * 60)
        print()

        # Start dashboard server if enabled
        if self.dashboard:
            await self.dashboard.start()

        self.running = True
        self._start_time = time.time()

        while self.running:
            if self._is_in_entry_window():
                if not self._traded_this_window:
                    boundary = datetime.datetime.fromtimestamp(self._next_boundary())
                    logger.info(f"⏰ ENTRY — targeting {boundary.strftime('%H:%M')}")
                    await self._trading_cycle()
                    self._traded_this_window = True
                    logger.info(f"💤 Next: {self._format_next_entry()}")
            else:
                self._traded_this_window = False

            await asyncio.sleep(self.config.sleep_poll_secs)

    def stop(self):
        self.running = False
        logger.info("Shutdown initiated")

    async def shutdown(self):
        self.stop()
        await self.oracle.close()
        await self.polymarket.close()
        if self.dashboard:
            await self.dashboard.stop()
        stats = self.polymarket.get_stats()
        self.trade_logger.save_performance({
            "status": "shutdown", "cycles": self._cycle_count,
            "uptime_secs": time.time() - self._start_time, **stats,
        })
        logger.info(f"Stopped after {self._cycle_count} cycles")


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="BTC-15M-Oracle — Polymarket Prediction Bot")
    parser.add_argument("--bankroll", type=float, default=500.0, help="Starting bankroll in USD (default: 500)")
    parser.add_argument("--cycles", type=int, default=0, help="Max cycles, 0=unlimited (default: 0)")
    parser.add_argument("--arb", action="store_true", help="Enable arbitrage scanner")
    parser.add_argument("--hedge", action="store_true", help="Enable hedge engine")
    parser.add_argument("--dashboard", action="store_true", help="Start WebSocket server on :8765 for live dashboard")
    args = parser.parse_args()

    config = BotConfig(bankroll=args.bankroll)
    config.edge.enable_arb = args.arb
    config.edge.enable_hedge = args.hedge
    bot = BTCPredictionBot(config, dashboard=args.dashboard)

    def handle_signal(sig, frame):
        print("\n\nCtrl+C — shutting down...")
        bot.stop()
    signal.signal(signal.SIGINT, handle_signal)

    try:
        if args.cycles > 0:
            bot._start_time = time.time()
            bot.running = True
            completed = 0
            print(f"\nRunning {args.cycles} cycles | Bankroll: ${args.bankroll}")
            print(f"Next: {bot._format_next_entry()}\n")
            while completed < args.cycles and bot.running:
                if bot._is_in_entry_window():
                    if not bot._traded_this_window:
                        await bot._trading_cycle()
                        bot._traded_this_window = True
                        completed += 1
                        if completed < args.cycles:
                            logger.info(f"Cycle {completed}/{args.cycles}. Next: {bot._format_next_entry()}")
                else:
                    bot._traded_this_window = False
                await asyncio.sleep(bot.config.sleep_poll_secs)
        else:
            await bot.run()
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
