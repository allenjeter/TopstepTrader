import asyncio
import websockets
import json
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
import requests
import time
import hmac
import hashlib
import base64

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"

class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"

@dataclass
class MarketTick:
    symbol: str
    price: float
    volume: int
    timestamp: datetime
    bid: float
    ask: float
    bid_size: int
    ask_size: int

@dataclass
class BreakoutLevel:
    price: float
    volume: float
    timestamp: datetime
    strength: float  # 0-1 confidence score

@dataclass
class Position:
    symbol: str
    side: OrderSide
    quantity: int
    entry_price: float
    timestamp: datetime
    stop_loss: float
    take_profit: float

class TradingViewDataFeed:
    """TradingView real-time market data feed for CME futures"""
    
    def __init__(self, username: str, password: str, symbols: List[str]):
        self.username = username
        self.password = password
        self.symbols = symbols
        self.websocket = None
        self.session_id = None
        self.chart_sessions = {}
        self.callbacks = {}
        self.running = False
        
        # TradingView WebSocket URL
        self.ws_url = "wss://data.tradingview.com/socket.io/websocket"
        
        # Symbol mapping for CME futures
        self.symbol_map = {
            "ES": "CME_MINI:ES1!",  # E-mini S&P 500
            "NQ": "CME_MINI:NQ1!",  # E-mini Nasdaq 100
            "YM": "CBOT_MINI:YM1!", # E-mini Dow Jones
            "RTY": "CME_MINI:RTY1!", # E-mini Russell 2000
            "CL": "NYMEX:CL1!",     # Crude Oil
            "GC": "COMEX:GC1!",     # Gold
            "SI": "COMEX:SI1!",     # Silver
            "ZN": "CBOT:ZN1!",      # 10-Year T-Note
            "ZB": "CBOT:ZB1!",      # 30-Year T-Bond
            "6E": "CME:6E1!",       # Euro FX
            "6J": "CME:6J1!",       # Japanese Yen
        }
    
    def add_callback(self, symbol: str, callback):
        """Add callback for symbol updates"""
        if symbol not in self.callbacks:
            self.callbacks[symbol] = []
        self.callbacks[symbol].append(callback)
    
    async def connect(self):
        """Connect to TradingView websocket"""
        try:
            self.websocket = await websockets.connect(self.ws_url)
            self.running = True
            
            # Send initial connection message
            await self._send_message("set_auth_token", [self.username, self.password])
            
            # Create chart sessions for each symbol
            for symbol in self.symbols:
                await self._create_chart_session(symbol)
            
            # Start listening for messages
            await self._listen_messages()
            
        except Exception as e:
            logger.error(f"Failed to connect to TradingView: {e}")
            self.running = False
    
    async def disconnect(self):
        """Disconnect from TradingView"""
        self.running = False
        if self.websocket:
            await self.websocket.close()
    
    async def _send_message(self, method: str, params: List = None):
        """Send message to TradingView websocket"""
        if not self.websocket:
            return
        
        message = {
            "m": method,
            "p": params or []
        }
        
        await self.websocket.send(json.dumps(message))
    
    async def _create_chart_session(self, symbol: str):
        """Create a chart session for a symbol"""
        tv_symbol = self.symbol_map.get(symbol, symbol)
        session_id = f"cs_{symbol}_{int(time.time())}"
        
        # Create chart session
        await self._send_message("chart_create_session", [session_id, ""])
        
        # Set symbol
        await self._send_message("resolve_symbol", [
            session_id,
            "symbol_1",
            f'={{"symbol":"{tv_symbol}","adjustment":"splits","session":"extended"}}'
        ])
        
        # Create series (1-second bars for scalping)
        await self._send_message("create_series", [
            session_id,
            "s1",
            "s1",
            "symbol_1",
            "1S",  # 1-second resolution
            300    # 300 bars
        ])
        
        # Subscribe to quote data
        await self._send_message("quote_create_session", [f"qs_{symbol}"])
        await self._send_message("quote_set_fields", [
            f"qs_{symbol}",
            "ch", "chp", "current_session", "description", "local_description",
            "language", "exchange", "fractional", "is_tradable", "lp", "lp_time",
            "minmov", "minmove2", "original_name", "pricescale", "pro_name",
            "short_name", "type", "update_mode", "volume", "currency_code",
            "rchp", "rtc", "rtc_time", "status", "basic_eps_net_income",
            "beta_1_year", "market_cap_basic", "earnings_per_share_basic_ttm",
            "industry", "sector", "bid", "ask", "bid_size", "ask_size"
        ])
        
        await self._send_message("quote_add_symbols", [f"qs_{symbol}", tv_symbol])
        
        self.chart_sessions[symbol] = session_id
    
    async def _listen_messages(self):
        """Listen for incoming messages"""
        try:
            async for message in self.websocket:
                if not self.running:
                    break
                
                try:
                    data = json.loads(message)
                    await self._process_message(data)
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
        
        except websockets.exceptions.ConnectionClosed:
            logger.warning("TradingView connection closed")
        except Exception as e:
            logger.error(f"Error in message listener: {e}")
    
    async def _process_message(self, data: Dict):
        """Process incoming message from TradingView"""
        method = data.get("m")
        params = data.get("p", [])
        
        if method == "timescale_update":
            await self._handle_timescale_update(params)
        elif method == "quote_completed":
            await self._handle_quote_update(params)
        elif method == "qsd":  # Quote symbol data
            await self._handle_quote_data(params)
    
    async def _handle_timescale_update(self, params: List):
        """Handle timescale updates (price bars)"""
        if len(params) < 2:
            return
        
        session_id = params[0]
        update_data = params[1]
        
        # Find symbol for this session
        symbol = None
        for sym, sess_id in self.chart_sessions.items():
            if sess_id == session_id:
                symbol = sym
                break
        
        if not symbol or "s1" not in update_data:
            return
        
        series_data = update_data["s1"]
        if "s" not in series_data:
            return
        
        bars = series_data["s"]
        for bar in bars:
            if len(bar) >= 6:  # [time, open, high, low, close, volume]
                timestamp = datetime.fromtimestamp(bar[0])
                price = bar[4]  # Close price
                volume = bar[5] if len(bar) > 5 else 0
                
                # Create market tick
                tick = MarketTick(
                    symbol=symbol,
                    price=price,
                    volume=volume,
                    timestamp=timestamp,
                    bid=price - 0.25,  # Approximate bid
                    ask=price + 0.25,  # Approximate ask
                    bid_size=1,
                    ask_size=1
                )
                
                # Call callbacks
                if symbol in self.callbacks:
                    for callback in self.callbacks[symbol]:
                        await callback(tick)
    
    async def _handle_quote_data(self, params: List):
        """Handle real-time quote data"""
        if len(params) < 2:
            return
        
        session_id = params[0]
        quote_data = params[1]
        
        # Extract symbol from session ID
        symbol = session_id.replace("qs_", "")
        
        if symbol not in self.callbacks:
            return
        
        # Extract quote information
        price = quote_data.get("lp", 0)  # Last price
        volume = quote_data.get("volume", 0)
        bid = quote_data.get("bid", price - 0.25)
        ask = quote_data.get("ask", price + 0.25)
        bid_size = quote_data.get("bid_size", 1)
        ask_size = quote_data.get("ask_size", 1)
        
        if price > 0:
            tick = MarketTick(
                symbol=symbol,
                price=price,
                volume=volume,
                timestamp=datetime.now(),
                bid=bid,
                ask=ask,
                bid_size=bid_size,
                ask_size=ask_size
            )
            
            # Call callbacks
            for callback in self.callbacks[symbol]:
                await callback(tick)
    
    async def _handle_quote_update(self, params: List):
        """Handle quote update completion"""
        pass  # Can add additional processing if needed

