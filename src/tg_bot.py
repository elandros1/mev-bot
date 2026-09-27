"""
Notification Push Module
------------------------
Supports multi-channel push of sandwich attack diagnostic reports:
- Telegram Bot API (requires BOT_TOKEN / CHAT_ID)
- ntfy.sh (free, no registration required, just a topic name)
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

from .analyzer import SandwichReport
from .i18n import t, bi

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    """Telegram message pusher."""

    def __init__(self, bot_token: str, chat_id: str,
                 api_base: str = DEFAULT_API_BASE):
        self.bot_token = bot_token.strip()
        self.chat_id = str(chat_id).strip()
        self.api_base = api_base.rstrip("/")
        self.api_url = f"{self.api_base}/bot{self.bot_token}/sendMessage"
        self.enabled = bool(self.bot_token and self.chat_id)
        self.bot_username = ""
        # Auto-fetch Bot username on init (for Deep Linking)
        if self.bot_token:
            self._fetch_bot_username()

    def _fetch_bot_username(self):
        """Fetch Bot username via getMe API."""
        url = f"{self.api_base}/bot{self.bot_token}/getMe"
        try:
            resp = requests.post(url, json={}, timeout=10,
                                 proxies=self._proxies())
            if resp.status_code == 200 and resp.json().get("ok"):
                self.bot_username = resp.json()["result"].get("username", "")
                if self.bot_username:
                    logger.info("Bot identity: @%s", self.bot_username)
        except Exception as e:
            logger.debug("getMe failed: %s", e)

    @staticmethod
    def _proxies() -> Optional[dict]:
        """Read HTTP proxy config from environment variables."""
        proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                 or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
        return {"http": proxy, "https": proxy} if proxy else None

    @staticmethod
    def get_native_price_usd(symbol: str = "ETH") -> float:
        """Get native token USD price (returns 0 on failure)."""
        proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                 or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
        proxies = {"http": proxy, "https": proxy} if proxy else None

        # Try multiple price sources (sandbox may block some APIs)
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
                logger.debug("Price source %s failed: %s", url.split('/')[2], e)
                continue
        logger.warning("All price sources failed, cannot get %s price", symbol)
        return 0.0

    def _get_native_price_usd(self, symbol: str = "ETH") -> float:
        return self.get_native_price_usd(symbol)

    def format_report(self, report: SandwichReport, eth_price_usd: float = 0.0) -> str:
        """Format a diagnostic report as bilingual (Chinese / English) text using i18n locale files."""
        profit = report.attacker_profit_native
        loss = report.victim_loss_native
        profit_usd = profit * eth_price_usd if eth_price_usd else 0
        loss_usd = loss * eth_price_usd if eth_price_usd else 0

        if report.chain == "ethereum":
            chain_bi = bi("card.network_ethereum")
        else:
            chain_bi = bi("card.network_bsc")
        scan = report.scan_url

        # Victim display: prefer address, append ENS if available
        victim_display = report.victim_address or report.victim_tx[:10] + "..."
        if len(victim_display) == 42:
            victim_short = f"{victim_display[:10]}...{victim_display[-6:]}"
        else:
            victim_short = victim_display

        attacker_short = f"{report.attacker[:10]}...{report.attacker[-6:]}"

        # Profit / loss lines
        profit_line = f"*_{profit:.6f} {report.native_symbol}_*"
        if profit_usd:
            profit_line += f"  ≈ ${profit_usd:,.2f}"
        loss_line = f"*_{loss:.6f} {report.native_symbol}_*"
        if loss_usd:
            loss_line += f"  ≈ ${loss_usd:,.2f}"

        lines = [
            f"🚨 *{t('zh', 'card.alert_title')} / {t('en', 'card.alert_title')}* 🚨",
            "",
            f"📦 {bi('card.block_label')}: `#{report.block_number}`",
            f"🔗 {bi('card.network_label')}: {chain_bi}",
            "",
            f"👤 {bi('card.victim_label')}: `{victim_short}`",
            f"🕵️ {bi('card.attacker_label')}: `{attacker_short}`",
            f"💱 {bi('card.token_label')}: {report.token_symbol} ({report.token_amount:,.2f})",
            "",
            f"💰 {bi('card.profit_label')}: {profit_line}",
            f"📉 {bi('card.loss_label')}: {loss_line}",
            "",
            f"⚠️ *{bi('card.root_cause_title')}*",
            f"• {bi('card.root_cause_1')}",
            f"• {bi('card.root_cause_2')}",
            f"• {bi('card.root_cause_3')}",
            "",
            f"🛡️ *{bi('card.recommendations_title')}*",
            f"1. {bi('card.rec_1')}",
            f"2. {bi('card.rec_2')}",
            f"3. {bi('card.rec_3')}",
            "",
            f"[🔗 {bi('card.victim_tx_link')}]({scan}/tx/{report.victim_tx})",
            f"[🕵️ {bi('card.attacker_addr_link')}]({scan}/address/{report.attacker})",
        ]
        return "\n".join(lines)

    def _build_inline_keyboard(self, report: SandwichReport,
                               report_url: str = "") -> dict:
        """Build Telegram inline keyboard buttons.

        Button layout:
        Row 1: [Watch This Wallet]  (Deep Link -> Bot DM auto /watch)
        Row 2: [Victim Tx] [Attacker]
        Row 3: [MEV Protection Guide]  (Deep Link -> Bot DM)
        """
        buttons = []

        # Deep Link: click to open Bot DM, auto-trigger /start watch_<address>
        victim_addr = report.victim_address or ""
        if self.bot_username and victim_addr:
            deep_link = (
                f"https://t.me/{self.bot_username}"
                f"?start=watch_{victim_addr}"
            )
            buttons.append([{
                "text": f"🔔 {t('zh', 'buttons.watch_wallet')} / {t('en', 'buttons.watch_wallet')}",
                "url": deep_link,
            }])
        elif report_url:
            # Fallback to report link if no bot_username
            buttons.append([{
                "text": f"📋 {t('zh', 'buttons.view_report')} / {t('en', 'buttons.view_report')}",
                "url": report_url,
            }])

        # Row 2: Etherscan links
        row2 = []
        row2.append({
            "text": f"🔗 {bi('buttons.victim_tx')}",
            "url": f"{report.scan_url}/tx/{report.victim_tx}",
        })
        row2.append({
            "text": f"🕵️ {bi('buttons.attacker')}",
            "url": f"{report.scan_url}/address/{report.attacker}",
        })
        buttons.append(row2)

        # Row 3: Protection guide (Deep Link to Bot DM, no param -> /help)
        if self.bot_username:
            buttons.append([{
                "text": f"🛡️ {bi('buttons.protection_guide')}",
                "url": f"https://t.me/{self.bot_username}?start=help",
            }])

        return {"inline_keyboard": buttons} if buttons else {}

    def send(self, report: SandwichReport, report_url: str = "") -> bool:
        """Send bilingual diagnostic report to Telegram (with inline buttons)."""
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
                logger.info("Telegram report sent | block #%s | profit %.4f %s",
                            report.block_number, report.attacker_profit_native,
                            report.native_symbol)
                return True
            else:
                logger.error("Telegram send failed: %s", resp.text[:300])
                return False
        except Exception as e:
            logger.error("Telegram send exception: %s", e)
            return False

    def send_text(self, text: str) -> bool:
        """Send a plain text message (for startup/status notifications)."""
        return self.send_to_chat(self.chat_id, text)

    def send_to_chat(self, chat_id: str, text: str) -> bool:
        """Send a message to a specific chat_id (supports groups/users)."""
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
            logger.error("Telegram send_to_chat failed: %s", e)
            return False


class NtfyNotifier:
    """ntfy.sh message pusher (free, no registration required)."""

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
        """Send diagnostic report to ntfy.sh."""
        if not self.enabled:
            return False
        eth_price = TelegramNotifier.get_native_price_usd(report.native_symbol)
        text = TelegramNotifier("", "").format_report(report, eth_price)
        if report_url:
            text += f"\n\n📋 {bi('buttons.view_report')}:\n{report_url}"

        # Note: HTTP headers cannot contain non-ASCII; use English for title
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
                logger.info("ntfy report sent | block #%s | topic=%s",
                            report.block_number, self.topic)
                return True
            else:
                logger.error("ntfy send failed: %s", resp.text[:300])
                return False
        except Exception as e:
            logger.error("ntfy send exception: %s", e)
            return False

    def send_text(self, text: str, title: str = "MEV Bot") -> bool:
        """Send a plain text message."""
        if not self.enabled:
            return False
        headers = {"Title": title, "Priority": "3"}
        try:
            resp = requests.post(self.url, data=text.encode("utf-8"),
                                 headers=headers, timeout=10,
                                 proxies=self._proxies())
            return resp.status_code == 200
        except Exception as e:
            logger.error("ntfy text send failed: %s", e)
            return False


class MultiNotifier:
    """Unified multi-channel notifier: pushes to both Telegram and ntfy."""

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
        """Send a message to a specific chat_id (for Bot command replies)."""
        if self.telegram:
            self.telegram.send_to_chat(chat_id, text)


class TelegramCommandHandler:
    """Telegram Bot command handler (long-polling getUpdates)."""

    def __init__(self, bot_token: str, api_base: str,
                 sub_manager, ens_resolver=None):
        self.bot_token = bot_token
        self.api_base = api_base.rstrip("/")
        self.sub_manager = sub_manager
        self.ens_resolver = ens_resolver  # VictimOutreach instance
        self.offset = 0
        self.enabled = bool(bot_token)
        self.bot_username = ""
        # Auto-fetch Bot username on init (for Deep Linking)
        if self.enabled:
            info = self._api_call("getMe")
            if info and info.get("ok"):
                self.bot_username = info["result"].get("username", "")
                logger.info("Bot identity: @%s", self.bot_username)

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
            logger.error("Telegram API %s failed: %s", method, resp.text[:200])
        except Exception as e:
            logger.debug("Telegram API %s exception: %s", method, e)
        return None

    def poll_once(self) -> list:
        """Long-poll getUpdates once, returning list of messages to handle."""
        result = self._api_call("getUpdates", {
            "offset": self.offset,
            "timeout": 25,  # Long-poll timeout
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
        """Handle a single message: parse command and reply."""
        text = msg.get("text", "").strip()
        chat_id = msg["chat"]["id"]
        sender = msg.get("from", {})
        sender_name = sender.get("first_name", "") or sender.get("username", "")

        if not text.startswith("/"):
            return

        parts = text.split(maxsplit=2)
        cmd = parts[0].lower().split("@")[0]  # Strip @botname suffix
        args = parts[1:]

        # Deep Linking: /start watch_<address> -> auto-subscribe
        # Deep Linking: /start help -> show help
        if cmd == "/start" and args:
            payload = args[0]
            if payload.startswith("watch_"):
                address = payload[6:]  # Strip "watch_" prefix
                if address:
                    self._cmd_watch(chat_id, address, deep_link=True)
                    return
            elif payload == "help":
                self._cmd_help(chat_id)
                return
            self._cmd_help(chat_id)
        elif cmd == "/start" or cmd == "/help":
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
            self._reply(chat_id, f"❓ {bi('commands.unknown_command')}")

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
    # Command implementations
    # ------------------------------------------------------------------
    def _cmd_help(self, chat_id: str):
        text = (
            f"🤖 *{t('zh', 'commands.help_title')} / {t('en', 'commands.help_title')}*\n\n"
            f"*{t('zh', 'commands.help_commands')} / {t('en', 'commands.help_commands')}:*\n"
            f"{t('en', 'commands.help_watch')}\n"
            f"{t('en', 'commands.help_unwatch')}\n"
            f"{t('en', 'commands.help_status')}\n"
            f"{t('en', 'commands.help_stats')}\n"
            f"{t('en', 'commands.help_help')}\n\n"
            f"*{t('zh', 'commands.help_how_it_works')} / {t('en', 'commands.help_how_it_works')}*\n"
            f"{t('en', 'commands.help_description')}\n\n"
            f"{t('zh', 'commands.help_description')}"
        )
        self._reply(chat_id, text)

    def _cmd_watch(self, chat_id: str, address: str, deep_link: bool = False):
        ens_name = None
        if self.ens_resolver:
            ens_name = self.ens_resolver.resolve_ens(address)
        reply = self.sub_manager.subscribe(str(chat_id), address, ens_name)
        self._reply(chat_id, reply)
        if deep_link:
            self._reply(chat_id, f"🔔 {bi('commands.deep_link_onboarding')}")

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
            f"📊 *{bi('commands.stats_title')}*\n\n"
            f"{t('zh', 'commands.stats_attacks')} / {t('en', 'commands.stats_attacks')}: {attacks}\n"
            f"{t('zh', 'commands.stats_subscriptions')} / {t('en', 'commands.stats_subscriptions')}: {subs}"
        )
        self._reply(chat_id, text)
