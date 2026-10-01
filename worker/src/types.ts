/**
 * Type definitions shared across the MEV sandwich worker.
 */

/** Parsed result of a single Uniswap V2/V3 Swap event. */
export interface SwapRecord {
  txHash: string;
  logIndex: number;
  pair: string;        // pair / pool address (lowercase)
  trader: string;      // recipient of the output token
  tokenIn: string;
  tokenOut: string;
  amountIn: bigint;    // smallest unit
  amountOut: bigint;   // smallest unit
  isBuy: boolean;      // true = buying target token with WETH
  version: 2 | 3;
}

/** Sandwich attack diagnostic report. */
export interface SandwichReport {
  blockNumber: number;
  victimTx: string;
  attacker: string;
  pair: string;
  frontRunTx: string;
  backRunTx: string;
  attackerProfitNative: number;   // in native token (ETH / BNB)
  victimLossNative: number;      // estimated victim loss
  tokenSymbol: string;
  tokenAmount: number;
  nativeSymbol: string;          // "ETH" | "BNB"
  chain: string;                 // "ethereum" | "bsc"
  scanUrl: string;
  victimAddress: string;
  tokenAddress: string;
}

/** Worker environment: secrets and vars declared in wrangler.toml. */
export interface Env {
  // Secrets
  RPC_URL: string;
  BSC_RPC_URL: string;
  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_CHAT_ID: string;
  TELEGRAM_API_BASE: string;
  NTFY_TOPIC: string;
  NTFY_SERVER: string;
  NEYNAR_API_KEY: string;
  NEYNAR_SIGNER_UUID: string;

  // Vars
  NETWORK: string;       // "ethereum" | "bsc"
  SCAN_BSC: string;      // "true" | "false"
  REPORT_BASE_URL: string;

  // Durable Object binding for cursor / subscription persistence
  STATE: DurableObjectNamespace;

  // KV namespace for diagnostics
  DIAG: KVNamespace;

  // Cron bindings
  CRON_TRIGGER: string;
}

/**
 * Re-export the MevState Durable Object so the binding can be typed.
 * Used as the stub generic param: Env["STATE"] returns MevState methods.
 */
import type { MevState } from "./state";
export type { MevState };
