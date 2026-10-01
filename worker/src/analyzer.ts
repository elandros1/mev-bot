/**
 * MEV sandwich attack detection — TypeScript port of src/analyzer.py.
 *
 * Monitors Uniswap V2/V3 (and Pancakeswap) Swap events, detects
 * front-run Buy + back-run Sell patterns within the same block, and
 * calculates attacker profit and victim slippage loss.
 *
 * Designed to run inside a Cloudflare Worker with a JSON-RPC provider
 * (no WebSocket subscriptions needed — uses eth_getLogs per block).
 */
import { ethers, JsonRpcProvider } from "ethers";
import type { SwapRecord, SandwichReport } from "./types";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const WETH_ETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2";   // Ethereum WETH
const WETH_BSC = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c";   // BSC WBNB

const V2_SWAP_SIG = "Swap(address,uint256,uint256,uint256,uint256,address)";
const V3_SWAP_SIG = "Swap(address,address,int256,int256,uint160,uint128,int24)";

// Minimal ABIs for reading pair/pool token info
const PAIR_ABI = [
  "function token0() view returns (address)",
  "function token1() view returns (address)",
];
const ERC20_ABI = [
  "function decimals() view returns (uint8)",
  "function symbol() view returns (string)",
];

/**
 * Compute keccak256 topic hash for an event signature, with 0x prefix.
 * ethers v6 has id() that does exactly this.
 */
function eventTopic(sig: string): string {
  return ethers.id(sig);
}

/**
 * Extract an address (checksummed) from a 32-byte topic word.
 * ethers v6: topic is a hex string; last 20 bytes are the address.
 */
function topicToAddress(topic: string): string {
  return ethers.getAddress("0x" + topic.slice(-40));
}

/**
 * The on-chain sandwich analyzer.
 *
 * Stateless across invocations — per-call caches (pair tokens, token info)
 * are kept on the instance so a single block scan can reuse lookups.
 */
export class MEVAnalyzer {
  readonly provider: JsonRpcProvider;
  readonly chain: string;
  readonly nativeSymbol: string;
  readonly weth: string;
  readonly v2SwapTopic: string;
  readonly v3SwapTopic: string;
  readonly scanUrl: string;

  private pairTokens = new Map<string, [string, string]>();
  private tokenInfo = new Map<string, [string, number]>();

  constructor(provider: JsonRpcProvider, chain = "ethereum") {
    this.provider = provider;
    this.chain = chain;
    this.nativeSymbol = chain === "ethereum" ? "ETH" : "BNB";
    this.weth = (chain === "ethereum" ? WETH_ETH : WETH_BSC).toLowerCase();
    this.v2SwapTopic = eventTopic(V2_SWAP_SIG);
    this.v3SwapTopic = eventTopic(V3_SWAP_SIG);
    this.scanUrl = chain === "ethereum"
      ? "https://etherscan.io"
      : "https://bscscan.com";
  }

  // -------------------------------------------------------------------
  // Token / pair info (cached per instance)
  // -------------------------------------------------------------------
  private async getPairTokens(pair: string): Promise<[string, string]> {
    const key = pair.toLowerCase();
    const cached = this.pairTokens.get(key);
    if (cached) return cached;
    try {
      const contract = new ethers.Contract(
        ethers.getAddress(pair), PAIR_ABI, this.provider,
      );
      const [t0, t1] = await Promise.all([
        contract.token0() as Promise<string>,
        contract.token1() as Promise<string>,
      ]);
      const result: [string, string] = [
        t0.toLowerCase(), t1.toLowerCase(),
      ];
      this.pairTokens.set(key, result);
      return result;
    } catch (e) {
      this.pairTokens.set(key, ["", ""]);
      return ["", ""];
    }
  }

  private async getTokenInfo(token: string): Promise<[string, number]> {
    const key = token.toLowerCase();
    const cached = this.tokenInfo.get(key);
    if (cached) return cached;
    try {
      const contract = new ethers.Contract(
        ethers.getAddress(token), ERC20_ABI, this.provider,
      );
      const [symbol, decimals] = await Promise.all([
        contract.symbol() as Promise<string>,
        contract.decimals() as Promise<number>,
      ]);
      const result: [string, number] = [symbol, decimals];
      this.tokenInfo.set(key, result);
      return result;
    } catch (e) {
      const fallback: [string, number] = ["TOKEN", 18];
      this.tokenInfo.set(key, fallback);
      return fallback;
    }
  }

  /** Prefetch token0/token1 for all unique pairs in parallel. */
  private async prefetchPairTokens(pairs: string[]): Promise<void> {
    const missing = pairs.filter((p) => !this.pairTokens.has(p.toLowerCase()));
    if (missing.length === 0) return;
    await Promise.all(
      missing.map(async (p) => {
        await this.getPairTokens(p);
      }),
    );
  }

