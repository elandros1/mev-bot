"""
MEV 链上受害者诊断与自动触达系统
================================
主入口：连接 RPC → 监听新区块 → 诊断夹子攻击 →
        提取受害者地址 → 查找 Web3 社交账号 →
        生成 HTML 报告 → 多渠道推送 → 订阅者通知

用法：
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

# ---------------------------------------------------------------------------
# 日志配置
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
# 配置加载
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
        "report_base_url": os.getenv("REPORT_BASE_URL", ""),
        "http_port": int(os.getenv("HTTP_PORT", "8080")),
    }


# ---------------------------------------------------------------------------
# 连接 Web3（带重试）
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
            logger.warning("RPC 连接未就绪（尝试 %d/%d）", attempt, max_retries)
        except Exception as e:
            logger.warning("RPC 连接失败（尝试 %d/%d）: %s",
                           attempt, max_retries, e)
        if attempt < max_retries:
            time.sleep(min(2 ** attempt, 10))
    raise ConnectionError(
        f"无法连接 RPC 节点（重试 {max_retries} 次后失败）: {rpc_url}")


# ---------------------------------------------------------------------------
# 本地保存检测结果
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
        logger.info("检测结果已保存: %s", filepath)
    except Exception as e:
        logger.error("保存检测结果失败: %s", e)


# ---------------------------------------------------------------------------
# 报告 HTTP 服务器（提供 HTML 报告链接）
# ---------------------------------------------------------------------------
def start_report_server(port: int) -> threading.Thread:
    """启动一个简单的 HTTP 服务器来提供 HTML 报告"""
    os.makedirs(REPORTS_DIR, exist_ok=True)

    class ReportHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=REPORTS_DIR, **kwargs)

    def _serve():
        server = HTTPServer(("0.0.0.0", port), ReportHandler)
        logger.info("报告 HTTP 服务器已启动: http://localhost:%d", port)
        server.serve_forever()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Telegram 命令处理线程
# ---------------------------------------------------------------------------
def start_telegram_command_poller(cmd_handler: TelegramCommandHandler):
    """在后台线程中轮询 Telegram getUpdates"""
    def _poll():
        logger.info("Telegram 命令处理器已启动（长轮询）")
        while True:
            try:
                messages = cmd_handler.poll_once()
                for msg in messages:
                    cmd_handler.handle_message(msg)
            except Exception as e:
                logger.debug("Telegram 命令轮询异常: %s", e)
                time.sleep(5)

    t = threading.Thread(target=_poll, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# 处理单个区块
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
    """处理单个区块：拉取 swap → 检测夹子 → 受害者识别 → 生成报告 → 推送"""
    try:
        swaps = analyzer.fetch_swaps(block_num)
        if not swaps:
            logger.debug("区块 #%d 无 swap 事件", block_num)
            return

        reports = analyzer.detect_sandwiches(swaps)
        if not reports:
            logger.info("扫描区块 #%d | %d 笔 swap | 未发现夹子攻击",
                        block_num, len(swaps))
            return

        logger.info("🚨 区块 #%d 检测到 %d 起夹子攻击！（%d 笔 swap）",
                    block_num, len(reports), len(swaps))

        for r in reports:
            r.block_number = block_num
            sub_manager.increment_stat("attacks_detected")

            # 1. 受害者身份识别（ENS + Web3 社交）
            victim_info = None
            if r.victim_address:
                logger.info("正在识别受害者: %s...",
                            r.victim_address[:12])
                victim_info = outreach.identify_victim(r.victim_address)
                if victim_info.get("ens_name"):
                    logger.info("受害者 ENS: %s",
                                victim_info["ens_name"])
                socials = victim_info.get("social_accounts", [])
                if socials:
                    logger.info("受害者社交账号: %s",
                                [s["platform"] for s in socials])

            # 2. 生成 HTML 诊断报告
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

            # 3. 保存检测 JSON（含受害者身份）
            save_detection(r, chain, victim_info)

            # 4. 公共广播（ntfy + Telegram 频道，带双语卡片+诊断报告按钮）
            notifier.send(r, report_url)

            # 5. 订阅者定向通知
            if r.victim_address:
                subs = sub_manager.find_subscribers(r.victim_address)
                if subs:
                    logger.info("找到 %d 个订阅者", len(subs))
                    for sub in subs:
                        chat_id = sub["chat_id"]
                        if report_url:
                            msg = (
                                f"🚨 *检测到你的钱包被夹！*\n"
                                f"地址: `{r.victim_address[:10]}...`\n"
                                f"损失: {r.victim_loss_native} "
                                f"{r.native_symbol}\n"
                                f"📋 [查看完整报告]({report_url})"
                            )
                        else:
                            msg = notifier.telegram.format_report(
                                r, eth_price) if notifier.telegram else ""
                        if notifier.telegram:
                            notifier.telegram.send_to_chat(chat_id, msg)

                # 也检查 ENS 名称匹配
                if victim_info and victim_info.get("ens_name"):
                    ens_subs = sub_manager.find_subscribers_by_ens(
                        victim_info["ens_name"])
                    for sub in ens_subs:
                        if sub not in subs:  # 避免重复
                            chat_id = sub["chat_id"]
                            msg = (
                                f"🚨 *检测到 {victim_info['ens_name']} "
                                f"被夹！*\n"
                                f"损失: {r.victim_loss_native} "
                                f"{r.native_symbol}"
                            )
                            if notifier.telegram:
                                notifier.telegram.send_to_chat(chat_id, msg)

    except Exception as e:
        logger.error("处理区块 #%d 失败: %s", block_num, e)


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def run() -> None:
    config = load_config()

    if not config["rpc_url"]:
        logger.error("缺少 RPC_URL 配置，请检查 .env")
        sys.exit(1)

    has_telegram = bool(config["bot_token"] and config["chat_id"])
    has_ntfy = bool(config["ntfy_topic"])
    if not (has_telegram or has_ntfy):
        logger.error("至少需要配置一个通知渠道 (Telegram 或 ntfy)")
        sys.exit(1)

    # ---- 连接 RPC ----
    logger.info("正在连接以太坊 RPC: %s", config["rpc_url"])
    w3 = connect_w3(config["rpc_url"])
    chain_id = w3.eth.chain_id
    logger.info("连接成功 | ChainID=%s | 当前区块 #%s",
                chain_id, w3.eth.block_number)

    analyzer = MEVAnalyzer(w3, chain="ethereum")

    # ---- 初始化通知器 ----
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
    logger.info("通知渠道: %s", " + ".join(channels))

    # ---- 初始化订阅管理器 ----
    sub_manager = SubscriptionManager()
    logger.info("订阅管理器已就绪（SQLite: %s）", sub_manager.db_path)

    # ---- 初始化受害者触达模块 ----
    outreach = VictimOutreach(
        w3=w3,
        neynar_api_key=config["neynar_api_key"],
    )
    logger.info("受害者触达模块已就绪（ENS: %s, Farcaster: %s, Lens: %s）",
                "✅" if outreach._ens_registry else "❌",
                "✅" if config["neynar_api_key"] else "❌",
                "✅")

    # ---- 启动报告 HTTP 服务器 ----
    report_base_url = config["report_base_url"]
    if not report_base_url:
        start_report_server(config["http_port"])
        report_base_url = f"http://localhost:{config['http_port']}"
        logger.info("报告访问地址: %s", report_base_url)

    # ---- 启动 Telegram 命令处理 ----
    if has_telegram:
        cmd_handler = TelegramCommandHandler(
            bot_token=config["bot_token"],
            api_base=config["telegram_api_base"],
            sub_manager=sub_manager,
            ens_resolver=outreach,
        )
        # 复用 TelegramNotifier 获取的 bot_username（避免重复 API 调用）
        if tg and tg.bot_username:
            cmd_handler.bot_username = tg.bot_username
        start_telegram_command_poller(cmd_handler)

    # ---- 启动通知 ----
    notifier.send_text(
        "🤖 *MEV 夹子攻击诊断系统已启动*\n"
        f"网络：以太坊主网\n"
        f"当前区块：`#{w3.eth.block_number}`\n"
        f"通知渠道：{', '.join(channels)}\n"
        f"受害者触达：ENS反解 + Web3社交\n"
        f"订阅系统：已就绪 (/watch)\n"
        f"报告服务：{report_base_url}\n"
        "正在实时监听新区块..."
    )

    last_block = w3.eth.block_number
    logger.info("正在监听新区块...（起始区块 #%d）", last_block)

    # ---- 可选：BSC ----
    bsc_analyzer = None
    if config["scan_bsc"] and config["bsc_rpc_url"]:
        try:
            w3_bsc = connect_w3(config["bsc_rpc_url"])
            bsc_analyzer = MEVAnalyzer(w3_bsc, chain="bsc")
            logger.info("BSC 监听已启用，当前区块 #%s",
                        w3_bsc.eth.block_number)
        except Exception as e:
            logger.warning("BSC 连接失败，跳过: %s", e)

    # ---- 轮询主循环 ----
    while True:
        try:
            if not w3.is_connected():
                logger.warning("RPC 连接断开，尝试重连...")
                w3 = connect_w3(config["rpc_url"])
                analyzer = MEVAnalyzer(w3, chain="ethereum")
                logger.info("RPC 重连成功 | 当前区块 #%s",
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
                    logger.debug("BSC 轮询异常: %s", e)

        except Exception as e:
            logger.error("主循环异常: %s", e)
            time.sleep(5)

        time.sleep(config["poll_interval"])


if __name__ == "__main__":
    run()
