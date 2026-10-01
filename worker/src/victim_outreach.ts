/**
 * Victim outreach — TypeScript port of src/victim_outreach.py.
 *
 * When a sandwich attack is detected:
 * 1. Look up the victim's ENS name (reverse lookup)
 * 2. Read ENS text records (Farcaster, Twitter, etc.)
 * 3. Query Neynar for the victim's Farcaster profile (if API key set)
 * 4. Auto-mention the victim on Farcaster (if signer UUID set) — zero
 *    opt-in, zero subscription, fully passive outreach
 *
 * The ENS reverse lookup uses on-chain calls against the ENS registry.
 */
import { ethers, JsonRpcProvider, Contract } from "ethers";
import type { SandwichReport, Env } from "./types";
import { t } from "./i18n";

const ENS_REGISTRY = "0x00000000C2E074eC69A0dFb2997BA6C7d2e1e";

const ENS_REGISTRY_ABI = [
  "function resolver(bytes32 node) view returns (address)",
];
const ENS_RESOLVER_ABI = [
  "function name(bytes32 node) view returns (string)",
  "function text(bytes32 node, string key) view returns (string)",
];

const ENS_TEXT_KEYS = [
  "com.twitter", "com.github", "org.telegram",
  "com.discord", "url", "description", "email",
];

interface SocialAccount {
  platform: string;
  handle: string;
  url: string;
  fid?: number;   // Farcaster user id (for true mention notifications)
}

interface VictimInfo {
  address: string;
  ensName: string | null;
  socialAccounts: SocialAccount[];
  ensTextRecords: Record<string, string>;
}

/**
 * Victim identification + Farcaster outreach.
 */
export class VictimOutreach {
  private ensRegistry: Contract | null = null;
  private fcReady: boolean;

  constructor(
    private provider: JsonRpcProvider,
    private env: Env,
  ) {
    try {
      this.ensRegistry = new ethers.Contract(
        ethers.getAddress(ENS_REGISTRY),
        ENS_REGISTRY_ABI,
        provider,
      );
      console.log("ENS reverse-lookup module loaded");
    } catch (e) {
      console.warn("ENS contract init failed:", e);
    }
    this.fcReady = Boolean(env.NEYNAR_API_KEY && env.NEYNAR_SIGNER_UUID);
  }

  // -------------------------------------------------------------------
  // ENS helpers
  // -------------------------------------------------------------------
  /** Compute ENS namehash for a name (e.g. "vitalik.eth"). */
  private static namehash(name: string): string {
    let node = "0x" + "0".repeat(64); // 32 zero bytes
    if (!name) return node;
    const labels = name.split(".").reverse();
    for (const label of labels) {
      const labelHash = ethers.keccak256(ethers.toUtf8Bytes(label));
      node = ethers.keccak256(
        ethers.concat([ethers.getBytes(node), ethers.getBytes(labelHash)]),
      );
    }
    return node;
  }

  /** Compute the reverse-lookup node for an address. */
  private static reverseNode(address: string): string {
    const addrLower = address.toLowerCase().replace("0x", "");
    return VictimOutreach.namehash(`${addrLower}.addr.reverse`);
  }

  // -------------------------------------------------------------------
  // ENS reverse lookup
  // -------------------------------------------------------------------
  async resolveEns(address: string): Promise<string | null> {
    if (!this.ensRegistry) return null;
    try {
      const node = VictimOutreach.reverseNode(address);
      const resolverAddr = await this.ensRegistry.resolver(node) as string;
      if (resolverAddr === ethers.ZeroAddress) return null;

      const resolver = new ethers.Contract(
        ethers.getAddress(resolverAddr),
        ENS_RESOLVER_ABI,
        this.provider,
      );
      const name = await resolver.name(node) as string;
      if (name) {
        console.log(`ENS reverse lookup: ${address.slice(0, 10)}... -> ${name}`);
        return name;
      }
    } catch (e) {
      // Ignore
    }
    return null;
  }

  /** Read ENS text records (Twitter, GitHub, Farcaster, etc.). */
  async getEnsTextRecords(ensName: string): Promise<Record<string, string>> {
    const records: Record<string, string> = {};
    if (!this.ensRegistry) return records;
    try {
      const node = VictimOutreach.namehash(ensName);
      const resolverAddr = await this.ensRegistry.resolver(node) as string;
      if (resolverAddr === ethers.ZeroAddress) return records;

      const resolver = new ethers.Contract(
        ethers.getAddress(resolverAddr),
        ENS_RESOLVER_ABI,
        this.provider,
      );
      await Promise.all(ENS_TEXT_KEYS.map(async (key) => {
        try {
          const val = await resolver.text(node, key) as string;
          if (val) records[key] = val;
        } catch (e) {
          // Skip missing keys
        }
      }));
    } catch (e) {
      // Ignore
    }
    return records;
  }

  // -------------------------------------------------------------------
  // Farcaster lookup via Neynar
  // -------------------------------------------------------------------
  async lookupFarcaster(address: string): Promise<SocialAccount | null> {
    if (!this.env.NEYNAR_API_KEY) return null;
    try {
      const url = `https://api.neynar.com/v2/farcaster/user/by_address?address=${address}`;
      const r = await fetch(url, {
        headers: { api_key: this.env.NEYNAR_API_KEY },
      });
      if (r.ok) {
        const data = await r.json() as { users?: Array<{ username?: string; fid?: number }> };
        if (data.users && data.users.length > 0) {
          const u = data.users[0];
          return {
            platform: "farcaster",
            handle: u.username || "",
            url: `https://warpcast.com/${u.username || ""}`,
            fid: u.fid,
          };
        }
      }
    } catch (e) {
      // Ignore
    }
    return null;
  }

