/**
 * Diagnostic report HTML card generator — TypeScript port of
 * src/report_generator.py.
 *
 * Returns the HTML as a string (the Worker stores it in KV / R2 / Durable
 * Object, or returns it via a Worker Route — there is no local filesystem
 * in Cloudflare Workers).
 */
import type { SandwichReport } from "./types";

const HTML_TEMPLATE = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MEV Sandwich Attack Report #{block_num}</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0d1117; color: #c9d1d9; padding: 20px;
}
.card {
    max-width: 680px; margin: 0 auto; background: #161b22;
    border: 1px solid #30363d; border-radius: 12px; overflow: hidden;
}
.header {
    background: linear-gradient(135deg, #da3633 0%, #f85149 100%);
    padding: 24px 28px; color: #fff;
}
.header h1 { font-size: 22px; margin-bottom: 6px; }
.header .meta { font-size: 13px; opacity: 0.85; }
.section { padding: 20px 28px; border-bottom: 1px solid #21262d; }
.section:last-child { border-bottom: none; }
.section h2 {
    font-size: 14px; color: #8b949e; text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 12px;
}
.row { display: flex; justify-content: space-between; padding: 6px 0; }
.row .label { color: #8b949e; font-size: 14px; }
.row .value { color: #e6edf3; font-size: 14px; font-family: monospace; }
.row .value a { color: #58a6ff; text-decoration: none; }
.row .value a:hover { text-decoration: underline; }
.profit { color: #f85149 !important; font-weight: 600; }
.loss { color: #f85149 !important; font-weight: 600; }
.usd { color: #8b949e; font-size: 12px; }
.tag {
    display: inline-block; padding: 2px 8px; border-radius: 6px;
    font-size: 12px; font-weight: 500; margin-right: 4px;
}
.tag-chain { background: #1f6feb22; color: #58a6ff; border: 1px solid #1f6feb55; }
.tag-version { background: #23863622; color: #3fb950; border: 1px solid #23863655; }
.explain {
    background: #1c1c1c; border-left: 3px solid #f85149;
    padding: 12px 16px; border-radius: 4px; font-size: 13px;
    line-height: 1.6; color: #b1bac4;
}
.explain ul { padding-left: 20px; margin-top: 6px; }
.explain li { margin-bottom: 4px; }
.footer {
    padding: 16px 28px; text-align: center;
    font-size: 12px; color: #484f58;
}
.addr { font-size: 12px; word-break: break-all; }
</style>
</head>
<body>
<div class="card">
    <div class="header">
        <h1>Sandwich Attack Report</h1>
        <div class="meta">
            Block #{block_num} - {chain} - {timestamp}
        </div>
    </div>

    <div class="section">
        <h2>Attack Overview</h2>
        <div class="row">
            <span class="label">Victim TX</span>
            <span class="value"><a href="{scan}/tx/{victim_tx}" target="_blank">{victim_tx_short}</a></span>
        </div>
        <div class="row">
            <span class="label">Attacker</span>
            <span class="value addr"><a href="{scan}/address/{attacker}" target="_blank">{attacker}</a></span>
        </div>
        <div class="row">
            <span class="label">Front-run TX</span>
            <span class="value"><a href="{scan}/tx/{front_run_tx}" target="_blank">{front_run_short}</a></span>
        </div>
        <div class="row">
            <span class="label">Back-run TX</span>
            <span class="value"><a href="{scan}/tx/{back_run_tx}" target="_blank">{back_run_short}</a></span>
        </div>
        <div class="row">
            <span class="label">Pair Contract</span>
            <span class="value addr"><a href="{scan}/address/{pair}" target="_blank">{pair}</a></span>
        </div>
    </div>

    <div class="section">
        <h2>Financial Impact</h2>
        <div class="row">
            <span class="label">Attacker Profit</span>
            <span class="value profit">+{profit} {native_symbol} {profit_usd}</span>
        </div>
        <div class="row">
            <span class="label">Victim Loss (est.)</span>
            <span class="value loss">-{victim_loss} {native_symbol} {loss_usd}</span>
        </div>
        <div class="row">
            <span class="label">Token Sandwiched</span>
            <span class="value">{token_amount} {token_symbol}</span>
        </div>
        <div class="row">
            <span class="label">Native Price</span>
            <span class="value">\${native_price} USD</span>
        </div>
    </div>

    <div class="section">
        <h2>Root Cause Analysis</h2>
        <div class="explain">
            The victim's transaction was front-run and back-run by a MEV bot
            within the same block. The attacker profited from the price
            slippage they artificially created.
            <ul>
                <li><b>High slippage tolerance</b>: The victim likely set
                    slippage too high (&gt;1%), allowing the attacker to
                    manipulate the price range.</li>
                <li><b>Public mempool exposure</b>: The transaction was
                    visible in the public mempool before execution, giving
                    the attacker time to prepare front/back-run
                    transactions.</li>
                <li><b>Low liquidity pool</b>: The pair may have
                    insufficient liquidity, amplifying price impact.</li>
            </ul>
        </div>
    </div>

    <div class="section">
        <h2>Recommended Actions</h2>
        <div class="explain">
            <ul>
                <li>Use <b>MEV-Share</b> or <b>Flashbots Protect</b> to
                    submit transactions privately, bypassing the public
                    mempool.</li>
                <li>Reduce slippage tolerance to &lt;0.5%.</li>
                <li>Split large trades into smaller batches across
                    multiple blocks.</li>
                <li>Consider using DEX aggregators (1inch, CowSwap) that
                    have built-in MEV protection.</li>
            </ul>
        </div>
    </div>

    <div class="footer">
        Generated by MEV Sandwich Detection Bot -
        <a href="https://github.com" style="color:#484f58;">Source</a> -
        Block #{block_num} on {chain}
    </div>
</div>
</body>
</html>`;

function formatNum(n: number, decimals: number): string {
  return n.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function shortHash(hash: string): string {
  return hash.slice(0, 10) + "..." + hash.slice(-6);
}

/**
 * Generate the HTML report for a sandwich attack.
 * Returns the HTML string and a suggested filename.
 */
export function generateReportHtml(
  report: SandwichReport,
  ethPriceUsd = 0,
): { html: string; filename: string } {
  const profitUsd = ethPriceUsd
    ? `(approx. $${formatNum(report.attackerProfitNative * ethPriceUsd, 2)})`
    : "";
  const lossUsd = ethPriceUsd
    ? `(approx. $${formatNum(report.victimLossNative * ethPriceUsd, 2)})`
    : "";

  const ts = new Date().toISOString().replace("T", " ").slice(0, 19) + " UTC";

  const html = HTML_TEMPLATE
    .replace(/{block_num}/g, String(report.blockNumber))
    .replace(/{chain}/g, report.chain.toUpperCase())
    .replace(/{timestamp}/g, ts)
    .replace(/{scan}/g, report.scanUrl)
    .replace(/{victim_tx}/g, report.victimTx)
    .replace(/{victim_tx_short}/g, shortHash(report.victimTx))
    .replace(/{attacker}/g, report.attacker)
    .replace(/{front_run_tx}/g, report.frontRunTx)
    .replace(/{front_run_short}/g, shortHash(report.frontRunTx))
    .replace(/{back_run_tx}/g, report.backRunTx)
    .replace(/{back_run_short}/g, shortHash(report.backRunTx))
    .replace(/{pair}/g, report.pair)
    .replace(/{profit}/g, report.attackerProfitNative.toFixed(6))
    .replace(/{victim_loss}/g, report.victimLossNative.toFixed(6))
    .replace(/{native_symbol}/g, report.nativeSymbol)
    .replace(/{profit_usd}/g, profitUsd)
    .replace(/{loss_usd}/g, lossUsd)
    .replace(/{token_amount}/g, formatNum(report.tokenAmount, 4))
    .replace(/{token_symbol}/g, report.tokenSymbol)
    .replace(/{native_price}/g, ethPriceUsd ? formatNum(ethPriceUsd, 2) : "0");

  const filename = `${report.chain}_${report.blockNumber}_${report.victimTx.slice(0, 10)}.html`;
  return { html, filename };
}
