/**
 * Telegram Bot API Proxy Worker
 * ============================
 * Deploy to Cloudflare Workers (free) to forward Bot API requests in
 * environments that cannot directly reach api.telegram.org.
 *
 * Deployment steps:
 * 1. Open https://workers.cloudflare.com and sign in/register to Cloudflare
 * 2. Click "Create a Worker"
 * 3. Paste the entire contents of this file into the code editor
 * 4. Click "Deploy"
 * 5. Copy the Worker URL (e.g. https://tg-proxy.your-name.workers.dev)
 * 6. In the project .env, set: TELEGRAM_API_BASE="https://tg-proxy.your-name.workers.dev"
 *
 * Note: This Worker only forwards requests and stores no data; the Bot Token is safe.
 */

const TELEGRAM_API = "https://api.telegram.org";

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // Health check
    if (url.pathname === "/") {
      return new Response("Telegram Bot API Proxy is running.", {
        status: 200,
        headers: { "Content-Type": "text/plain" },
      });
    }

    // Build the forwarded Telegram URL
    const targetUrl = TELEGRAM_API + url.pathname + url.search;

    // Copy request headers (drop host, pass the rest through)
    const headers = new Headers(request.headers);
    headers.delete("host");

    const init = {
      method: request.method,
      headers,
      body: request.body,
      redirect: "follow",
    };

    try {
      const response = await fetch(targetUrl, init);
      // Copy response headers
      const respHeaders = new Headers(response.headers);
      respHeaders.set("Access-Control-Allow-Origin", "*");
      return new Response(response.body, {
        status: response.status,
        headers: respHeaders,
      });
    } catch (err) {
      return new Response(JSON.stringify({ ok: false, error: String(err) }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      });
    }
  },
};
