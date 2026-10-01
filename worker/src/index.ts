/**
 * MEV Sandwich Bot — Cloudflare Workers entry point.
 *
 * Triggered by Cron every minute (configurable in wrangler.toml).
 * Each tick:
 *   1. Read last scanned block from the state Durable Object
 *   2. Scan all blocks since then for sandwich attacks
 *   3. For each attack: notify Telegram + ntfy, generate HTML report,
 *      identify victim, auto-mention on Farcaster if available,
 *      notify any subscribers watching the victim's wallet
 *   4. Persist the new block cursor
 *
 * Also handles Telegram webhook updates (commands, deep-link bindings)
 * for the subscription flow — `/watch`, `/unwatch`, `/status`, `/help`.
 */
import { ethers, JsonRpcProvider } from "ethers";
import { MEVAnalyzer } from "./analyzer";
import { MultiNotifier } from "./notifier";
import { VictimOutreach } from "./victim_outreach";
import { generateReportHtml } from "./report_generator";
import { MevState } from "./state";
import { t } from "./i18n";
import type { Env, SandwichReport } from "./types";

export { MevState } from "./state";

/** Type-safe accessor for the state DO stub. */
function getState(env: Env): DurableObjectStub<MevState> {
  const id = env.STATE.idFromName("global");
  return env.STATE.get(id) as DurableObjectStub<MevState>;
}

const STARTUP_BLOCK_OFFSET = 5; // start scanning 5 blocks behind latest

