"""
MEV 夹子攻击诊断模块
-------------------
监听 Uniswap V2/V3 (及 Pancakeswap) 的 Swap 事件，
在同一个区块内检测「前跑 Buy + 后跑 Sell」的夹子攻击模式，
计算攻击者利润与受害者滑点损失。
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
# 常量
# ---------------------------------------------------------------------------
WETH_ETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"   # Ethereum WETH
WETH_BSC = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"   # BSC WBNB

# 事件签名（运行时用 keccak 计算，避免硬编码错误）
V2_SWAP_SIG = "Swap(address,uint256,uint256,uint256,uint256,address)"
V3_SWAP_SIG = "Swap(address,address,int256,int256,uint160,uint128,int24)"

# 读取 pair/pool 代币信息的最小 ABI
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
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class SwapRecord:
    """单笔 Swap 事件的解析结果"""
    tx_hash: str
    log_index: int
    pair: str                 # 交易对/pool 地址
    trader: str               # 接收输出代币的地址 (to / recipient)
    token_in: str             # 输入代币地址
    token_out: str            # 输出代币地址
    amount_in: int            # 输入数量（最小单位）
    amount_out: int           # 输出数量（最小单位）
    is_buy: bool              # True = 用 WETH 买入目标代币
    version: int              # 2 = Uniswap V2, 3 = V3


@dataclass
class SandwichReport:
    """夹子攻击诊断报告"""
    block_number: int
    victim_tx: str
    attacker: str
    pair: str
    front_run_tx: str
    back_run_tx: str
    attacker_profit_native: float   # 攻击者利润（ETH/BNB）
    victim_loss_native: float       # 受害者预估损失（ETH/BNB）
    token_symbol: str
    token_amount: float             # 被夹交易涉及的代币数量
    native_symbol: str              # "ETH" / "BNB"
    chain: str                      # "ethereum" / "bsc"
    scan_url: str = ""              # 区块浏览器链接前缀
    victim_address: str = ""        # 受害者钱包地址（用于订阅匹配/触达）
    token_address: str = ""         # 被夹代币合约地址


# ---------------------------------------------------------------------------
# 分析器
# ---------------------------------------------------------------------------
class MEVAnalyzer:
    """链上夹子攻击分析器"""

    def __init__(self, w3: Web3, chain: str = "ethereum"):
        self.w3 = w3
        self.chain = chain
        self.native_symbol = "ETH" if chain == "ethereum" else "BNB"
        self.weth = (WETH_ETH if chain == "ethereum" else WETH_BSC).lower()

        # 计算事件 topic（keccak256），需带 0x 前缀
        self.v2_swap_topic = Web3.to_hex(Web3.keccak(text=V2_SWAP_SIG))
        self.v3_swap_topic = Web3.to_hex(Web3.keccak(text=V3_SWAP_SIG))

        # 缓存：pair -> (token0, token1)
        self._pair_tokens: Dict[str, Tuple[str, str]] = {}
        # 缓存：token -> (symbol, decimals)
        self._token_info: Dict[str, Tuple[str, int]] = {}

        self.scan_url = (
            "https://etherscan.io" if chain == "ethereum" else "https://bscscan.com"
        )

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    def _to_addr(self, topic_hex: str) -> str:
        """从 32 字节 topic 中提取地址（去掉前导零）"""
        return Web3.to_checksum_address("0x" + topic_hex[-40:])

    def _get_pair_tokens(self, pair: str) -> Tuple[str, str]:
        """获取 pair 的 token0 / token1（带缓存）"""
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
            logger.debug("读取 pair 代币信息失败 %s: %s", pair, e)
            self._pair_tokens[key] = ("", "")
        return self._pair_tokens[key]

    def _get_token_info(self, token: str) -> Tuple[str, int]:
        """获取代币 symbol / decimals（带缓存）"""
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
            logger.debug("读取代币信息失败 %s: %s", token, e)
            self._token_info[key] = ("TOKEN", 18)
        return self._token_info[key]

    # ------------------------------------------------------------------
    # 日志拉取与解析
    # ------------------------------------------------------------------
    def fetch_swaps(self, block_number: int) -> List[SwapRecord]:
        """拉取指定区块内所有 Uniswap V2/V3 Swap 事件并解析"""
        swaps: List[SwapRecord] = []

        # 同时查询 V2 和 V3 的 Swap 事件
        filter_params = {
            "fromBlock": block_number,
            "toBlock": block_number,
            "topics": [[self.v2_swap_topic, self.v3_swap_topic]],
        }

        try:
            logs: List[LogReceipt] = self.w3.eth.get_logs(filter_params)
        except Exception as e:
            logger.error("拉取区块 %d 日志失败: %s", block_number, e)
            return swaps

        if not logs:
            return swaps

        # 并行预热所有唯一交易对的 token0/token1（避免逐个同步调用拖慢速度）
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
                logger.debug("解析 swap 日志失败: %s", e)
                continue

        logger.debug("区块 %d 解析到 %d 笔 swap", block_number, len(swaps))
        return swaps

    def _prefetch_pair_tokens(self, pairs: List[str]) -> None:
        """并行获取多个交易对的 token0/token1，填充缓存"""
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
        """解析 Uniswap V2 Swap 事件"""
        data = log["data"]
        # amount0In, amount1In, amount0Out, amount1Out 各 32 字节
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

        # 判断哪个是 WETH，并分类买卖
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
            # 非 WETH 交易对，跳过
            return None

        is_buy = weth_in > 0  # 输入 WETH = 买入目标代币
        amount_in = weth_in if is_buy else (amount0_in if token0 != self.weth else amount1_in)
        amount_out = (amount0_out if token0 != self.weth else amount1_out) if is_buy else weth_out

        # to 地址在 topics[2]
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
        """解析 Uniswap V3 Swap 事件"""
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

        # V3: 正数表示池子收入（用户支出），负数表示池子支出（用户收入）
        if token1 == self.weth:
            weth_amount = amount1
            token_amount = amount0
        elif token0 == self.weth:
            weth_amount = amount0
            token_amount = amount1
        else:
            return None

        # weth_amount < 0 表示用户收到 WETH（卖出目标代币）
        # weth_amount > 0 表示用户支付 WETH（买入目标代币）
        is_buy = weth_amount > 0

        # recipient 在 topics[2]
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
    # 夹子攻击检测
    # ------------------------------------------------------------------
    def detect_sandwiches(self, swaps: List[SwapRecord]) -> List[SandwichReport]:
        """
        检测夹子攻击：
        同一交易对、同一区块内，同一地址先 Buy（前跑）再 Sell（后跑），
        且两者之间存在受害者交易。
        """
        reports: List[SandwichReport] = []

        # 按交易对分组
        by_pair: Dict[str, List[SwapRecord]] = {}
        for s in swaps:
            by_pair.setdefault(s.pair, []).append(s)

        for pair, pair_swaps in by_pair.items():
            # 按 log_index 排序（区块内执行顺序）
            pair_swaps.sort(key=lambda x: x.log_index)

            # 按交易者分组
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
                        # 寻找 buy 与 sell 之间的受害者交易
                        victims = [
                            s for s in pair_swaps
                            if buy.log_index < s.log_index < sell.log_index
                            and s.trader != attacker
                        ]
                        if not victims:
                            continue

                        # 取中间最大的一笔作为主要受害者
                        victim = max(victims, key=lambda v: v.amount_in)

                        # 计算攻击者利润（WETH 单位）
                        profit_raw = sell.amount_out - buy.amount_in
                        profit_eth = profit_raw / 1e18

                        if profit_eth <= 0:
                            continue  # 无利润则不是有效夹子

                        # 代币信息
                        token_addr = buy.token_out
                        symbol, decimals = self._get_token_info(token_addr)
                        token_amount = victim.amount_out / (10 ** decimals) if victim.is_buy else victim.amount_in / (10 ** decimals)

                        # 受害者损失估算 ≈ 攻击者利润（简化模型）
                        victim_loss = profit_eth * 0.9  # 扣除大约手续费后的净损失

                        report = SandwichReport(
                            block_number=0,  # 由调用方填充
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
                        break  # 每个 buy 只匹配最早的 sell

        return reports