class TopStepAPI:
    """TopStep API client for futures trading"""
    
    def __init__(self, api_key: str, secret: str, account_type: str = "practice", base_url: str = "https://api.topsteptrader.com"):
        self.api_key = api_key
        self.secret = secret
        self.account_type = account_type  # "practice" or "live"
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        })
        
        # Account selection in API calls
        self.account_prefix = "demo" if account_type == "practice" else "live"
        
        logger.info(f"🔧 TopStep API initialized for {account_type.upper()} account")
    
    async def get_account_info(self) -> Dict:
        """Get account information"""
        try:
            # Some APIs use different endpoints for practice vs live
            endpoint = f"{self.base_url}/account/{self.account_prefix}"
            response = self.session.get(endpoint)
            response.raise_for_status()
            
            account_data = response.json()
            
            # Log account details for verification
            logger.info(f"📊 Account Type: {self.account_type}")
            logger.info(f"📊 Account ID: {account_data.get('account_id', 'N/A')}")
            logger.info(f"📊 Balance: ${account_data.get('balance', 0):,.2f}")
            logger.info(f"📊 Buying Power: ${account_data.get('buying_power', 0):,.2f}")
            
            return account_data
            
        except Exception as e:
            logger.error(f"Error getting account info: {e}")
            return {}
    
    async def get_positions(self) -> List[Dict]:
        """Get current positions"""
        try:
            endpoint = f"{self.base_url}/positions/{self.account_prefix}"
            response = self.session.get(endpoint)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []
    
    async def place_order(self, symbol: str, side: OrderSide, quantity: int, 
                         order_type: OrderType = OrderType.MARKET, 
                         price: Optional[float] = None) -> Dict:
        """Place a futures order"""
        
        # Safety check for practice account
        if self.account_type == "practice":
            logger.info(f"🧪 PRACTICE ORDER: {side.value} {quantity} {symbol} @ {price if price else 'market'}")
        else:
            logger.warning(f"🚨 LIVE ORDER: {side.value} {quantity} {symbol} @ {price if price else 'market'}")
        
        order_data = {
            'symbol': symbol,
            'side': side.value,
            'quantity': quantity,
            'type': order_type.value,
            'account_type': self.account_type
        }
        
        if price and order_type != OrderType.MARKET:
            order_data['price'] = price
        
        try:
            endpoint = f"{self.base_url}/orders/{self.account_prefix}"
            response = self.session.post(endpoint, json=order_data)
            response.raise_for_status()
            
            order_result = response.json()
            
            # Log order confirmation
            order_id = order_result.get('order_id', 'N/A')
            fill_price = order_result.get('fill_price', price or 0)
            
            logger.info(f"✅ Order executed - ID: {order_id}, Fill: ${fill_price:.2f}")
            
            return order_result
            
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            return {}
    
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order"""
        try:
            endpoint = f"{self.base_url}/orders/{self.account_prefix}/{order_id}"
            response = self.session.delete(endpoint)
            response.raise_for_status()
            
            logger.info(f"❌ Order cancelled: {order_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error canceling order: {e}")
            return False
    
    async def get_order_status(self, order_id: str) -> Dict:
        """Get order status"""
        try:
            endpoint = f"{self.base_url}/orders/{self.account_prefix}/{order_id}"
            response = self.session.get(endpoint)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error getting order status: {e}")
            return {}
    
    async def get_account_rules(self) -> Dict:
        """Get account trading rules and limits"""
        try:
            endpoint = f"{self.base_url}/account/{self.account_prefix}/rules"
            response = self.session.get(endpoint)
            response.raise_for_status()
            
            rules = response.json()
            
            # Log important rules
            logger.info(f"📋 Max Daily Loss: ${rules.get('max_daily_loss', 0):,.2f}")
            logger.info(f"📋 Max Position Size: {rules.get('max_position_size', 0)}")
            logger.info(f"📋 Allowed Symbols: {rules.get('allowed_symbols', [])}")
            
            return rules
            
        except Exception as e:
            logger.error(f"Error getting account rules: {e}")
            return {}

class BreakoutDetector:
    """Detects breakout opportunities using technical analysis"""
    
    def __init__(self, lookback_period: int = 20, volume_threshold: float = 1.5):
        self.lookback_period = lookback_period
        self.volume_threshold = volume_threshold
        self.price_data = {}
        self.volume_data = {}
    
    def update_data(self, tick: MarketTick):
        """Update price and volume data from market tick"""
        symbol = tick.symbol
        
        if symbol not in self.price_data:
            self.price_data[symbol] = []
            self.volume_data[symbol] = []
        
        self.price_data[symbol].append((tick.timestamp, tick.price))
        self.volume_data[symbol].append((tick.timestamp, tick.volume))
        
        # Keep only recent data
        cutoff_time = tick.timestamp - timedelta(minutes=self.lookback_period)
        self.price_data[symbol] = [(t, p) for t, p in self.price_data[symbol] if t > cutoff_time]
        self.volume_data[symbol] = [(t, v) for t, v in self.volume_data[symbol] if t > cutoff_time]
    
    def detect_breakout(self, tick: MarketTick) -> Optional[BreakoutLevel]:
        """Detect breakout opportunities from market tick"""
        symbol = tick.symbol
        
        if symbol not in self.price_data or len(self.price_data[symbol]) < 10:
            return None
        
        prices = [p for _, p in self.price_data[symbol]]
        volumes = [v for _, v in self.volume_data[symbol]]
        
        # Calculate support and resistance levels
        highs = self._find_peaks(prices, window=5)
        lows = self._find_troughs(prices, window=5)
        
        resistance_level = np.mean(highs) if highs else max(prices)
        support_level = np.mean(lows) if lows else min(prices)
        
        # Check for breakout conditions
        avg_volume = np.mean(volumes[-10:]) if len(volumes) >= 10 else tick.volume
        volume_surge = tick.volume / avg_volume if avg_volume > 0 else 1
        
        # Consider bid/ask spread for better entry timing
        spread = tick.ask - tick.bid
        mid_price = (tick.bid + tick.ask) / 2
        
        # Bullish breakout
        if tick.price > resistance_level and volume_surge > self.volume_threshold:
            # Additional confirmation: price should be near ask for strong buying
            price_momentum = (tick.price - mid_price) / spread if spread > 0 else 0
            
            strength = min(1.0, 
                         (tick.price - resistance_level) / resistance_level * 100 + 
                         (volume_surge - 1) * 0.2 +
                         price_momentum * 0.1)
            
            return BreakoutLevel(
                price=tick.price,
                volume=tick.volume,
                timestamp=tick.timestamp,
                strength=strength
            )
        
        # Bearish breakout
        elif tick.price < support_level and volume_surge > self.volume_threshold:
            # Additional confirmation: price should be near bid for strong selling
            price_momentum = (mid_price - tick.price) / spread if spread > 0 else 0
            
            strength = min(1.0, 
                         (support_level - tick.price) / support_level * 100 + 
                         (volume_surge - 1) * 0.2 +
                         price_momentum * 0.1)
            
            return BreakoutLevel(
                price=tick.price,
                volume=tick.volume,
                timestamp=tick.timestamp,
                strength=strength
            )
        
        return None
    
    def _find_peaks(self, prices: List[float], window: int = 5) -> List[float]:
        """Find local peaks in price data"""
        if len(prices) < window * 2 + 1:
            return []
        
        peaks = []
        for i in range(window, len(prices) - window):
            if all(prices[i] >= prices[i-j] for j in range(1, window+1)) and \
               all(prices[i] >= prices[i+j] for j in range(1, window+1)):
                peaks.append(prices[i])
        
        return peaks
    
    def _find_troughs(self, prices: List[float], window: int = 5) -> List[float]:
        """Find local troughs in price data"""
        if len(prices) < window * 2 + 1:
            return []
        
        troughs = []
        for i in range(window, len(prices) - window):
            if all(prices[i] <= prices[i-j] for j in range(1, window+1)) and \
               all(prices[i] <= prices[i+j] for j in range(1, window+1)):
                troughs.append(prices[i])
        
        return troughs

class RiskManager:
    """Manages risk and position sizing"""
    
    def __init__(self, max_risk_per_trade: float = 0.02, max_daily_loss: float = 0.05):
        self.max_risk_per_trade = max_risk_per_trade
        self.max_daily_loss = max_daily_loss
        self.daily_pnl = 0.0
        self.open_positions = {}
    
    def calculate_position_size(self, account_balance: float, entry_price: float, 
                              stop_loss: float, symbol: str) -> int:
        """Calculate position size based on risk management"""
        if self.daily_pnl <= -account_balance * self.max_daily_loss:
            logger.warning("Daily loss limit reached")
            return 0
        
        risk_amount = account_balance * self.max_risk_per_trade
        price_diff = abs(entry_price - stop_loss)
        
        if price_diff == 0:
            return 0
        
        # Calculate position size (simplified for futures)
        position_size = int(risk_amount / price_diff)
        
        # Apply position limits
        max_position = max(1, int(account_balance / entry_price * 0.1))  # Max 10% of account
        return min(position_size, max_position)
    
    def calculate_stops(self, entry_price: float, side: OrderSide, 
                       breakout_strength: float) -> Tuple[float, float]:
        """Calculate stop loss and take profit levels"""
        if side == OrderSide.BUY:
            # For long positions
            stop_loss = entry_price * (1 - 0.005 * (2 - breakout_strength))  # 0.3-0.5% stop
            take_profit = entry_price * (1 + 0.015 * breakout_strength)     # 1-1.5% target
        else:
            # For short positions
            stop_loss = entry_price * (1 + 0.005 * (2 - breakout_strength))
            take_profit = entry_price * (1 - 0.015 * breakout_strength)
        
        return stop_loss, take_profit
    
    def update_pnl(self, realized_pnl: float):
        """Update daily P&L"""
        self.daily_pnl += realized_pnl
    
    def can_trade(self) -> bool:
        """Check if trading is allowed based on risk limits"""
        return self.daily_pnl > -self.max_daily_loss * 10000  # Assuming $10k account

class FuturesScalpingBot:
    """Main trading bot class with TradingView integration"""
    
    def __init__(self, api_key: str, secret: str, tv_username: str, tv_password: str, 
                 symbols: List[str], account_type: str = "practice"):
        self.api = TopStepAPI(api_key, secret, account_type)
        self.breakout_detector = BreakoutDetector()
        self.risk_manager = RiskManager()
        self.symbols = symbols
        self.account_type = account_type
        self.positions = {}
        self.orders = {}
        self.running = False
        self.account_balance = 50000.0  # Default starting balance
        self.account_rules = {}
        
        # TradingView data feed
        self.data_feed = TradingViewDataFeed(tv_username, tv_password, symbols)
        
        # Market data storage
        self.latest_ticks = {}
        self.tick_history = {}
        
        # Trading statistics
        self.stats = {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'total_pnl': 0.0,
            'max_drawdown': 0.0,
            'start_time': None
        }
        
        # Register callbacks for each symbol
        for symbol in symbols:
            self.data_feed.add_callback(symbol, self._on_market_tick)
        
        # Safety warnings for live accounts
        if account_type == "live":
            logger.warning("🚨 LIVE TRADING MODE ENABLED - REAL MONEY AT RISK!")
            logger.warning("🚨 Make sure you've tested thoroughly on practice account first!")
        else:
            logger.info("🧪 PRACTICE MODE - Safe for testing and development")
    
    async def start(self):
        """Start the trading bot"""
        logger.info(f"Starting futures scalping bot in {self.account_type.upper()} mode...")
        self.running = True
        self.stats['start_time'] = datetime.now()
        
        # Get initial account info and rules
        account_info = await self.api.get_account_info()
        if account_info:
            self.account_balance = account_info.get('balance', 50000.0)
            logger.info(f"💰 Account balance: ${self.account_balance:,.2f}")
        
        # Get account rules
        self.account_rules = await self.api.get_account_rules()
        if self.account_rules:
            # Update risk manager with account-specific rules
            max_daily_loss = self.account_rules.get('max_daily_loss', 0)
            if max_daily_loss > 0:
                self.risk_manager.max_daily_loss = max_daily_loss / self.account_balance
                logger.info(f"📋 Updated max daily loss: ${max_daily_loss:,.2f}")
        
        # Verify symbols are allowed
        allowed_symbols = self.account_rules.get('allowed_symbols', [])
        if allowed_symbols:
            invalid_symbols = [s for s in self.symbols if s not in allowed_symbols]
            if invalid_symbols:
                logger.warning(f"⚠️ Invalid symbols for this account: {invalid_symbols}")
                self.symbols = [s for s in self.symbols if s in allowed_symbols]
                logger.info(f"✅ Trading symbols: {self.symbols}")
        
        # Connect to TradingView
        await self.data_feed.connect()
        logger.info("📡 Connected to TradingView data feed")
        
        # Start position monitoring
        asyncio.create_task(self._monitor_positions())
        
        # Start statistics reporting
        asyncio.create_task(self._report_statistics())
        
        # Practice mode specific features
        if self.account_type == "practice":
            logger.info("🧪 Practice mode features enabled:")
            logger.info("   - All orders clearly marked as PRACTICE")
            logger.info("   - Detailed logging for learning")
            logger.info("   - No real money risk")
        
        # Keep the bot running
        while self.running:
            await asyncio.sleep(1)
    
    async def _report_statistics(self):
        """Report trading statistics periodically"""
        while self.running:
            await asyncio.sleep(300)  # Every 5 minutes
            
            if self.stats['total_trades'] > 0:
                win_rate = (self.stats['winning_trades'] / self.stats['total_trades']) * 100
                avg_pnl = self.stats['total_pnl'] / self.stats['total_trades']
                
                runtime = datetime.now() - self.stats['start_time']
                
                logger.info("📈 TRADING STATISTICS:")
                logger.info(f"   Runtime: {runtime}")
                logger.info(f"   Total Trades: {self.stats['total_trades']}")
                logger.info(f"   Win Rate: {win_rate:.1f}%")
                logger.info(f"   Total P&L: ${self.stats['total_pnl']:,.2f}")
                logger.info(f"   Avg P&L per Trade: ${avg_pnl:.2f}")
                logger.info(f"   Max Drawdown: ${self.stats['max_drawdown']:,.2f}")
                logger.info(f"   Account Type: {self.account_type.upper()}")
    
    async def _close_position(self, position: Position, reason: str = "Manual", exit_price: float = None):
        """Close a position with enhanced tracking"""
        opposite_side = OrderSide.SELL if position.side == OrderSide.BUY else OrderSide.BUY
        
        # Safety confirmation for live accounts
        if self.account_type == "live":
            logger.warning(f"🚨 LIVE ACCOUNT: Closing {position.symbol} position")
        
        order_result = await self.api.place_order(
            position.symbol, opposite_side, position.quantity, OrderType.MARKET
        )
        
        if order_result:
            # Use provided exit price or order fill price
            final_exit_price = exit_price or order_result.get('fill_price', position.entry_price)
            
            # Calculate P&L
            if position.side == OrderSide.BUY:
                pnl = (final_exit_price - position.entry_price) * position.quantity
            else:
                pnl = (position.entry_price - final_exit_price) * position.quantity
            
            # Update statistics
            self.stats['total_trades'] += 1
            self.stats['total_pnl'] += pnl
            
            if pnl > 0:
                self.stats['winning_trades'] += 1
                logger.info(f"✅ WINNING trade")
            else:
                self.stats['losing_trades'] += 1
                logger.info(f"❌ LOSING trade")
            
            # Track drawdown
            if pnl < 0:
                self.stats['max_drawdown'] = min(self.stats['max_drawdown'], pnl)
            
            self.risk_manager.update_pnl(pnl)
            del self.positions[position.symbol]
            
            # Enhanced logging
            hold_time = datetime.now() - position.timestamp
            logger.info(f"📊 Position closed - {position.symbol} {position.side.value}")
            logger.info(f"   Entry: ${position.entry_price:.2f}")
            logger.info(f"   Exit: ${final_exit_price:.2f}")
            logger.info(f"   P&L: ${pnl:.2f}")
            logger.info(f"   Hold Time: {hold_time}")
            logger.info(f"   Reason: {reason}")
            logger.info(f"   Account: {self.account_type.upper()}")
    
    def get_trading_summary(self) -> Dict:
        """Get current trading summary"""
        return {
            'account_type': self.account_type,
            'account_balance': self.account_balance,
            'daily_pnl': self.risk_manager.daily_pnl,
            'open_positions': len(self.positions),
            'total_trades': self.stats['total_trades'],
            'win_rate': (self.stats['winning_trades'] / max(1, self.stats['total_trades'])) * 100,
            'total_pnl': self.stats['total_pnl'],
            'max_drawdown': self.stats['max_drawdown'],
            'symbols': self.symbols
        }
    
    async def stop(self):
        """Stop the trading bot"""
        logger.info("Stopping futures scalping bot...")
        self.running = False
        
        # Close all positions
        for position in self.positions.values():
            await self._close_position(position)
        
        # Disconnect from TradingView
        await self.data_feed.disconnect()
    
    async def _on_market_tick(self, tick: MarketTick):
        """Handle incoming market tick from TradingView"""
        symbol = tick.symbol
        
        # Store latest tick
        self.latest_ticks[symbol] = tick
        
        # Store tick history for analysis
        if symbol not in self.tick_history:
            self.tick_history[symbol] = []
        
        self.tick_history[symbol].append(tick)
        
        # Keep only recent history (last 1000 ticks)
        if len(self.tick_history[symbol]) > 1000:
            self.tick_history[symbol] = self.tick_history[symbol][-1000:]
        
        # Update breakout detector
        self.breakout_detector.update_data(tick)
        
        # Check for breakout opportunities
        breakout = self.breakout_detector.detect_breakout(tick)
        
        if breakout and breakout.strength > 0.65:  # High confidence threshold
            await self._handle_breakout(symbol, breakout, tick)
        
        # Update existing positions
        await self._update_positions(symbol, tick)
    
    async def _handle_breakout(self, symbol: str, breakout: BreakoutLevel, tick: MarketTick):
        """Handle detected breakout with improved entry logic"""
        if not self.risk_manager.can_trade():
            logger.warning("Risk manager blocked trade")
            return
        
        if symbol in self.positions:
            logger.info(f"Already have position in {symbol}")
            return
        
        # Additional filters for scalping
        if not self._is_good_scalping_time():
            logger.info(f"Not ideal scalping time for {symbol}")
            return
        
        # Check spread - avoid wide spreads
        spread = tick.ask - tick.bid
        if spread > tick.price * 0.0005:  # 0.05% max spread
            logger.info(f"Spread too wide for {symbol}: {spread}")
            return
        
        # Determine trade direction based on breakout and current price action
        if breakout.price > tick.price:
            # Bullish breakout - wait for price to get close to ask
            if tick.price < (tick.bid + tick.ask) / 2:
                return
            side = OrderSide.BUY
            entry_price = tick.ask  # Market buy at ask
        else:
            # Bearish breakout - wait for price to get close to bid
            if tick.price > (tick.bid + tick.ask) / 2:
                return
            side = OrderSide.SELL
            entry_price = tick.bid  # Market sell at bid
        
        # Calculate position size and stops
        stop_loss, take_profit = self.risk_manager.calculate_stops(
            entry_price, side, breakout.strength
        )
        
        position_size = self.risk_manager.calculate_position_size(
            self.account_balance, entry_price, stop_loss, symbol
        )
        
        if position_size <= 0:
            logger.info(f"Position size too small for {symbol}")
            return
        
        # Place order
        order_result = await self.api.place_order(
            symbol, side, position_size, OrderType.MARKET
        )
        
        if order_result:
            position = Position(
                symbol=symbol,
                side=side,
                quantity=position_size,
                entry_price=entry_price,
                timestamp=datetime.now(),
                stop_loss=stop_loss,
                take_profit=take_profit
            )
            
            self.positions[symbol] = position
            logger.info(f"🚀 Opened {side.value} position in {symbol}: {position_size} @ {entry_price:.2f} (Strength: {breakout.strength:.2f})")
    
    def _is_good_scalping_time(self) -> bool:
        """Check if current time is good for scalping"""
        now = datetime.now()
        hour = now.hour
        
        # Avoid low-volume periods
        # Good times: 8:30-11:30 AM, 1:00-4:00 PM EST
        if (8 <= hour <= 11) or (13 <= hour <= 16):
            return True
        
        return False
    
    async def _update_positions(self, symbol: str, tick: MarketTick):
        """Update existing positions with real-time tick data"""
        if symbol not in self.positions:
            return
        
        position = self.positions[symbol]
        current_price = tick.price
        
        # Use bid/ask for more accurate exit prices
        if position.side == OrderSide.BUY:
            # For long positions, we'd sell at bid
            exit_price = tick.bid
        else:
            # For short positions, we'd buy at ask
            exit_price = tick.ask
        
        # Check stop loss
        if ((position.side == OrderSide.BUY and exit_price <= position.stop_loss) or
            (position.side == OrderSide.SELL and exit_price >= position.stop_loss)):
            await self._close_position(position, "Stop Loss", exit_price)
        
        # Check take profit
        elif ((position.side == OrderSide.BUY and exit_price >= position.take_profit) or
              (position.side == OrderSide.SELL and exit_price <= position.take_profit)):
            await self._close_position(position, "Take Profit", exit_price)
    
    async def _close_position(self, position: Position, reason: str = "Manual", exit_price: float = None):
        """Close a position"""
        opposite_side = OrderSide.SELL if position.side == OrderSide.BUY else OrderSide.BUY
        
        order_result = await self.api.place_order(
            position.symbol, opposite_side, position.quantity, OrderType.MARKET
        )
        
        if order_result:
            # Use provided exit price or order fill price
            final_exit_price = exit_price or order_result.get('fill_price', position.entry_price)
            
            # Calculate P&L
            if position.side == OrderSide.BUY:
                pnl = (final_exit_price - position.entry_price) * position.quantity
            else:
                pnl = (position.entry_price - final_exit_price) * position.quantity
            
            self.risk_manager.update_pnl(pnl)
            del self.positions[position.symbol]
            
            logger.info(f"✅ Closed {position.side.value} position in {position.symbol}: {reason}, P&L: ${pnl:.2f}")
    
    async def _monitor_positions(self):
        """Monitor positions and market conditions"""
        while self.running:
            try:
                # Log current positions
                if self.positions:
                    for symbol, position in self.positions.items():
                        if symbol in self.latest_ticks:
                            tick = self.latest_ticks[symbol]
                            unrealized_pnl = self._calculate_unrealized_pnl(position, tick)
                            logger.info(f"📊 {symbol}: {position.side.value} @ {position.entry_price:.2f}, Current: {tick.price:.2f}, P&L: ${unrealized_pnl:.2f}")
                
                # Log daily P&L
                if self.risk_manager.daily_pnl != 0:
                    logger.info(f"💰 Daily P&L: ${self.risk_manager.daily_pnl:.2f}")
                
                await asyncio.sleep(30)  # Update every 30 seconds
                
            except Exception as e:
                logger.error(f"Error in position monitor: {e}")
                await asyncio.sleep(5)
    
    def _calculate_unrealized_pnl(self, position: Position, tick: MarketTick) -> float:
        """Calculate unrealized P&L for a position"""
        if position.side == OrderSide.BUY:
            return (tick.bid - position.entry_price) * position.quantity
        else:
            return (position.entry_price - tick.ask) * position.quantitySELL
        
        # Calculate position size and stops
        stop_loss, take_profit = self.risk_manager.calculate_stops(
            current_price, side, breakout.strength
        )
        
        position_size = self.risk_manager.calculate_position_size(
            self.account_balance, current_price, stop_loss, symbol
        )
        
        if position_size <= 0:
            return
        
        # Place order
        order_result = await self.api.place_order(
            symbol, side, position_size, OrderType.MARKET
        )
        
        if order_result:
            position = Position(
                symbol=symbol,
                side=side,
                quantity=position_size,
                entry_price=current_price,
                timestamp=datetime.now(),
                stop_loss=stop_loss,
                take_profit=take_profit
            )
            
            self.positions[symbol] = position
            logger.info(f"Opened {side.value} position in {symbol}: {position_size} @ {current_price}")
    
    async def _update_positions(self, symbol: str, current_price: float):
        """Update existing positions"""
        if symbol not in self.positions:
            return
        
        position = self.positions[symbol]
        
        # Check stop loss
        if ((position.side == OrderSide.BUY and current_price <= position.stop_loss) or
            (position.side == OrderSide.SELL and current_price >= position.stop_loss)):
            await self._close_position(position, "Stop Loss")
        
        # Check take profit
        elif ((position.side == OrderSide.BUY and current_price >= position.take_profit) or
              (position.side == OrderSide.SELL and current_price <= position.take_profit)):
            await self._close_position(position, "Take Profit")
    
    async def _close_position(self, position: Position, reason: str = "Manual"):
        """Close a position"""
        opposite_side = OrderSide.SELL if position.side == OrderSide.BUY else OrderSide.BUY
        
        order_result = await self.api.place_order(
            position.symbol, opposite_side, position.quantity, OrderType.MARKET
        )
        
        if order_result:
            # Calculate P&L (simplified)
            if position.side == OrderSide.BUY:
                pnl = (order_result.get('fill_price', 0) - position.entry_price) * position.quantity
            else:
                pnl = (position.entry_price - order_result.get('fill_price', 0)) * position.quantity
            
            self.risk_manager.update_pnl(pnl)
            del self.positions[position.symbol]
            
            logger.info(f"Closed position in {position.symbol}: {reason}, P&L: ${pnl:.2f}")

# Example usage with TradingView integration
async def main():
    # Initialize bot with your credentials
    bot = FuturesScalpingBot(
        api_key="your_topstep_api_key",
        secret="your_topstep_secret",
        tv_username="your_tradingview_username",
        tv_password="your_tradingview_password",
        symbols=["ES", "NQ", "YM", "RTY"]  # Major e-mini futures
    )
    
    try:
        logger.info("🚀 Starting TradingView-powered futures scalping bot...")
        
        # Print initial configuration
        logger.info(f"Symbols: {bot.symbols}")
        logger.info(f"Risk per trade: {bot.risk_manager.max_risk_per_trade*100:.1f}%")
        logger.info(f"Max daily loss: {bot.risk_manager.max_daily_loss*100:.1f}%")
        logger.info(f"Breakout confidence threshold: 65%")
        
        await bot.start()
        
    except KeyboardInterrupt:
        logger.info("🛑 Bot interrupted by user")
    except Exception as e:
        logger.error(f"❌ Bot error: {e}")
    finally:
        await bot.stop()
        logger.info("✅ Bot stopped successfully")

if __name__ == "__main__":
    asyncio.run(main())
