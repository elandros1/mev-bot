"""
MEV On-Chain Victim Diagnostic & Auto-Outreach System
=====================================================
Main entry point: Connect RPC -> Listen for new blocks -> Detect sandwich attacks ->
Extract victim address -> Lookup Web3 social accounts ->
Generate HTML report -> Multi-channel push -> Subscriber notifications

Usage:
    python main.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler

from dotenv import load_dotenv
from web3 import Web3

from src.analyzer import MEVAnalyzer
from src.tg_bot import (
    TelegramNotifier, NtfyNotifier, MultiNotifier, TelegramCommandHandler,
)
from src.report_generator import generate_report_html
from src.subscription_manager import SubscriptionManager
from src.victim_outreach import VictimOutreach
from src.i18n import t, get_subscriber_message

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("mev_bot")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DETECTIONS_DIR = os.path.join(BASE_DIR, "detections")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------
def load_config() -> dict:
    load_dotenv()
    return {
        "rpc_url": os.getenv("RPC_URL", ""),
        "bsc_rpc_url": os.getenv("BSC_RPC_URL", ""),
        "scan_bsc": os.getenv("SCAN_BSC", "false").lower() == "true",
        "network": os.getenv("NETWORK", "ethereum"),
        "poll_interval": float(os.getenv("POLL_INTERVAL", "2")),
        "bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
        "telegram_api_base": os.getenv("TELEGRAM_API_BASE",
                                       "https://api.telegram.org"),
        "ntfy_topic": os.getenv("NTFY_TOPIC", ""),
        "ntfy_server": os.getenv("NTFY_SERVER", "https://ntfy.sh"),
        "neynar_api_key": os.getenv("NEYNAR_API_KEY", ""),
        "neynar_signer_uuid": os.getenv("NEYNAR_SIGNER_UUID", ""),
        "report_base_url": os.getenv("REPORT_BASE_URL", ""),
        "http_port": int(os.getenv("HTTP_PORT", "8080")),
    }


# ---------------------------------------------------------------------------
# Connect to Web3 (with retry)
# ---------------------------------------------------------------------------
def connect_w3(rpc_url: str, max_retries: int = 5) -> Web3:
    for attempt in range(1, max_retries + 1):
        try:
            if rpc_url.startswith(("ws://", "wss://")):
                w3 = Web3(Web3.WebsocketProvider(rpc_url))
            else:
                w3 = Web3(Web3.HTTPProvider(
                    rpc_url, request_kwargs={"timeout": 30}))
            if w3.is_connected():
                return w3
            logger.warning("RPC not ready (attempt %d/%d)", attempt, max_retries)
        except Exception as e:
            logger.warning("RPC connection failed (attempt %d/%d): %s",
                           attempt, max_retries, e)
        if attempt < max_retries:
            time.sleep(min(2 ** attempt, 10))
    raise ConnectionError(
        f"Failed to connect to RPC node after {max_retries} retries: {rpc_url}")


# ---------------------------------------------------------------------------
# Save detection results locally
# ---------------------------------------------------------------------------
def save_detection(report, chain: str, victim_info: dict = None) -> None:
    os.makedirs(DETECTIONS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{chain}_{report.block_number}.json"
    filepath = os.path.join(DETECTIONS_DIR, filename)
    try:
        data = {
            "detected_at": datetime.now().isoformat(),
            "block_number": report.block_number,
            "chain": chain,
            "victim_tx": report.victim_tx,
            "victim_address": report.victim_address,
            "attacker": report.attacker,
            "pair": report.pair,
            "front_run_tx": report.front_run_tx,
            "back_run_tx": report.back_run_tx,
            "token_symbol": report.token_symbol,
            "token_address": report.token_address,
            "token_amount": report.token_amount,
            "attacker_profit_native": report.attacker_profit_native,
            "victim_loss_native": report.victim_loss_native,
            "native_symbol": report.native_symbol,
            "scan_url": report.scan_url,
        }
        if victim_info:
            data["victim_identity"] = victim_info
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Detection saved: %s", filepath)
    except Exception as e:
        logger.error("Failed to save detection: %s", e)


# ---------------------------------------------------------------------------
# Report HTTP server (serves HTML report links)
# ---------------------------------------------------------------------------
def start_report_server(port: int) -> threading.Thread:
    """Start a simple HTTP server to serve HTML reports."""
    os.makedirs(REPORTS_DIR, exist_ok=True)

    class ReportHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=REPORTS_DIR, **kwargs)

    def _serve():
        server = HTTPServer(("0.0.0.0", port), ReportHandler)
        logger.info("Report HTTP server started: http://localhost:%d", port)
        server.serve_forever()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Telegram command handler thread
# ---------------------------------------------------------------------------
def start_telegram_command_poller(cmd_handler: TelegramCommandHandler):
    """Poll Telegram getUpdates in a background thread."""
    def _poll():
        logger.info("Telegram command handler started (long-polling)")
        while True:
            try:
                messages = cmd_handler.poll_once()
                for msg in messages:
                    cmd_handler.handle_message(msg)
            except Exception as e:
                logger.debug("Telegram command polling error: %s", e)
                time.sleep(5)

    t = threading.Thread(target=_poll, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Process a single block
# ---------------------------------------------------------------------------
def process_block(
    analyzer: MEVAnalyzer,
    notifier: MultiNotifier,
    block_num: int,
    chain: str,
    sub_manager: SubscriptionManager,
    outreach: VictimOutreach,
    report_base_url: str = "",
) -> None:
    """Process a single block: fetch swaps -> detect sandwiches -> victim identification -> generate report -> push."""
    try:
        swaps = analyzer.fetch_swaps(block_num)
        if not swaps:
            logger.debug("Block #%d: no swap events", block_num)
            return

        reports = analyzer.detect_sandwiches(swaps)
        if not reports:
            logger.info("Scanned block #%d | %d swaps | no sandwich attacks detected",
                        block_num, len(swaps))
            return

        logger.info("Block #%d: %d sandwich attack(s) detected! (%d swaps)",
                    block_num, len(reports), len(swaps))

        for r in reports:
            r.block_number = block_num
            sub_manager.increment_stat("attacks_detected")

            # 1. Victim identity identification (ENS + Web3 social)
            victim_info = None
            if r.victim_address:
                logger.info("Identifying victim: %s...",
                            r.victim_address[:12])
                victim_info = outreach.identify_victim(r.victim_address)
                if victim_info.get("ens_name"):
                    logger.info("Victim ENS: %s",
                                victim_info["ens_name"])
                socials = victim_info.get("social_accounts", [])
                if socials:
                    logger.info("Victim social accounts: %s",
                                [s["platform"] for s in socials])

            # 2. Generate HTML diagnostic report
            eth_price = TelegramNotifier.get_native_price_usd(
                r.native_symbol)
            report_path = generate_report_html(
                r, eth_price,
                victim_ens=victim_info.get("ens_name") if victim_info else None,
            )
            report_url = ""
            if report_base_url:
                report_filename = os.path.basename(report_path)
                report_url = f"{report_base_url}/{report_filename}"

            # 3. Save detection JSON (including victim identity)
            save_detection(r, chain, victim_info)

            # 4. Public broadcast (ntfy + Telegram channel, with bilingual card + report buttons)
            notifier.send(r, report_url)

            # 5. Zero-friction victim outreach: auto-mention on Farcaster
            #    Victim receives the alert next time they open Warpcast,
            #    no subscription or opt-in required.
            if victim_info and report_url:
                try:
                    outreach.outreach_to_farcaster(
                        victim_info, r, report_url)
                except Exception as e:
                    logger.error("Farcaster outreach failed: %s", e)

            # 6. Subscriber targeted notifications
            if r.victim_address:
                subs = sub_manager.find_subscribers(r.victim_address)
                if subs:
                    logger.info("Found %d subscriber(s)", len(subs))
                    for sub in subs:
                        chat_id = sub["chat_id"]
                        msg = get_subscriber_message("en", r, report_url)
                        if notifier.telegram:
                            notifier.telegram.send_to_chat(chat_id, msg)

                # Also check ENS name matching
                if victim_info and victim_info.get("ens_name"):
                    ens_subs = sub_manager.find_subscribers_by_ens(
                        victim_info["ens_name"])
                    for sub in ens_subs:
                        if sub not in subs:  # Avoid duplicates
                            chat_id = sub["chat_id"]
                            msg = (
                                f"🚨 *{t('en', 'main.subscriber_ens_alert')}* "
                                f"*{victim_info['ens_name']}*\n"
                                f"{t('en', 'main.subscriber_loss')}: "
                                f"{r.victim_loss_native} {r.native_symbol}"
                            )
                            if notifier.telegram:
                                notifier.telegram.send_to_chat(chat_id, msg)

    except Exception as e:
        logger.error("Failed to process block #%d: %s", block_num, e)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run() -> None:
    config = load_config()

    if not config["rpc_url"]:
        logger.error("Missing RPC_URL configuration, please check .env")
        sys.exit(1)

    has_telegram = bool(config["bot_token"] and config["chat_id"])
    has_ntfy = bool(config["ntfy_topic"])
    if not (has_telegram or has_ntfy):
        logger.error("At least one notification channel required (Telegram or ntfy)")
        sys.exit(1)

    # ---- Connect to RPC ----
    logger.info("Connecting to Ethereum RPC: %s", config["rpc_url"])
    w3 = connect_w3(config["rpc_url"])
    chain_id = w3.eth.chain_id
    logger.info("Connected | ChainID=%s | Current block #%s",
                chain_id, w3.eth.block_number)

    analyzer = MEVAnalyzer(w3, chain="ethereum")

    # ---- Initialize notifiers ----
    tg = TelegramNotifier(
        config["bot_token"], config["chat_id"],
        api_base=config["telegram_api_base"],
    ) if has_telegram else None

    ntfy = NtfyNotifier(
        config["ntfy_topic"], config["ntfy_server"],
    ) if has_ntfy else None

    notifier = MultiNotifier(telegram=tg, ntfy=ntfy)

    channels = []
    if tg:
        channels.append("Telegram")
    if ntfy:
        channels.append(f"ntfy({config['ntfy_topic']})")
    logger.info("Notification channels: %s", " + ".join(channels))

    # ---- Initialize subscription manager ----
    sub_manager = SubscriptionManager()
    logger.info("Subscription manager ready (SQLite: %s)", sub_manager.db_path)

    # ---- Initialize victim outreach module ----
    outreach = VictimOutreach(
        w3=w3,
        neynar_api_key=config["neynar_api_key"],
        neynar_signer_uuid=config["neynar_signer_uuid"],
    )
    fc_ready = bool(config["neynar_api_key"] and config["neynar_signer_uuid"])
    logger.info("Victim outreach module ready (ENS: %s, Farcaster: %s, Lens: %s)",
                "yes" if outreach._ens_registry else "no",
                "yes (auto-cast)" if fc_ready else "lookup only",
                "yes")

    # ---- Start report HTTP server ----
    report_base_url = config["report_base_url"]
    if not report_base_url:
        start_report_server(config["http_port"])
        report_base_url = f"http://localhost:{config['http_port']}"
        logger.info("Report URL: %s", report_base_url)

    # ---- Start Telegram command handler ----
    if has_telegram:
        cmd_handler = TelegramCommandHandler(
            bot_token=config["bot_token"],
            api_base=config["telegram_api_base"],
            sub_manager=sub_manager,
            ens_resolver=outreach,
        )
        # Reuse TelegramNotifier's bot_username (avoid duplicate API call)
        if tg and tg.bot_username:
            cmd_handler.bot_username = tg.bot_username
        start_telegram_command_poller(cmd_handler)

    # ---- Startup notification ----
    notifier.send_text(
        f"🤖 *{t('en', 'main.startup_title')}*\n"
        f"{t('en', 'main.startup_network')}\n"
        f"{t('en', 'main.startup_block')}: `#{w3.eth.block_number}`\n"
        f"{t('en', 'main.startup_channels')}: {', '.join(channels)}\n"
        f"{t('en', 'main.startup_outreach')}\n"
        f"{t('en', 'main.startup_subscriptions')}\n"
        f"{t('en', 'main.startup_report_service')}: {report_base_url}\n"
        f"{t('en', 'main.startup_listening')}"
    )

    last_block = w3.eth.block_number
    logger.info("Listening for new blocks... (start block #%d)", last_block)

    # ---- Optional: BSC ----
    bsc_analyzer = None
    if config["scan_bsc"] and config["bsc_rpc_url"]:
        try:
            w3_bsc = connect_w3(config["bsc_rpc_url"])
            bsc_analyzer = MEVAnalyzer(w3_bsc, chain="bsc")
            logger.info("BSC monitoring enabled, current block #%s",
                        w3_bsc.eth.block_number)
        except Exception as e:
            logger.warning("BSC connection failed, skipping: %s", e)

    # ---- Main polling loop ----
    while True:
        try:
            if not w3.is_connected():
                logger.warning("RPC connection lost, attempting reconnect...")
                w3 = connect_w3(config["rpc_url"])
                analyzer = MEVAnalyzer(w3, chain="ethereum")
                logger.info("RPC reconnected | current block #%s",
                            w3.eth.block_number)

            current = w3.eth.block_number
            if current > last_block:
                for block_num in range(last_block + 1, current + 1):
                    process_block(
                        analyzer, notifier, block_num, "ethereum",
                        sub_manager, outreach, report_base_url,
                    )
                last_block = current

            if bsc_analyzer is not None:
                try:
                    bsc_block = bsc_analyzer.w3.eth.block_number
                    if (not hasattr(run, "_last_bsc")
                            or bsc_block > run._last_bsc):
                        run._last_bsc = bsc_block
                        process_block(
                            bsc_analyzer, notifier, bsc_block, "bsc",
                            sub_manager, outreach, report_base_url,
                        )
                except Exception as e:
                    logger.debug("BSC polling error: %s", e)

        except Exception as e:
            logger.error("Main loop exception: %s", e)
            time.sleep(5)

        time.sleep(config["poll_interval"])


if __name__ == "__main__":
    run()
