"""
Diagnostic Report HTML Card Generator
------------------------------------
Generates a structured HTML report page for each detected sandwich attack,
including transaction hash, victim loss (USD), attacker profit, and root cause
analysis for victims to review directly.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from .analyzer import SandwichReport

logger = logging.getLogger(__name__)

REPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "reports",
)

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MEV Sandwich Attack Report #{block_num}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0d1117; color: #c9d1d9; padding: 20px;
}}
.card {{
    max-width: 680px; margin: 0 auto; background: #161b22;
    border: 1px solid #30363d; border-radius: 12px; overflow: hidden;
}}
.header {{
    background: linear-gradient(135deg, #da3633 0%, #f85149 100%);
    padding: 24px 28px; color: #fff;
}}
.header h1 {{ font-size: 22px; margin-bottom: 6px; }}
.header .meta {{ font-size: 13px; opacity: 0.85; }}
.section {{ padding: 20px 28px; border-bottom: 1px solid #21262d; }}
.section:last-child {{ border-bottom: none; }}
.section h2 {{
    font-size: 14px; color: #8b949e; text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 12px;
}}
.row {{ display: flex; justify-content: space-between; padding: 6px 0; }}
.row .label {{ color: #8b949e; font-size: 14px; }}
.row .value {{ color: #e6edf3; font-size: 14px; font-family: monospace; }}
.row .value a {{ color: #58a6ff; text-decoration: none; }}
.row .value a:hover {{ text-decoration: underline; }}
.profit {{ color: #f85149 !important; font-weight: 600; }}
.loss {{ color: #f85149 !important; font-weight: 600; }}
.usd {{ color: #8b949e; font-size: 12px; }}
.tag {{
    display: inline-block; padding: 2px 8px; border-radius: 6px;
    font-size: 12px; font-weight: 500; margin-right: 4px;
}}
.tag-chain {{ background: #1f6feb22; color: #58a6ff; border: 1px solid #1f6feb55; }}
.tag-version {{ background: #23863622; color: #3fb950; border: 1px solid #23863655; }}
.explain {{
    background: #1c1c1c; border-left: 3px solid #f85149;
    padding: 12px 16px; border-radius: 4px; font-size: 13px;
    line-height: 1.6; color: #b1bac4;
}}
.explain ul {{ padding-left: 20px; margin-top: 6px; }}
.explain li {{ margin-bottom: 4px; }}
.footer {{
    padding: 16px 28px; text-align: center;
    font-size: 12px; color: #484f58;
}}
.addr {{ font-size: 12px; word-break: break-all; }}
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
            <span class="value">${native_price:,.2f} USD</span>
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
</html>"""


def generate_report_html(
    report: SandwichReport,
    eth_price_usd: float = 0.0,
    victim_ens: Optional[str] = None,
) -> str:
    """Generate an HTML diagnostic report and return the file path."""
    os.makedirs(REPORTS_DIR, exist_ok=True)

    profit_usd_str = f"(approx. ${report.attacker_profit_native * eth_price_usd:,.2f})" if eth_price_usd else ""
    loss_usd_str = f"(approx. ${report.victim_loss_native * eth_price_usd:,.2f})" if eth_price_usd else ""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")

    html = HTML_TEMPLATE.format(
        block_num=report.block_number,
        chain=report.chain.upper(),
        timestamp=ts,
        scan=report.scan_url,
        victim_tx=report.victim_tx,
        victim_tx_short=f"{report.victim_tx[:10]}...{report.victim_tx[-6:]}",
        attacker=report.attacker,
        front_run_tx=report.front_run_tx,
        front_run_short=f"{report.front_run_tx[:10]}...{report.front_run_tx[-6:]}",
        back_run_tx=report.back_run_tx,
        back_run_short=f"{report.back_run_tx[:10]}...{report.back_run_tx[-6:]}",
        pair=report.pair,
        profit=f"{report.attacker_profit_native:.6f}",
        victim_loss=f"{report.victim_loss_native:.6f}",
        native_symbol=report.native_symbol,
        profit_usd=profit_usd_str,
        loss_usd=loss_usd_str,
        token_amount=f"{report.token_amount:,.4f}",
        token_symbol=report.token_symbol,
        native_price=eth_price_usd,
    )

    filename = f"{report.chain}_{report.block_number}_{report.victim_tx[:10]}.html"
    filepath = os.path.join(REPORTS_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info("HTML report generated: %s", filepath)
    return filepath