  // -------------------------------------------------------------------
  // Log fetching and parsing
  // -------------------------------------------------------------------
  /**
   * Fetch all Uniswap V2/V3 Swap events in a given block and parse them.
   * Uses eth_getLogs with topic0 = [v2, v3].
   */
  async fetchSwaps(blockNumber: number): Promise<SwapRecord[]> {
    const swaps: SwapRecord[] = [];
    let logs: ethers.Log[] = [];
    try {
      logs = await this.provider.getLogs({
        fromBlock: blockNumber,
        toBlock: blockNumber,
        topics: [[this.v2SwapTopic, this.v3SwapTopic]],
      });
    } catch (e) {
      console.error(`Failed to fetch logs for block ${blockNumber}:`, e);
      return swaps;
    }
    if (logs.length === 0) return swaps;

    // Prefetch pair tokens in parallel
    const uniquePairs = [...new Set(logs.map((l) => l.address.toLowerCase()))];
    await this.prefetchPairTokens(uniquePairs);

    for (const log of logs) {
      try {
        const topic0 = log.topics[0];
        if (topic0 === this.v2SwapTopic) {
          const rec = await this.parseV2Swap(log);
          if (rec) swaps.push(rec);
        } else if (topic0 === this.v3SwapTopic) {
          const rec = await this.parseV3Swap(log);
          if (rec) swaps.push(rec);
        }
      } catch (e) {
        // Skip malformed logs
      }
    }
    return swaps;
  }

  /** Parse a Uniswap V2 Swap event. */
  private async parseV2Swap(log: ethers.Log): Promise<SwapRecord | null> {
    const data = log.data;
    // amount0In, amount1In, amount0Out, amount1Out — each 32 bytes
    if (data.length < 2 + 128) return null; // 0x + 4 * 32 bytes

    const rawHex = data.slice(2);
    const amount0In = BigInt("0x" + rawHex.slice(0, 64));
    const amount1In = BigInt("0x" + rawHex.slice(64, 128));
    const amount0Out = BigInt("0x" + rawHex.slice(128, 192));
    const amount1Out = BigInt("0x" + rawHex.slice(192, 256));

    const pair = log.address;
    const [token0, token1] = await this.getPairTokens(pair);
    if (!token0 || !token1) return null;

    let wethIn: bigint, wethOut: bigint;
    let tokenInAddr: string, tokenOutAddr: string;
    if (token1 === this.weth) {
      wethIn = amount1In;
      wethOut = amount1Out;
      tokenInAddr = amount0In > 0n ? token0 : token1;
      tokenOutAddr = amount1Out > 0n ? token1 : token0;
    } else if (token0 === this.weth) {
      wethIn = amount0In;
      wethOut = amount0Out;
      tokenInAddr = amount1In > 0n ? token1 : token0;
      tokenOutAddr = amount0Out > 0n ? token0 : token1;
    } else {
      // Non-WETH pair, skip
      return null;
    }

    const isBuy = wethIn > 0n;
    const amountIn = isBuy
      ? wethIn
      : (token0 !== this.weth ? amount0In : amount1In);
    const amountOut = isBuy
      ? (token0 !== this.weth ? amount0Out : amount1Out)
      : wethOut;

    const toAddr = topicToAddress(log.topics[2]);

    return {
      txHash: log.transactionHash,
      logIndex: log.index,
      pair: pair.toLowerCase(),
      trader: toAddr.toLowerCase(),
      tokenIn: tokenInAddr,
      tokenOut: tokenOutAddr,
      amountIn,
      amountOut,
      isBuy,
      version: 2,
    };
  }

  /** Parse a Uniswap V3 Swap event. */
  private async parseV3Swap(log: ethers.Log): Promise<SwapRecord | null> {
    const data = log.data;
    // amount0 (int256), amount1 (int256), sqrtPriceX96, liquidity, tick
    if (data.length < 2 + 160) return null;
    const rawHex = data.slice(2);
    // int256 (two's complement)
    const amount0 = fromTwos("0x" + rawHex.slice(0, 64));
    const amount1 = fromTwos("0x" + rawHex.slice(64, 128));

    const pair = log.address;
    const [token0, token1] = await this.getPairTokens(pair);
    if (!token0 || !token1) return null;

    let wethAmount: bigint, tokenAmount: bigint;
    if (token1 === this.weth) {
      wethAmount = amount1;
      tokenAmount = amount0;
    } else if (token0 === this.weth) {
      wethAmount = amount0;
      tokenAmount = amount1;
    } else {
      return null;
    }

    // V3: positive = pool receives (user pays), negative = pool sends
    // wethAmount > 0 => user paid WETH (buying target token)
    const isBuy = wethAmount > 0n;

    const recipient = topicToAddress(log.topics[2]);

    return {
      txHash: log.transactionHash,
      logIndex: log.index,
      pair: pair.toLowerCase(),
      trader: recipient.toLowerCase(),
      tokenIn: amount0 > 0n ? token0 : token1,
      tokenOut: amount0 > 0n ? token1 : token0,
      amountIn: isBuy ? abs(wethAmount) : abs(tokenAmount),
      amountOut: isBuy ? abs(tokenAmount) : abs(wethAmount),
      isBuy,
      version: 3,
    };
  }

