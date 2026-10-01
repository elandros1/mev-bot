/**
 * Durable Object for persistent state:
 * - last scanned block number (resumes across cron runs)
 * - subscriber list (chat_id <-> wallet address)
 *
 * Cloudflare Workers are stateless across invocations, so this DO
 * provides the shared memory the cron tick reads / writes.
 */
import { DurableObject } from "cloudflare:workers";

export interface Subscription {
  chatId: string;
  address: string;     // lowercase
  ensName?: string | null;
  createdAt: number;
}

export class MevState extends DurableObject {
  // Block cursor ------------------------------------------------------
  async getLastBlock(): Promise<number> {
    const v = await this.ctx.storage.get<number>("lastBlock");
    return v ?? 0;
  }
  async setLastBlock(n: number): Promise<void> {
    await this.ctx.storage.put("lastBlock", n);
  }

  // Subscriptions -----------------------------------------------------
  async addSubscription(chatId: string, address: string, ensName?: string | null): Promise<void> {
    const key = `sub:${chatId}:${address.toLowerCase()}`;
    const sub: Subscription = {
      chatId,
      address: address.toLowerCase(),
      ensName: ensName ?? null,
      createdAt: Date.now(),
    };
    await this.ctx.storage.put(key, sub);
  }

  async removeSubscription(chatId: string, address: string): Promise<boolean> {
    const key = `sub:${chatId}:${address.toLowerCase()}`;
    const existing = await this.ctx.storage.get<Subscription>(key);
    if (!existing) return false;
    await this.ctx.storage.delete(key);
    return true;
  }

  async getSubscriptionsForChat(chatId: string): Promise<Subscription[]> {
    const list = await this.ctx.storage.list<Subscription>({
      prefix: `sub:${chatId}:`,
    });
    return [...list.values()];
  }

  /** Get all subscribers watching the given address. */
  async getSubscribersForAddress(address: string): Promise<Subscription[]> {
    const target = address.toLowerCase();
    const list = await this.ctx.storage.list<Subscription>({ prefix: "sub:" });
    return [...list.values()].filter((s) => s.address === target);
  }

  async countSubscriptions(): Promise<number> {
    const list = await this.ctx.storage.list<Subscription>({ prefix: "sub:" });
    return list.size;
  }

  // Stats -------------------------------------------------------------
  async incrementAttacks(): Promise<void> {
    const n = await this.ctx.storage.get<number>("attacks") ?? 0;
    await this.ctx.storage.put("attacks", n + 1);
  }
  async getStats(): Promise<{ attacks: number; subscriptions: number }> {
    const attacksVal = await this.ctx.storage.get<number>("attacks");
    const subs = await this.countSubscriptions();
    return { attacks: attacksVal ?? 0, subscriptions: subs };
  }

  // Error logging -----------------------------------------------------
  async recordError(message: string): Promise<void> {
    await this.ctx.storage.put("lastError", {
      message,
      at: Date.now(),
    });
  }
  async getLastError(): Promise<{ message: string; at: number } | null> {
    return (await this.ctx.storage.get<{ message: string; at: number }>("lastError")) ?? null;
  }
}
