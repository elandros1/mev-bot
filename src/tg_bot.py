"""
通知推送模块
------------
支持多渠道推送夹子攻击诊断报告：
- Telegram Bot API（需配置 BOT_TOKEN / CHAT_ID）
- ntfy.sh（免费、无需注册，仅需一个 topic 名称）
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

from .analyzer import SandwichReport

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    """Telegram 消息推送器"""

    def __init__(self, bot_token: str, chat_id: str,
                 api_base: str = DEFAULT_API_BASE):
        self.bot_token = bot_token.strip()
        self.chat_id = str(chat_id).strip()
        self.api_base = api_base.rstrip("/")
        self.api_url = f"{self.api_base}/bot{self.bot_token}/sendMessage"
        self.enabled = bool(self.bot_token and self.chat_id)

    @staticmethod
    def _proxies() -> Optional[dict]:
        """读取环境变量中的 HTTP 代理配置"""
        proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                 or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
        return {"http": proxy, "https": proxy} if proxy else None

    @staticmethod
    def get_native_price_usd(symbol: str = "ETH") -> float:
        """获取原生代币的 USD 价格（失败返回 0）"""
        proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                 or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
        proxies = {"http": proxy, "https": proxy} if proxy else None

        # 尝试多个价格源（沙箱可能屏蔽部分 API）
        pair = "ETH_USDT" if symbol == "ETH" else "BNB_USDT"
        sources = [
            # Gate.io
            (f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={pair}",
             lambda r: float(r.json()[0]["last"])),
            # Huobi
            (f"https://api.huobi.pro/market/detail/merged?symbol={pair.lower()}",
             lambda r: float(r.json()["tick"]["close"])),
            # CoinGecko
            (f"https://api.coingecko.com/api/v3/simple/price?ids="
             f"{'ethereum' if symbol == 'ETH' else 'binancecoin'}&vs_currencies=usd",
             lambda r: float(r.json()[list(r.json().keys())[0]]["usd"])),
        ]
        for url, parser in sources:
            try:
                resp = requests.get(url, timeout=5, proxies=proxies)
                if resp.status_code == 200:
                    return parser(resp)
            except Exception as e:
                logger.debug("价格源 %s 失败: %s", url.split('/')[2], e)
                continue
        logger.warning("所有价格源均失败，无法获取 %s 价格", symbol)
        return 0.0

    def _get_native_price_usd(self, symbol: str = "ETH") -> float:
        return self.get_native_price_usd(symbol)

    def format_report(self, report: SandwichReport, eth_price_usd: float = 0.0) -> str:
        """将诊断报告格式化为中英双语对照文本"""
        profit = report.attacker_profit_native
        loss = report.victim_loss_native
        profit_usd = profit * eth_price_usd if eth_price_usd else 0
        loss_usd = loss * eth_price_usd if eth_price_usd else 0

        if report.chain == "ethereum":
            chain_cn, chain_en = "以太坊", "Ethereum"
        else:
            chain_cn, chain_en = "币安智能链", "BSC"
        scan = report.scan_url

        # 受害者显示：优先用地址，有 ENS 则附加
        victim_display = report.victim_address or report.victim_tx[:10] + "..."
        if len(victim_display) == 42:
            victim_short = f"{victim_display[:10]}...{victim_display[-6:]}"
        else:
            victim_short = victim_display

        attacker_short = f"{report.attacker[:10]}...{report.attacker[-6:]}"

        # 利润/损失行
        profit_line = f"*_{profit:.6f} {report.native_symbol}_*"
        if profit_usd:
            profit_line += f"  ≈ ${profit_usd:,.2f}"
        loss_line = f"*_{loss:.6f} {report.native_symbol}_*"
        if loss_usd:
            loss_line += f"  ≈ ${loss_usd:,.2f}"

        lines = [
            "🚨 *MEV 夹子攻击预警 / Sandwich Attack Alert* 🚨",
            "",
            f"📦 区块 / Block: `#{report.block_number}`",
            f"🔗 网络 / Network: {chain_cn} / {chain_en}",
            "",
            f"👤 受害者 / Victim: `{victim_short}`",
            f"🕵️ 攻击者 / Attacker: `{attacker_short}`",
            f"💱 代币 / Token: {report.token_symbol} ({report.token_amount:,.2f})",
            "",
            f"💰 攻击者获利 / Attacker Profit: {profit_line}",
            f"📉 受害者损失 / Victim Loss: {loss_line}",
            "",
            "⚠️ *被夹原因 / Root Cause*",
            "• 滑点设置过高 / Slippage tolerance set too high",
            "• 交易在公共内存池暴露 / Tx exposed in public mempool",
            "• 被 MEV Bot 前跑+后跑 / Front-run & back-run by MEV bot",
            "",
            "🛡️ *防护建议 / Recommendations*",
            "1. 使用 MEV-Share / Flashbots Protect 隐私池 | Use private mempool",
            "2. 调低滑点 < 0.5% | Lower slippage tolerance",
            "3. 大额拆分多笔 | Split large trades",
            "",
            f"[🔗 受害者交易 / Victim Tx]({scan}/tx/{report.victim_tx})",
            f"[🕵️ 攻击者地址 / Attacker Addr]({scan}/address/{report.attacker})",
        ]
        return "\n".join(lines)

    def _build_inline_keyboard(self, report: SandwichReport,
                               report_url: str = "") -> dict:
        """构建 Telegram 内联键盘按钮（查看诊断报告）"""
        buttons = []
        if report_url:
            buttons.append([{
                "text": "📋 查看诊断报告 / View Report",
                "url": report_url,
            }])
        buttons.append([{
            "text": "🔗 受害者交易 / Victim Tx",
            "url": f"{report.scan_url}/tx/{report.victim_tx}",
        }])
        return {"inline_keyboard": buttons} if buttons else {}

    def send(self, report: SandwichReport, report_url: str = "") -> bool:
        """发送双语诊断报告到 Telegram（带内联按钮）"""
        if not self.enabled:
            return False
        eth_price = self._get_native_price_usd(report.native_symbol)
        text = self.format_report(report, eth_price)
        reply_markup = self._build_inline_keyboard(report, report_url)

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            resp = requests.post(self.api_url, json=payload, timeout=10,
                                 proxies=self._proxies())
            if resp.status_code == 200 and resp.json().get("ok"):
                logger.info("Telegram 报告已发送 | 区块 #%s | 获利 %.4f %s",
                            report.block_number, report.attacker_profit_native,
                            report.native_symbol)
                return True
            else:
                logger.error("Telegram 发送失败: %s", resp.text[:300])
                return False
        except Exception as e:
            logger.error("Telegram 发送异常: %s", e)
            return False

    def send_text(self, text: str) -> bool:
        """发送纯文本消息（用于启动/状态通知）"""
        return self.send_to_chat(self.chat_id, text)

    def send_to_chat(self, chat_id: str, text: str) -> bool:
        """发送消息到指定 chat_id（支持群聊/用户）"""
        if not self.bot_token:
            return False
        url = f"{self.api_base}/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": str(chat_id),
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=10,
                                 proxies=self._proxies())
            return resp.status_code == 200 and resp.json().get("ok", False)
        except Exception as e:
            logger.error("Telegram send_to_chat 失败: %s", e)
            return False


class NtfyNotifier:
    """ntfy.sh 消息推送器（免费、无需注册）"""

    def __init__(self, topic: str, server: str = "https://ntfy.sh"):
        self.topic = topic.strip()
        self.server = server.rstrip("/")
        self.url = f"{self.server}/{self.topic}"
        self.enabled = bool(self.topic)

    @staticmethod
    def _proxies() -> Optional[dict]:
        proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                 or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
        return {"http": proxy, "https": proxy} if proxy else None

    def send(self, report: SandwichReport, report_url: str = "") -> bool:
        """发送诊断报告到 ntfy.sh"""
        if not self.enabled:
            return False
        eth_price = TelegramNotifier.get_native_price_usd(report.native_symbol)
        text = TelegramNotifier("", "").format_report(report, eth_price)
        if report_url:
            text += f"\n\n📋 查看诊断报告 / View Report:\n{report_url}"

        # 注意：HTTP headers 不能含非 ASCII 字符，标题用英文
        headers = {
            "Title": f"MEV Sandwich Alert #{report.block_number}",
            "Priority": "4",
            "Tags": "warning,rotating_light",
            "Click": report_url,
        }
        try:
            resp = requests.post(self.url, data=text.encode("utf-8"),
                                 headers=headers, timeout=10,
                                 proxies=self._proxies())
            if resp.status_code == 200:
                logger.info("ntfy 报告已发送 | 区块 #%s | topic=%s",
                            report.block_number, self.topic)
                return True
            else:
                logger.error("ntfy 发送失败: %s", resp.text[:300])
                return False
        except Exception as e:
            logger.error("ntfy 发送异常: %s", e)
            return False

    def send_text(self, text: str, title: str = "MEV Bot") -> bool:
        """发送纯文本消息"""
        if not self.enabled:
            return False
        headers = {"Title": title, "Priority": "3"}
        try:
            resp = requests.post(self.url, data=text.encode("utf-8"),
                                 headers=headers, timeout=10,
                                 proxies=self._proxies())
            return resp.status_code == 200
        except Exception as e:
            logger.error("ntfy 文本发送失败: %s", e)
            return False


class MultiNotifier:
    """统一多渠道通知器：同时推送 Telegram + ntfy"""

    def __init__(self, telegram: Optional[TelegramNotifier] = None,
                 ntfy: Optional[NtfyNotifier] = None):
        self.telegram = telegram
        self.ntfy = ntfy

    def send(self, report: SandwichReport, report_url: str = "") -> None:
        if self.telegram:
            self.telegram.send(report, report_url)
        if self.ntfy:
            self.ntfy.send(report, report_url)

    def send_text(self, text: str) -> None:
        if self.telegram:
            self.telegram.send_text(text)
        if self.ntfy:
            self.ntfy.send_text(text)

    def send_to_chat(self, chat_id: str, text: str) -> None:
        """发送消息到指定 chat_id（用于 Bot 命令回复）"""
        if self.telegram:
            self.telegram.send_to_chat(chat_id, text)


class TelegramCommandHandler:
    """Telegram Bot 命令处理器（长轮询 getUpdates）"""

    def __init__(self, bot_token: str, api_base: str,
                 sub_manager, ens_resolver=None):
        self.bot_token = bot_token
        self.api_base = api_base.rstrip("/")
        self.sub_manager = sub_manager
        self.ens_resolver = ens_resolver  # VictimOutreach instance
        self.offset = 0
        self.enabled = bool(bot_token)

    @staticmethod
    def _proxies() -> Optional[dict]:
        proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                 or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
        return {"http": proxy, "https": proxy} if proxy else None

    def _api_call(self, method: str, params: dict = None) -> Optional[dict]:
        url = f"{self.api_base}/bot{self.bot_token}/{method}"
        try:
            resp = requests.post(url, json=params or {}, timeout=30,
                                 proxies=self._proxies())
            if resp.status_code == 200:
                return resp.json()
            logger.error("Telegram API %s 失败: %s", method, resp.text[:200])
        except Exception as e:
            logger.debug("Telegram API %s 异常: %s", method, e)
        return None

    def poll_once(self) -> list:
        """长轮询一次 getUpdates，返回需处理的消息列表"""
        result = self._api_call("getUpdates", {
            "offset": self.offset,
            "timeout": 25,  # 长轮询超时
            "allowed_updates": ["message"],
        })
        if not result or not result.get("ok"):
            return []

        updates = result.get("result", [])
        handled = []
        for update in updates:
            self.offset = update["update_id"] + 1
            msg = update.get("message")
            if msg:
                handled.append(msg)
        return handled

    def handle_message(self, msg: dict) -> None:
        """处理单条消息：解析命令并回复"""
        text = msg.get("text", "").strip()
        chat_id = msg["chat"]["id"]
        sender = msg.get("from", {})
        sender_name = sender.get("first_name", "") or sender.get("username", "")

        if not text.startswith("/"):
            return

        parts = text.split(maxsplit=2)
        cmd = parts[0].lower().split("@")[0]  # 去掉 @botname 后缀
        args = parts[1:]

        if cmd == "/start" or cmd == "/help":
            self._cmd_help(chat_id)
        elif cmd == "/watch" and args:
            self._cmd_watch(chat_id, args[0])
        elif cmd == "/unwatch" and args:
            self._cmd_unwatch(chat_id, args[0])
        elif cmd == "/status":
            self._cmd_status(chat_id)
        elif cmd == "/stats":
            self._cmd_stats(chat_id)
        else:
            self._reply(chat_id, "未知命令，发送 /help 查看帮助")

    def _reply(self, chat_id: str, text: str):
        url = f"{self.api_base}/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        try:
            requests.post(url, json=payload, timeout=10,
                          proxies=self._proxies())
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 命令实现
    # ------------------------------------------------------------------
    def _cmd_help(self, chat_id: str):
        text = (
            "🤖 *MEV 夹子攻击防护 Bot*\n\n"
            "*命令列表:*\n"
            "/watch `<地址>` - 订阅钱包监控\n"
            "/unwatch `<地址>` - 取消订阅\n"
            "/status - 查看你的订阅列表\n"
            "/stats - 查看全局统计\n"
            "/help - 显示此帮助\n\n"
            "*工作原理:*\n"
            "Bot 实时监控链上交易，当检测到你订阅的钱包地址"
            "遭遇 MEV 夹子攻击时，自动推送告警与诊断报告。\n\n"
            "把此 Bot 拉入你的交易群即可使用，零门槛。"
        )
        self._reply(chat_id, text)

    def _cmd_watch(self, chat_id: str, address: str):
        ens_name = None
        if self.ens_resolver:
            ens_name = self.ens_resolver.resolve_ens(address)
        reply = self.sub_manager.subscribe(str(chat_id), address, ens_name)
        self._reply(chat_id, reply)

    def _cmd_unwatch(self, chat_id: str, address: str):
        reply = self.sub_manager.unsubscribe(str(chat_id), address)
        self._reply(chat_id, reply)

    def _cmd_status(self, chat_id: str):
        text = self.sub_manager.format_status(str(chat_id))
        self._reply(chat_id, text)

    def _cmd_stats(self, chat_id: str):
        attacks = self.sub_manager.get_stat("attacks_detected")
        subs = self.sub_manager.get_stat("total_subscriptions")
        text = (
            f"📊 *全局统计*\n\n"
            f"夹子攻击检测数: {attacks}\n"
            f"总订阅数: {subs}"
        )
        self._reply(chat_id, text)