  // -------------------------------------------------------------------
  // Sandwich detection
  // -------------------------------------------------------------------
  /**
   * Detect sandwich attacks in a list of swaps within the same block.
   *
   * Pattern: same trader does Buy (front-run) then Sell (back-run) on the
   * same pair, with a victim trade in between. Profit = back-run WETH out
   * minus front-run WETH in, must be > 0.
   */
  async detectSandwiches(swaps: SwapRecord[]): Promise<SandwichReport[]> {
    const reports: SandwichReport[] = [];

    // Group swaps by pair
    const byPair = new Map<string, SwapRecord[]>();
    for (const s of swaps) {
      const arr = byPair.get(s.pair) ?? [];
      arr.push(s);
      byPair.set(s.pair, arr);
    }

    for (const [pair, pairSwaps] of byPair) {
      // Sort by log index (execution order within the block)
      pairSwaps.sort((a, b) => a.logIndex - b.logIndex);

      // Group by trader
      const byTrader = new Map<string, SwapRecord[]>();
      for (const s of pairSwaps) {
        const arr = byTrader.get(s.trader) ?? [];
        arr.push(s);
        byTrader.set(s.trader, arr);
      }

      for (const [attacker, traderSwaps] of byTrader) {
        const buys = traderSwaps.filter((s) => s.isBuy);
        const sells = traderSwaps.filter((s) => !s.isBuy);
        if (buys.length === 0 || sells.length === 0) continue;

        for (const buy of buys) {
          for (const sell of sells) {
            if (sell.logIndex <= buy.logIndex) continue;

            // Find victim transactions between front-run and back-run
            const victims = pairSwaps.filter(
              (s) =>
                buy.logIndex < s.logIndex &&
                s.logIndex < sell.logIndex &&
                s.trader !== attacker,
            );
            if (victims.length === 0) continue;

            // Take the largest intermediate trade as the primary victim
            const victim = victims.reduce((a, b) =>
              a.amountIn > b.amountIn ? a : b,
            );

            // Attacker profit in WETH units (18 decimals)
            const profitRaw = sell.amountOut - buy.amountIn;
            const profitEth = Number(profitRaw) / 1e18;
            if (profitEth <= 0) continue; // no profit = not a sandwich

            const tokenAddr = buy.tokenOut;
            const [symbol, decimals] = await this.getTokenInfo(tokenAddr);
            const tokenAmount = Number(
              victim.isBuy ? victim.amountOut : victim.amountIn,
            ) / 10 ** decimals;

            // Victim loss estimate ≈ attacker profit (simplified model)
            const victimLoss = profitEth * 0.9;

            reports.push({
              blockNumber: 0, // filled by caller
              victimTx: victim.txHash,
              attacker,
              pair,
              frontRunTx: buy.txHash,
              backRunTx: sell.txHash,
              attackerProfitNative: roundTo(profitEth, 6),
              victimLossNative: roundTo(victimLoss, 6),
              tokenSymbol: symbol,
              tokenAmount: roundTo(tokenAmount, 4),
              nativeSymbol: this.nativeSymbol,
              chain: this.chain,
              scanUrl: this.scanUrl,
              victimAddress: victim.trader,
              tokenAddress: tokenAddr,
            });
            break; // each buy only matches the earliest sell
          }
        }
      }
    }
    return reports;
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
/** Interpret a 32-byte hex word as a signed two's complement integer. */
function fromTwos(hexWord: string): bigint {
  const big = BigInt(hexWord);
  const MASK = (1n << 256n) - 1n;
  const SIGN_BIT = 1n << 255n;
  if (big & SIGN_BIT) {
    // Negative: compute two's complement
    return -((~big & MASK) + 1n);
  }
  return big;
}

function abs(n: bigint): bigint {
  return n < 0n ? -n : n;
}

function roundTo(value: number, decimals: number): number {
  const factor = 10 ** decimals;
  return Math.round(value * factor) / factor;
}