export default {
  /**
   * Cron trigger handler — the main detection loop entry point.
   */
  async scheduled(
    _controller: ScheduledController,
    env: Env,
    ctx: ExecutionContext,
  ): Promise<void> {
    const state = getState(env);
    let lastBlock = 0;
    try {
      // Heartbeat — throttled to every 5 min to avoid rate limits.
      const hbBody = `Worker alive (cron fired). NTFY_TOPIC=${env.NTFY_TOPIC ? "set" : "EMPTY"}. RPC_URL=${env.RPC_URL ? "set" : "EMPTY"}.`;
      const diag: Record<string, string> = { time: new Date().toISOString() };
      let shouldSendHb = true;
      try {
        const lastHb = await env.DIAG.get("last_hb_minute");
        const nowMin = Math.floor(Date.now() / 60000);
        if (lastHb && nowMin - parseInt(lastHb) < 5) {
          shouldSendHb = false;
        }
      } catch { /* ignore */ }
      if (shouldSendHb) {
        // Send heartbeat via notifier (uses ntfy with fallback + Telegram)
        const notifier = new MultiNotifier(env);
        const ntfyOk = await notifier.ntfy.sendText(hbBody, "MEV Bot Heartbeat");
        diag["ntfy"] = ntfyOk ? "OK" : "FAILED";
        // Telegram: send + diagnose if chat_id is wrong
        if (env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID) {
          const tgOk = await notifier.telegram.sendText(`🤖 Heartbeat\n${hbBody}`);
          diag["telegram"] = tgOk ? "OK" : "FAILED (chat not found? message @my_mev_xu_bot)";
          diag["chat_id_set"] = env.TELEGRAM_CHAT_ID;
          // Check for webhook + getUpdates to see if user messaged the bot
          try {
            const apiBase = (env.TELEGRAM_API_BASE || "https://api.telegram.org").replace(/\/$/, "");
            const wh = await fetch(`${apiBase}/bot${env.TELEGRAM_BOT_TOKEN}/getWebhookInfo`, { method: "GET" });
            diag["webhook_info"] = (await wh.text()).slice(0, 300);
            const upd = await fetch(`${apiBase}/bot${env.TELEGRAM_BOT_TOKEN}/getUpdates?limit=3`, { method: "GET" });
            diag["telegram_updates"] = (await upd.text()).slice(0, 600);
          } catch { /* ignore */ }
        }
        try {
          await env.DIAG.put("last_hb_minute", String(Math.floor(Date.now() / 60000)));
          await env.DIAG.put("hb_send_result", JSON.stringify(diag));
        } catch { /* ignore */ }
      } else {
        diag["heartbeat"] = "skipped (throttled)";
      }

      // Touch the DO early so we can confirm the cron fired even if later
      // steps fail.
      lastBlock = await state.getLastBlock();
      // Record DO state in diagnostics for remote verification
      try {
        const stats = await state.getStats();
        diag["last_block"] = String(lastBlock);
        diag["attacks_detected"] = String(stats.attacks);
        diag["subscriptions"] = String(stats.subscriptions);
      } catch { /* ignore */ }

      // Store diagnostics in KV for remote inspection
      try {
        await env.DIAG.put("heartbeat", JSON.stringify(diag));
      } catch { /* ignore */ }

      const chain = env.NETWORK === "bsc" ? "bsc" : "ethereum";
      const rpcUrl = chain === "ethereum" ? env.RPC_URL : (env.BSC_RPC_URL || env.RPC_URL);

      // No staticNetwork: let ethers auto-detect the chain id (one extra
      // eth_chainId call; avoids undefined-network issues).
      const provider = new JsonRpcProvider(rpcUrl);
      const analyzer = new MEVAnalyzer(provider, chain);
      const notifier = new MultiNotifier(env);
      const outreach = new VictimOutreach(provider, env);

      let latest: number;
      try {
        latest = await provider.getBlockNumber();
      } catch (e) {
        console.error("Failed to fetch latest block:", e);
        await state.recordError(`getBlockNumber failed: ${(e as Error).message}`);
        await notifier.ntfy.sendText(
          `RPC getBlockNumber failed: ${(e as Error).message}. lastBlock #${lastBlock}.`,
          "MEV Bot Error",
        );
        return;
      }
      // If too far behind (>50 blocks), skip ahead to avoid spending hours
      // catching up on stale blocks. Sandwich alerts are time-sensitive.
      const MAX_LAG = 50;
      const startBlock = (lastBlock === 0 || latest - lastBlock > MAX_LAG)
        ? Math.max(latest - STARTUP_BLOCK_OFFSET, 1)
        : lastBlock + 1;
      if (lastBlock > 0 && latest - lastBlock > MAX_LAG) {
        console.log(`Lag ${latest - lastBlock} blocks > ${MAX_LAG}, jumping to #${startBlock}`);
      }

      if (startBlock > latest) {
        console.log(`No new blocks since #${lastBlock} (latest #${latest})`);
        return;
      }

      // Cap iterations to keep cron within CPU limits.
      // Ethereum produces ~5 blocks/min; scan 6 to gradually catch up.
      const endBlock = Math.min(startBlock + 5, latest);
      console.log(`Scanning blocks ${startBlock} -> ${endBlock} (latest ${latest})`);

      for (let blockNum = startBlock; blockNum <= endBlock; blockNum++) {
        try {
          const swaps = await analyzer.fetchSwaps(blockNum);
          if (swaps.length === 0) {
            console.log(`Block #${blockNum}: no swaps`);
            continue;
          }
          const reports = await analyzer.detectSandwiches(swaps);
          console.log(
            `Block #${blockNum}: ${swaps.length} swaps, ${reports.length} sandwich attacks`,
          );

          for (const r of reports) {
            r.blockNumber = blockNum;
            await handleAttack(r, env, state, notifier, outreach);
          }
        } catch (e) {
          console.error(`Block #${blockNum} scan failed:`, e);
        }
      }

      await state.setLastBlock(endBlock);
      console.log(`Cursor updated -> #${endBlock}`);
    } catch (e) {
      const msg = `scheduled handler error: ${(e as Error).message}`;
      console.error(msg, e);
      try { await state.recordError(msg); } catch { /* ignore */ }
    }
  },

  /**
   * HTTP handler — Telegram webhook (Bot commands + deep-link bindings).
   *
   * Configure webhook once with:
   *   POST https://api.telegram.org/bot<TOKEN>/setWebhook
   *        ?url=https://<worker>.workers.dev/tg&...
   */
  async fetch(
    req: Request,
    env: Env,
    _ctx: ExecutionContext,
  ): Promise<Response> {
    const url = new URL(req.url);
    if (url.pathname === "/tg" && req.method === "POST") {
      return handleTelegramUpdate(req, env);
    }
    if (url.pathname === "/health") {
      return new Response("ok", { status: 200 });
    }
    if (url.pathname === "/debug") {
      const state = getState(env);
      const [lastBlock, lastError, stats] = await Promise.all([
        state.getLastBlock(),
        state.getLastError(),
        state.getStats(),
      ]);
      return new Response(JSON.stringify({
        ok: true,
        lastBlock,
        lastError,
        stats,
        time: new Date().toISOString(),
      }, null, 2), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    // Test endpoint: verify the Worker can reach ntfy.sh
    if (url.pathname === "/test-ntfy") {
      const topic = env.NTFY_TOPIC || "(none)";
      const server = env.NTFY_SERVER || "https://ntfy.sh";
      try {
        const r = await fetch(`${server}/${topic}`, {
          method: "POST",
          headers: {
            "Title": "MEV Bot Test",
            "Priority": "3",
            "Tags": "test_tube",
          },
          body: `Test from Worker. topic=${topic} server=${server}`,
        });
        const text = await r.text();
        return new Response(JSON.stringify({
          ok: r.ok,
          status: r.status,
          topic,
          server,
          response: text.slice(0, 200),
        }, null, 2), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      } catch (e) {
        return new Response(JSON.stringify({
          ok: false,
          error: (e as Error).message,
          topic,
          server,
        }, null, 2), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        });
      }
    }
    return new Response("MEV Sandwich Bot Worker", { status: 200 });
  },
};

// ---------------------------------------------------------------------------
// Per-attack handler
// ---------------------------------------------------------------------------
async function handleAttack(
  report: SandwichReport,
  env: Env,
  state: DurableObjectStub<MevState>,
  notifier: MultiNotifier,
  outreach: VictimOutreach,
): Promise<void> {
  console.log(
    `Sandwich detected | block #${report.blockNumber} | victim ${report.victimAddress.slice(0, 10)}... | attacker profit ${report.attackerProfitNative} ${report.nativeSymbol}`,
  );

  // 1. Identify victim
  let victimInfo = null;
  if (report.victimAddress) {
    victimInfo = await outreach.identifyVictim(report.victimAddress);
    console.log(
      `Victim identified: ENS=${victimInfo.ensName ?? "none"}, socials=${victimInfo.socialAccounts.length}`,
    );
  }

  // 2. Get native price + generate report
  const nativePrice = await getNativePriceUsd(report.nativeSymbol);
  const reportBase = env.REPORT_BASE_URL || "";
  const reportUrl = reportBase
    ? `${reportBase}/${report.chain}_${report.blockNumber}_${report.victimTx.slice(0, 10)}.html`
    : "";

  // 3. Public broadcast (Telegram + ntfy)
  await notifier.send(report, reportUrl, nativePrice);

  // 4. Zero-friction Farcaster outreach
  if (victimInfo && reportUrl) {
    try {
      await outreach.outreachToFarcaster(victimInfo, report, reportUrl);
    } catch (e) {
      console.error("Farcaster outreach failed:", e);
    }
  }

  // 5. Subscriber-targeted notifications
  if (report.victimAddress) {
    const subscribers = await state.getSubscribersForAddress(report.victimAddress);
    if (subscribers.length > 0) {
      console.log(`Notifying ${subscribers.length} subscriber(s) for ${report.victimAddress}`);
      const msg = VictimOutreach.formatOutreachMessage(
        victimInfo ?? ({} as any), reportUrl, report,
      );
      for (const sub of subscribers) {
        // Send to each subscriber's chat (uses Telegram directly)
        await sendTelegramMessage(env, sub.chatId, msg);
      }
    }
  }

  // 6. Stats
  await state.incrementAttacks();
}

// ---------------------------------------------------------------------------
// Native price (best-effort, multiple sources)
// ---------------------------------------------------------------------------
async function getNativePriceUsd(symbol = "ETH"): Promise<number> {
  const pair = symbol === "ETH" ? "ETH_USDT" : "BNB_USDT";
  const sources: Array<[string, (r: Response) => Promise<number>]> = [
    [
      `https://api.gateio.ws/api/v4/spot/tickers?currency_pair=${pair}`,
      async (r) => (await r.json() as Array<{ last: string }>)[0] ? parseFloat((await r.json() as any)[0].last) : 0,
    ],
    [
      `https://api.huobi.pro/market/detail/merged?symbol=${pair.toLowerCase()}`,
      async (r) => parseFloat(((await r.json() as any).tick?.close) ?? "0"),
    ],
    [
      `https://api.coingecko.com/api/v3/simple/price?ids=${symbol === "ETH" ? "ethereum" : "binancecoin"}&vs_currencies=usd`,
      async (r) => {
        const data = await r.json() as Record<string, { usd?: number }>;
        const first = Object.values(data)[0];
        return first?.usd ?? 0;
      },
    ],
  ];
  for (const [url, parser] of sources) {
    try {
      const r = await fetch(url, { cf: { cacheTtl: 30 } });
      if (r.ok) {
        const p = await parser(r);
        if (p > 0) return p;
      }
    } catch (e) {
      // Try next source
    }
  }
  return 0;
}

// ---------------------------------------------------------------------------
// Telegram webhook handler (Bot commands + Deep Link auto-binding)
// ---------------------------------------------------------------------------
async function handleTelegramUpdate(req: Request, env: Env): Promise<Response> {
  let update: any;
  try {
    update = await req.json();
  } catch {
    return new Response("bad json", { status: 400 });
  }

  const state = getState(env);

  const msg = update.message;
  if (!msg) return new Response("ok", { status: 200 });

  const chatId = String(msg.chat.id);
  const text: string = msg.text || "";

  // Deep Link handler: /start watch_<address> | /start help
  if (text.startsWith("/start ")) {
    const payload = text.split(" ")[1];
    if (payload.startsWith("watch_")) {
      const address = payload.slice(6);
      try {
        await state.addSubscription(chatId, address);
        const reply = `🔔 ${t("card.alert_title")}\n\n` +
          `✅ You have been auto-subscribed to wallet \`${address.slice(0, 10)}...${address.slice(-6)}\`.\n` +
          `You will receive alerts when this wallet is sandwiched.\n\n` +
          `Commands: /status, /unwatch <address>, /help`;
        await sendTelegramMessage(env, chatId, reply);
      } catch (e) {
        // Invalid address
      }
      return new Response("ok", { status: 200 });
    } else if (payload === "help") {
      return sendHelp(env, chatId);
    }
  }

  if (text.startsWith("/watch ")) {
    const address = text.split(" ")[1]?.trim();
    if (!address || !address.startsWith("0x") || address.length !== 42) {
      await sendTelegramMessage(env, chatId, `❌ ${t("subscription.invalid_address")}`);
    } else {
      await state.addSubscription(chatId, address);
      await sendTelegramMessage(
        env, chatId,
        `✅ ${t("subscription.subscribed")}\nAddress: \`${address.slice(0, 10)}...${address.slice(-6)}\``,
      );
    }
    return new Response("ok", { status: 200 });
  }

  if (text.startsWith("/unwatch ")) {
    const address = text.split(" ")[1]?.trim();
    const removed = await state.removeSubscription(chatId, address || "");
    await sendTelegramMessage(
      env, chatId,
      removed ? `✅ ${t("subscription.unsubscribed")}` : `❌ ${t("subscription.not_found")}`,
    );
    return new Response("ok", { status: 200 });
  }

  if (text === "/status") {
    const subs = await state.getSubscriptionsForChat(chatId);
    if (subs.length === 0) {
      await sendTelegramMessage(env, chatId, t("subscription.status_empty"));
    } else {
      const lines = [t("subscription.status_header")];
      for (const s of subs) {
        const a = s.address as string;
        lines.push(`• \`${a.slice(0, 10)}...${a.slice(-6)}\` ${s.ensName ? `(${s.ensName})` : ""}`);
      }
      lines.push("", t("subscription.status_footer"));
      await sendTelegramMessage(env, chatId, lines.join("\n"));
    }
    return new Response("ok", { status: 200 });
  }

  if (text === "/stats") {
    const stats = await state.getStats();
    await sendTelegramMessage(
      env, chatId,
      `${t("commands.stats_title")}:\n` +
      `• ${t("commands.stats_attacks")}: ${stats.attacks}\n` +
      `• ${t("commands.stats_subscriptions")}: ${stats.subscriptions}`,
    );
    return new Response("ok", { status: 200 });
  }

  if (text === "/help" || text === "/start") {
    return sendHelp(env, chatId);
  }

  await sendTelegramMessage(env, chatId, t("commands.unknown_command"));
  return new Response("ok", { status: 200 });
}

async function sendHelp(env: Env, chatId: string): Promise<Response> {
  const text = [
    `🤖 *${t("commands.help_title")}*`,
    "",
    `*${t("commands.help_commands")}*`,
    `${t("commands.help_watch")}`,
    `${t("commands.help_unwatch")}`,
    `${t("commands.help_status")}`,
    `${t("commands.help_stats")}`,
    `${t("commands.help_help")}`,
    "",
    `*${t("commands.help_how_it_works")}*`,
    t("commands.help_description"),
  ].join("\n");
  await sendTelegramMessage(env, chatId, text);
  return new Response("ok", { status: 200 });
}

/** Send a Telegram message to a specific chat (used for subscriber alerts + commands). */
async function sendTelegramMessage(env: Env, chatId: string, text: string): Promise<void> {
  if (!env.TELEGRAM_BOT_TOKEN) return;
  const apiBase = (env.TELEGRAM_API_BASE || "https://api.telegram.org").replace(/\/$/, "");
  const url = `${apiBase}/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`;
  try {
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        parse_mode: "Markdown",
        disable_web_page_preview: true,
      }),
    });
  } catch (e) {
    console.error("Telegram sendMessage failed:", e);
  }
}
