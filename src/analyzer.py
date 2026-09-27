"""
MEV Sandwich Attack Detection Module
------------------------------------
Monitors Uniswap V2/V3 (and Pancakeswap) Swap events,
detects front-run Buy + back-run Sell patterns within the same block,
and calculates attacker profit and victim slippage loss.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from web3 import Web3
from web3.types import LogReceipt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WETH_ETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"   # Ethereum WETH
WETH_BSC = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"   # BSC WBNB

# Event signatures (computed via keccak at runtime to avoid hardcoding errors)
V2_SWAP_SIG = "Swap(address,uint256,uint256,uint256,uint256,address)"
V3_SWAP_SIG = "Swap(address,address,int256,int256,uint160,uint128,int24)"

# Minimal ABI for reading pair/pool token info
PAIR_ABI = [
    {"constant": True, "inputs": [], "name": "token0",
     "outputs": [{"name": "", "type": "address"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "token1",
     "outputs": [{"name": "", "type": "address"}], "type": "function"},
]

ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol",
     "outputs": [{"name": "", "type": "string"}], "type": "function"},
]


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------
@dataclass
class SwapRecord:
    """Parsed result of a single Swap event."""
    tx_hash: str
    log_index: int
    pair: str                 # Pair/pool address
    trader: str               # Recipient of output token (to / recipient)
    token_in: str             # Input token address
    token_out: str            # Output token address
    amount_in: int            # Input amount (smallest unit)
    amount_out: int           # Output amount (smallest unit)
    is_buy: bool              # True = buying target token with WETH
    version: int              # 2 = Uniswap V2, 3 = V3


@dataclass
class SandwichReport:
    """Sandwich attack diagnostic report."""
    block_number: int
    victim_tx: str
    attacker: str
    pair: str
    front_run_tx: str
    back_run_tx: str
    attacker_profit_native: float   # Attacker profit (ETH/BNB)
    victim_loss_native: float       # Estimated victim loss (ETH/BNB)
    token_symbol: str
    token_amount: float             # Token amount involved in the sandwiched trade
    native_symbol: str              # "ETH" / "BNB"
    chain: str                      # "ethereum" / "bsc"
    scan_url: str = ""              # Block explorer URL prefix
    victim_address: str = ""        # Victim wallet address (for subscription matching / outreach)
    token_address: str = ""         # Sandwiched token contract address


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------
class MEVAnalyzer:
    """On-chain sandwich attack analyzer."""

    def __init__(self, w3: Web3, chain: str = "ethereum"):
        self.w3 = w3
        self.chain = chain
        self.native_symbol = "ETH" if chain == "ethereum" else "BNB"
        self.weth = (WETH_ETH if chain == "ethereum" else WETH_BSC).lower()

        # Compute event topics via keccak256, with 0x prefix
        self.v2_swap_topic = Web3.to_hex(Web3.keccak(text=V2_SWAP_SIG))
        self.v3_swap_topic = Web3.to_hex(Web3.keccak(text=V3_SWAP_SIG))

        # Cache: pair -> (token0, token1)
        self._pair_tokens: Dict[str, Tuple[str, str]] = {}
        # Cache: token -> (symbol, decimals)
        self._token_info: Dict[str, Tuple[str, int]] = {}

        self.scan_url = (
            "https://etherscan.io" if chain == "ethereum" else "https://bscscan.com"
        )

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------
    def _to_addr(self, topic_hex: str) -> str:
        """Extract an address from a 32-byte topic (strip leading zeros)."""
        return Web3.to_checksum_address("0x" + topic_hex[-40:])

    def _get_pair_tokens(self, pair: str) -> Tuple[str, str]:
        """Fetch token0 / token1 for a pair (cached)."""
        key = pair.lower()
        if key in self._pair_tokens:
            return self._pair_tokens[key]
        try:
            checksum_pair = Web3.to_checksum_address(pair)
            contract = self.w3.eth.contract(address=checksum_pair, abi=PAIR_ABI)
            t0 = contract.functions.token0().call()
            t1 = contract.functions.token1().call()
            self._pair_tokens[key] = (t0.lower(), t1.lower())
        except Exception as e:
            logger.debug("Failed to read pair tokens for %s: %s", pair, e)
            self._pair_tokens[key] = ("", "")
        return self._pair_tokens[key]

    def _get_token_info(self, token: str) -> Tuple[str, int]:
        """Fetch token symbol / decimals (cached)."""
        key = token.lower()
        if key in self._token_info:
            return self._token_info[key]
        try:
            checksum_token = Web3.to_checksum_address(token)
            contract = self.w3.eth.contract(address=checksum_token, abi=ERC20_ABI)
            symbol = contract.functions.symbol().call()
            decimals = contract.functions.decimals().call()
            self._token_info[key] = (str(symbol), int(decimals))
        except Exception as e:
            logger.debug("Failed to read token info for %s: %s", token, e)
            self._token_info[key] = ("TOKEN", 18)
        return self._token_info[key]

    # ------------------------------------------------------------------
    # Log fetching and parsing
    # ------------------------------------------------------------------
    def fetch_swaps(self, block_number: int) -> List[SwapRecord]:
        """Fetch all Uniswap V2/V3 Swap events in a given block and parse them."""
        swaps: List[SwapRecord] = []

        # Query both V2 and V3 Swap events simultaneously
        filter_params = {
            "fromBlock": block_number,
            "toBlock": block_number,
            "topics": [[self.v2_swap_topic, self.v3_swap_topic]],
        }

        try:
            logs: List[LogReceipt] = self.w3.eth.get_logs(filter_params)
        except Exception as e:
            logger.error("Failed to fetch logs for block %d: %s", block_number, e)
            return swaps

        if not logs:
            return swaps

        # Parallel prefetch of token0/token1 for all unique pairs (avoids slow sequential calls)
        unique_pairs = list({log["address"].lower() for log in logs})
        self._prefetch_pair_tokens(unique_pairs)

        for log in logs:
            try:
                topic0 = Web3.to_hex(log["topics"][0])
                if topic0 == self.v2_swap_topic:
                    rec = self._parse_v2_swap(log)
                elif topic0 == self.v3_swap_topic:
                    rec = self._parse_v3_swap(log)
                else:
                    continue
                if rec is not None:
                    swaps.append(rec)
            except Exception as e:
                logger.debug("Failed to parse swap log: %s", e)
                continue

        logger.debug("Block %d: parsed %d swaps", block_number, len(swaps))
        return swaps

    def _prefetch_pair_tokens(self, pairs: List[str]) -> None:
        """Fetch token0/token1 for multiple pairs in parallel, populating the cache."""
        missing = [p for p in pairs if p.lower() not in self._pair_tokens]
        if not missing:
            return

        def _fetch(pair: str) -> Tuple[str, Tuple[str, str]]:
            return pair, self._get_pair_tokens(pair)

        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = {pool.submit(_fetch, p): p for p in missing}
            for fut in as_completed(futures):
                try:
                    pair, tokens = fut.result()
                    self._pair_tokens[pair.lower()] = tokens
                except Exception:
                    pass

    def _parse_v2_swap(self, log: LogReceipt) -> Optional[SwapRecord]:
        """Parse a Uniswap V2 Swap event."""
        data = log["data"]
        # amount0In, amount1In, amount0Out, amount1Out — each 32 bytes
        if len(data) < 128:
            return None
        amount0_in = int.from_bytes(data[0:32], "big")
        amount1_in = int.from_bytes(data[32:64], "big")
        amount0_out = int.from_bytes(data[64:96], "big")
        amount1_out = int.from_bytes(data[96:128], "big")

        pair = log["address"]
        token0, token1 = self._get_pair_tokens(pair)
        if not token0 or not token1:
            return None

        # Determine which token is WETH and classify buy/sell
        if token1 == self.weth:
            weth_in = amount1_in
            weth_out = amount1_out
            token_in_addr = token0 if amount0_in > 0 else token1
            token_out_addr = token1 if amount1_out > 0 else token0
        elif token0 == self.weth:
            weth_in = amount0_in
            weth_out = amount0_out
            token_in_addr = token1 if amount1_in > 0 else token0
            token_out_addr = token0 if amount0_out > 0 else token1
        else:
            # Non-WETH pair, skip
            return None

        is_buy = weth_in > 0  # WETH input = buying target token
        amount_in = weth_in if is_buy else (amount0_in if token0 != self.weth else amount1_in)
        amount_out = (amount0_out if token0 != self.weth else amount1_out) if is_buy else weth_out

        # to address is in topics[2]
        to_addr = self._to_addr(log["topics"][2].hex())

        return SwapRecord(
            tx_hash=log["transactionHash"].hex(),
            log_index=log["logIndex"],
            pair=pair.lower(),
            trader=to_addr.lower(),
            token_in=token_in_addr,
            token_out=token_out_addr,
            amount_in=amount_in,
            amount_out=amount_out,
            is_buy=is_buy,
            version=2,
        )

    def _parse_v3_swap(self, log: LogReceipt) -> Optional[SwapRecord]:
        """Parse a Uniswap V3 Swap event."""
        data = log["data"]
        # amount0 (int256), amount1 (int256), sqrtPriceX96, liquidity, tick
        if len(data) < 160:
            return None
        amount0 = int.from_bytes(data[0:32], "big", signed=True)
        amount1 = int.from_bytes(data[32:64], "big", signed=True)

        pair = log["address"]
        token0, token1 = self._get_pair_tokens(pair)
        if not token0 or not token1:
            return None

        # V3: positive = pool receives (user pays), negative = pool sends (user receives)
        if token1 == self.weth:
            weth_amount = amount1
            token_amount = amount0
        elif token0 == self.weth:
            weth_amount = amount0
            token_amount = amount1
        else:
            return None

        # weth_amount < 0 means user received WETH (selling target token)
        # weth_amount > 0 means user paid WETH (buying target token)
        is_buy = weth_amount > 0

        # recipient is in topics[2]
        recipient = self._to_addr(log["topics"][2].hex())

        return SwapRecord(
            tx_hash=log["transactionHash"].hex(),
            log_index=log["logIndex"],
            pair=pair.lower(),
            trader=recipient.lower(),
            token_in=token0 if (amount0 > 0) else token1,
            token_out=token1 if (amount0 > 0) else token0,
            amount_in=abs(weth_amount) if is_buy else abs(token_amount),
            amount_out=abs(token_amount) if is_buy else abs(weth_amount),
            is_buy=is_buy,
            version=3,
        )

    # ------------------------------------------------------------------
    # Sandwich attack detection
    # ------------------------------------------------------------------
    def detect_sandwiches(self, swaps: List[SwapRecord]) -> List[SandwichReport]:
        """
        Detect sandwich attacks:
        Same pair, same block, same address does Buy (front-run) then Sell (back-run),
        with a victim transaction in between.
        """
        reports: List[SandwichReport] = []

        # Group by pair
        by_pair: Dict[str, List[SwapRecord]] = {}
        for s in swaps:
            by_pair.setdefault(s.pair, []).append(s)

        for pair, pair_swaps in by_pair.items():
            # Sort by log_index (execution order within block)
            pair_swaps.sort(key=lambda x: x.log_index)

            # Group by trader
            by_trader: Dict[str, List[SwapRecord]] = {}
            for s in pair_swaps:
                by_trader.setdefault(s.trader, []).append(s)

            for attacker, trader_swaps in by_trader.items():
                buys = [s for s in trader_swaps if s.is_buy]
                sells = [s for s in trader_swaps if not s.is_buy]
                if not buys or not sells:
                    continue

                for buy in buys:
                    for sell in sells:
                        if sell.log_index <= buy.log_index:
                            continue
                        # Find victim transactions between front-run and back-run
                        victims = [
                            s for s in pair_swaps
                            if buy.log_index < s.log_index < sell.log_index
                            and s.trader != attacker
                        ]
                        if not victims:
                            continue

                        # Take the largest intermediate trade as the primary victim
                        victim = max(victims, key=lambda v: v.amount_in)

                        # Calculate attacker profit (in WETH units)
                        profit_raw = sell.amount_out - buy.amount_in
                        profit_eth = profit_raw / 1e18

                        if profit_eth <= 0:
                            continue  # No profit = not a valid sandwich

                        # Token info
                        token_addr = buy.token_out
                        symbol, decimals = self._get_token_info(token_addr)
                        token_amount = victim.amount_out / (10 ** decimals) if victim.is_buy else victim.amount_in / (10 ** decimals)

                        # Victim loss estimate ≈ attacker profit (simplified model)
                        victim_loss = profit_eth * 0.9  # Net loss after gas fees

                        report = SandwichReport(
                            block_number=0,  # Filled by caller
                            victim_tx=victim.tx_hash,
                            attacker=attacker,
                            pair=pair,
                            front_run_tx=buy.tx_hash,
                            back_run_tx=sell.tx_hash,
                            attacker_profit_native=round(profit_eth, 6),
                            victim_loss_native=round(victim_loss, 6),
                            token_symbol=symbol,
                            token_amount=round(token_amount, 4),
                            native_symbol=self.native_symbol,
                            chain=self.chain,
                            scan_url=self.scan_url,
                            victim_address=victim.trader,
                            token_address=token_addr,
                        )
                        reports.append(report)
                        break  # Each buy only matches the earliest sell

        return reports
