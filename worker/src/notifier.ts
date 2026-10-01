/**
 * Multi-channel notifier — TypeScript port of src/tg_bot.py.
 *
 * Pushes the diagnostic report via:
 * - Telegram Bot API (with inline buttons + Deep Linking)
 * - ntfy.sh (free push notifications)
 *
 * Both render the same alert card text as the Python bot did, using the
 * i18n locale file for all user-facing strings.
 */
import type { SandwichReport, Env } from "./types";
import { t } from "./i18n";

interface InlineButton {
  text: string;
  url: string;
}

/**
 * Format a sandwich report as the alert card text (Telegram Markdown).
 * Mirrors TelegramNotifier.format_report() from the Python bot.
 */
export function formatReport(
  report: SandwichReport,
  ethPriceUsd = 0,
  botUsername = "",
): string {
  const profit = report.attackerProfitNative;
  const loss = report.victimLossNative;
  const profitUsd = ethPriceUsd ? profit * ethPriceUsd : 0;
  const lossUsd = ethPriceUsd ? loss * ethPriceUsd : 0;

  const chainBi = report.chain === "ethereum"
    ? t("card.network_ethereum")
    : t("card.network_bsc");
  const scan = report.scanUrl;

  const victimDisplay = report.victimAddress || report.victimTx.slice(0, 10) + "...";
  const victimShort = victimDisplay.length === 42
    ? victimDisplay.slice(0, 10) + "..." + victimDisplay.slice(-6)
    : victimDisplay;
  const attackerShort = report.attacker.slice(0, 10) + "..." + report.attacker.slice(-6);

  let profitLine = `*_${profit.toFixed(6)} ${report.nativeSymbol}_*`;
  if (profitUsd) profitLine += `  ≈ $${profitUsd.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  let lossLine = `*_${loss.toFixed(6)} ${report.nativeSymbol}_*`;
  if (lossUsd) lossLine += `  ≈ $${lossUsd.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

  const lines = [
    `🚨 *${t("card.alert_title")}* 🚨`,
    "",
    `📦 ${t("card.block_label")}: \`#${report.blockNumber}\``,
    `🔗 ${t("card.network_label")}: ${chainBi}`,
    "",
    `👤 ${t("card.victim_label")}: \`${victimShort}\``,
    `🕵️ ${t("card.attacker_label")}: \`${attackerShort}\``,
    `💱 ${t("card.token_label")}: ${report.tokenSymbol} (${report.tokenAmount.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })})`,
    "",
    `💰 ${t("card.profit_label")}: ${profitLine}`,
    `📉 ${t("card.loss_label")}: ${lossLine}`,
    "",
    `⚠️ *${t("card.root_cause_title")}*`,
    `• ${t("card.root_cause_1")}`,
    `• ${t("card.root_cause_2")}`,
    `• ${t("card.root_cause_3")}`,
    "",
    `🛡️ *${t("card.recommendations_title")}*`,
    `1. ${t("card.rec_1")}`,
    `2. ${t("card.rec_2")}`,
    `3. ${t("card.rec_3")}`,
    "",
    `[🔗 ${t("card.victim_tx_link")}](${scan}/tx/${report.victimTx})`,
    `[🕵️ ${t("card.attacker_addr_link")}](${scan}/address/${report.attacker})`,
  ];
  return lines.join("\n");
}

/**
 * Build the Telegram inline keyboard for an alert.
 * Mirrors TelegramNotifier._build_inline_keyboard() in Python.
 */
export function buildInlineKeyboard(
  report: SandwichReport,
  reportUrl: string,
  botUsername: string,
): InlineButton[][] {
  const buttons: InlineButton[][] = [];
  const victimAddr = report.victimAddress;

  // Row 1: Watch This Wallet (Deep Link -> Bot DM auto /watch)
  if (botUsername && victimAddr) {
    const deepLink = `https://t.me/${botUsername}?start=watch_${victimAddr}`;
    buttons.push([{ text: `🔔 ${t("buttons.watch_wallet")}`, url: deepLink }]);
  } else if (reportUrl) {
    buttons.push([{ text: `📋 ${t("buttons.view_report")}`, url: reportUrl }]);
  }

  // Row 2: Etherscan links
  buttons.push([
    { text: `🔗 ${t("buttons.victim_tx")}`, url: `${report.scanUrl}/tx/${report.victimTx}` },
    { text: `🕵️ ${t("buttons.attacker")}`, url: `${report.scanUrl}/address/${report.attacker}` },
  ]);

  // Row 3: Protection guide (Deep Link to Bot DM -> /help)
  if (botUsername) {
    buttons.push([{
      text: `🛡️ ${t("buttons.protection_guide")}`,
      url: `https://t.me/${botUsername}?start=help`,
    }]);
  }

  return buttons;
}

/**
 * Telegram Bot API notifier.
 */
export class TelegramNotifier {
  constructor(private env: Env) {}

  private get enabled(): boolean {
    return Boolean(this.env.TELEGRAM_BOT_TOKEN && this.env.TELEGRAM_CHAT_ID);
  }

  private get apiBase(): string {
    return (this.env.TELEGRAM_API_BASE || "https://api.telegram.org").replace(/\/$/, "");
  }

  /** Fetch the bot username once (cached via env). */
  private botUsernameCache = "";
  private async getBotUsername(): Promise<string> {
    if (this.botUsernameCache) return this.botUsernameCache;
    if (!this.env.TELEGRAM_BOT_TOKEN) return "";
    try {
      const url = `${this.apiBase}/bot${this.env.TELEGRAM_BOT_TOKEN}/getMe`;
      const r = await fetch(url, { method: "POST", body: "{}", headers: { "Content-Type": "application/json" } });
      const data = await r.json() as { ok?: boolean; result?: { username?: string } };
      if (r.ok && data.ok && data.result?.username) {
        this.botUsernameCache = data.result.username;
      }
    } catch (e) {
      // Ignore — bot username not strictly required
    }
    return this.botUsernameCache;
  }

  /** Send a sandwich report to the configured chat. */
  async sendReport(report: SandwichReport, reportUrl: string, ethPriceUsd = 0): Promise<boolean> {
    if (!this.enabled) return false;
    const username = await this.getBotUsername();
    const text = formatReport(report, ethPriceUsd, username);
    const keyboard = buildInlineKeyboard(report, reportUrl, username);

    const payload: Record<string, unknown> = {
      chat_id: this.env.TELEGRAM_CHAT_ID,
      text,
      parse_mode: "Markdown",
      disable_web_page_preview: true,
    };
    if (keyboard.length > 0) payload.reply_markup = { inline_keyboard: keyboard };

    const url = `${this.apiBase}/bot${this.env.TELEGRAM_BOT_TOKEN}/sendMessage`;
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await r.json() as { ok?: boolean };
      if (r.ok && data.ok) {
        console.log(`Telegram report sent | block #${report.blockNumber}`);
        return true;
      }
      console.error("Telegram send failed:", JSON.stringify(data));
      return false;
    } catch (e) {
      console.error("Telegram send exception:", e);
      return false;
    }
  }

  /** Send a plain text message (e.g. startup notice). */
  async sendText(text: string): Promise<boolean> {
    if (!this.enabled) return false;
    const url = `${this.apiBase}/bot${this.env.TELEGRAM_BOT_TOKEN}/sendMessage`;
    const payload = {
      chat_id: this.env.TELEGRAM_CHAT_ID,
      text,
      parse_mode: "Markdown",
      disable_web_page_preview: true,
    };
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      return r.ok;
    } catch (e) {
      console.error("Telegram send_text failed:", e);
      return false;
    }
  }
}