  // -------------------------------------------------------------------
  // Comprehensive victim identification
  // -------------------------------------------------------------------
  async identifyVictim(address: string): Promise<VictimInfo> {
    const info: VictimInfo = {
      address,
      ensName: null,
      socialAccounts: [],
      ensTextRecords: {},
    };

    const ensName = await this.resolveEns(address);
    if (ensName) {
      info.ensName = ensName;
      const records = await this.getEnsTextRecords(ensName);
      info.ensTextRecords = records;

      const mapping: Record<string, [string, string]> = {
        "com.twitter": ["twitter", "https://twitter.com/"],
        "com.github": ["github", "https://github.com/"],
        "org.telegram": ["telegram", "https://t.me/"],
        "com.discord": ["discord", ""],
        "url": ["website", ""],
      };
      for (const [key, val] of Object.entries(records)) {
        if (key in mapping) {
          const [platform, urlPrefix] = mapping[key];
          info.socialAccounts.push({
            platform,
            handle: val,
            url: urlPrefix ? `${urlPrefix}${val}` : val,
          });
        }
      }
    }

    const fc = await this.lookupFarcaster(address);
    if (fc) info.socialAccounts.push(fc);

    return info;
  }

  // -------------------------------------------------------------------
  // Farcaster auto-mention (zero-friction victim outreach)
  // -------------------------------------------------------------------
  /** Send a cast via the Neynar v2 API. */
  async sendFarcasterCast(
    text: string,
    mentions: number[] = [],
    embedUrl = "",
  ): Promise<boolean> {
    if (!this.env.NEYNAR_API_KEY || !this.env.NEYNAR_SIGNER_UUID) {
      console.debug("Farcaster cast skipped: missing API key or signer UUID");
      return false;
    }

    const payload: Record<string, unknown> = {
      signer_uuid: this.env.NEYNAR_SIGNER_UUID,
      text,
    };
    if (mentions.length > 0) payload.mentions = mentions;
    if (embedUrl) payload.embeds = [{ url: embedUrl }];

    try {
      const r = await fetch("https://api.neynar.com/v2/farcaster/cast", {
        method: "POST",
        headers: {
          api_key: this.env.NEYNAR_API_KEY,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      });
      if (r.ok) {
        const data = await r.json() as { cast?: { hash?: string } };
        console.log(`Farcaster cast sent | hash=${data.cast?.hash ?? ""}`);
        return true;
      }
      console.error(`Farcaster cast failed | HTTP ${r.status}`);
      return false;
    } catch (e) {
      console.error("Farcaster cast exception:", e);
      return false;
    }
  }

  /**
   * Auto-mention the victim on Farcaster with a diagnostic summary.
   * Victim receives the alert next time they open Warpcast — zero opt-in.
   */
  async outreachToFarcaster(
    victimInfo: VictimInfo,
    report: SandwichReport,
    reportUrl: string,
  ): Promise<boolean> {
    const fc = victimInfo.socialAccounts.find(
      (s) => s.platform === "farcaster",
    );
    if (!fc) {
      console.debug("Victim has no Farcaster account; skipping cast");
      return false;
    }

    const username = fc.handle;
    const fid = fc.fid;
    const lossStr = `${report.victimLossNative.toFixed(4)} ${report.nativeSymbol}`;
    const attackerTail = report.attacker.slice(0, 6) + "..." + report.attacker.slice(-4);
    const txTail = report.victimTx.slice(0, 10) + "..." + report.victimTx.slice(-4);

    let text =
      `@${username} you were hit by a MEV sandwich attack.\n` +
      `Loss: ${lossStr} | Attacker: ${attackerTail}\n` +
      `Victim tx: ${txTail}\n` +
      `Diagnostic report below 👇`;
    if (text.length > 320) text = text.slice(0, 317) + "...";

    // Farcaster mentions are by fid — the @username text alone is NOT
    // enough to trigger a notification. Pass the fid so Neynar turns the
    // @username into a real mention the victim gets pinged for.
    return this.sendFarcasterCast(text, fid ? [fid] : [], reportUrl);
  }

  /** Format a human-readable outreach message (for logs / fallback channels). */
  static formatOutreachMessage(
    victimInfo: VictimInfo,
    reportUrl: string,
    report: SandwichReport,
  ): string {
    const ens = victimInfo.ensName || "";
    const addr = victimInfo.address;
    const label = ens ? `${ens} (${addr.slice(0, 10)}...)` : `${addr.slice(0, 10)}...${addr.slice(-6)}`;

    const lines = [
      `📢 ${t("outreach.to")} ${label}`,
      "",
      t("outreach.detected"),
      "",
      t("outreach.report_summary"),
      "",
      `🔗 ${t("outreach.tx")}: ${report.victimTx.slice(0, 20)}...`,
      `💰 ${t("outreach.estimated_loss")}: ${report.victimLossNative} ${report.nativeSymbol}`,
      `🕵️ ${t("outreach.attacker")}: ${report.attacker.slice(0, 20)}...`,
      "",
      `📋 ${t("outreach.full_report")}: ${reportUrl}`,
      "",
      t("outreach.recommendations"),
      `1. ${t("outreach.rec_1")}`,
      `2. ${t("outreach.rec_2")}`,
      `3. ${t("outreach.rec_3")}`,
    ];

    if (victimInfo.socialAccounts.length > 0) {
      lines.push("");
      lines.push(`📱 ${t("outreach.social_verified")}`);
      for (const s of victimInfo.socialAccounts) {
        lines.push(`  - ${s.platform}: @${s.handle}`);
      }
    }
    return lines.join("\n");
  }
}
