/**
 * Telegram Bot API 代理 Worker
 * ============================
 * 部署到 Cloudflare Workers（免费），用于在无法直连 api.telegram.org 的环境中
 * 转发 Bot API 请求。
 *
 * 部署步骤：
 * 1. 打开 https://workers.cloudflare.com 登录/注册 Cloudflare
 * 2. 点击 "Create a Worker"
 * 3. 将本文件全部内容粘贴到代码编辑器中
 * 4. 点击 "Deploy"
 * 5. 复制 Worker 的 URL（如 https://tg-proxy.your-name.workers.dev）
 * 6. 在项目 .env 中设置：TELEGRAM_API_BASE="https://tg-proxy.your-name.workers.dev"
 *
 * 注意：本 Worker 仅透传请求，不存储任何数据，Bot Token 安全。
 */

const TELEGRAM_API = "https://api.telegram.org";

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // 健康检查
    if (url.pathname === "/") {
      return new Response("Telegram Bot API Proxy is running.", {
        status: 200,
        headers: { "Content-Type": "text/plain" },
      });
    }

    // 构造转发到 Telegram 的 URL
    const targetUrl = TELEGRAM_API + url.pathname + url.search;

    // 复制请求头（移除 host，其余透传）
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
      // 复制响应头
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