/**
 * ntfy notifier (free, no registration required).
 * Uses the configured server with fallback to known-working public servers.
 */
export class NtfyNotifier {
  // Known ntfy servers that work from Cloudflare Workers.
  // ntfy.sh rate-limits Cloudflare IPs (429), so it's last in the fallback list.
  private static readonly FALLBACK_SERVERS = [
    "https://ntfy.private.coffee",
    "https://ntfy.sh",
  ];

  constructor(private env: Env) {}

  private get enabled(): boolean {
    return Boolean(this.env.NTFY_TOPIC);
  }

  private get primaryServer(): string {
    return (this.env.NTFY_SERVER || "https://ntfy.private.coffee").replace(/\/$/, "");
  }

  /** Get the list of servers to try, primary first then fallbacks. */
  private getServers(): string[] {
    const servers = [this.primaryServer];
    for (const s of NtfyNotifier.FALLBACK_SERVERS) {
      if (!servers.includes(s)) servers.push(s);
    }
    return servers;
  }

  /** Try to POST to each server until one succeeds. Returns the success status. */
  private async post(body: string, headers: Record<string, string>): Promise<boolean> {
    if (!this.enabled) return false;
    for (const server of this.getServers()) {
      try {
        const r = await fetch(`${server}/${this.env.NTFY_TOPIC}`, {
          method: "POST",
          headers,
          body,
        });
        if (r.ok) {
          console.log(`ntfy sent via ${server}`);
          return true;
        }
        console.error(`ntfy ${server} failed: HTTP ${r.status}`);
      } catch (e) {
        console.error(`ntfy ${server} exception:`, (e as Error).message);
      }
    }
    return false;
  }

  /** Send a sandwich report to ntfy. */
  async sendReport(
    report: SandwichReport,
    reportUrl: string,
    ethPriceUsd = 0,
  ): Promise<boolean> {
    if (!this.enabled) return false;
    const text = formatReport(report, ethPriceUsd) +
      (reportUrl ? `\n\n📋 ${t("buttons.view_report")}:\n${reportUrl}` : "");

    const headers: Record<string, string> = {
      "Title": `MEV Sandwich Alert #${report.blockNumber}`,
      "Priority": "4",
      "Tags": "warning,rotating_light",
    };
    if (reportUrl) headers["Click"] = reportUrl;

    return this.post(text, headers);
  }

  /** Send a plain text message to ntfy (e.g. startup notice). */
  async sendText(text: string, title = "MEV Sandwich Bot"): Promise<boolean> {
    return this.post(text, {
      "Title": title,
      "Priority": "3",
      "Tags": "information_source",
    });
  }
}

/**
 * Multi-channel notifier: fans out to all configured channels.
 */
export class MultiNotifier {
  telegram: TelegramNotifier;
  ntfy: NtfyNotifier;

  constructor(env: Env) {
    this.telegram = new TelegramNotifier(env);
    this.ntfy = new NtfyNotifier(env);
  }

  async send(report: SandwichReport, reportUrl: string, ethPriceUsd = 0): Promise<void> {
    await Promise.all([
      this.telegram.sendReport(report, reportUrl, ethPriceUsd),
      this.ntfy.sendReport(report, reportUrl, ethPriceUsd),
    ]);
  }

  /** Fan a plain text message out to all configured channels (e.g. startup). */
  async sendText(text: string): Promise<void> {
    await Promise.all([
      this.telegram.sendText(text),
      this.ntfy.sendText(text),
    ]);
  }
}
