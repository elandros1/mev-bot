"""
Victim Outreach Module
----------------------
When a sandwich attack is detected:
1. Extract the victim's wallet address
2. Reverse-lookup ENS name
3. Search for Web3 social accounts (Farcaster / Lens / ENS text records)
4. Push the diagnostic report to the victim via available channels
   - Farcaster auto-mention (zero-friction: victim receives a cast without opting in)
   - ntfy / Telegram (operator-side monitoring)
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

import requests
from web3 import Web3

from .i18n import t

logger = logging.getLogger(__name__)


class VictimOutreach:
    """Automatic victim identification and outreach."""

    # ENS Registry contract address (Ethereum mainnet)
    ENS_REGISTRY = "0x00000000000C2E074eC69A0dFb2997BA6C7d2e1e"

    # ENS Registry ABI (resolver function only)
    ENS_REGISTRY_ABI = [
        {"constant": True, "inputs": [{"name": "node", "type": "bytes32"}],
         "name": "resolver", "outputs": [{"name": "", "type": "address"}],
         "type": "function"},
    ]

    # ENS Resolver ABI (name + text functions)
    ENS_RESOLVER_ABI = [
        {"constant": True, "inputs": [{"name": "node", "type": "bytes32"}],
         "name": "name", "outputs": [{"name": "", "type": "string"}],
         "type": "function"},
        {"constant": True,
         "inputs": [{"name": "node", "type": "bytes32"},
                    {"name": "key", "type": "string"}],
         "name": "text", "outputs": [{"name": "", "type": "string"}],
         "type": "function"},
    ]

    def __init__(self, w3=None, neynar_api_key: str = "",
                 neynar_signer_uuid: str = "",
                 lens_api_url: str = "https://api.lens.dev/graphql"):
        self.w3 = w3
        self.neynar_api_key = neynar_api_key or os.getenv("NEYNAR_API_KEY", "")
        self.neynar_signer_uuid = (
            neynar_signer_uuid or os.getenv("NEYNAR_SIGNER_UUID", "")
        )
        self.lens_api_url = lens_api_url
        self._ens_registry = None

        if w3 is not None:
            try:
                self._ens_registry = w3.eth.contract(
                    address=Web3.to_checksum_address(self.ENS_REGISTRY),
                    abi=self.ENS_REGISTRY_ABI,
                )
                logger.info("ENS reverse-lookup module loaded (native contract calls)")
            except Exception as e:
                logger.warning("ENS contract initialization failed: %s", e)

    @staticmethod
    def _proxies() -> Optional[dict]:
        proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                 or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
        return {"http": proxy, "https": proxy} if proxy else None

    @staticmethod
    def _namehash(name: str) -> bytes:
        """Compute ENS namehash."""
        if not name:
            return b'\x00' * 32
        labels = name.split(".")
        node = b'\x00' * 32
        for label in reversed(labels):
            label_hash = Web3.keccak(text=label)
            node = Web3.keccak(node + label_hash)
        return node

    @staticmethod
    def _reverse_node(address: str) -> bytes:
        """Compute the reverse-lookup node for an address."""
        addr_lower = address.lower().replace("0x", "")
        reverse_name = f"{addr_lower}.addr.reverse"
        return VictimOutreach._namehash(reverse_name)

    # ------------------------------------------------------------------
    # ENS Reverse Lookup
    # ------------------------------------------------------------------
    def resolve_ens(self, address: str) -> Optional[str]:
        """Reverse-lookup ENS name via ENS Registry contract (address -> name)."""
        if self._ens_registry is None or self.w3 is None:
            return None
        try:
            node = self._reverse_node(address)
            resolver_addr = self._ens_registry.functions.resolver(node).call()
            if resolver_addr == "0x0000000000000000000000000000000000000000":
                return None

            resolver = self.w3.eth.contract(
                address=Web3.to_checksum_address(resolver_addr),
                abi=self.ENS_RESOLVER_ABI,
            )
            name = resolver.functions.name(node).call()
            if name:
                logger.info("ENS reverse lookup: %s -> %s", address[:10] + "...", name)
                return name
        except Exception as e:
            logger.debug("ENS reverse lookup failed for %s: %s", address[:10] + "...", e)
        return None

    # ------------------------------------------------------------------
    # ENS text records (social accounts)
    # ------------------------------------------------------------------
    def get_ens_text_records(self, ens_name: str) -> Dict[str, str]:
        """Read ENS text records (Twitter, GitHub, Farcaster, etc.)."""
        if self._ens_registry is None or self.w3 is None:
            return {}
        records: Dict[str, str] = {}
        keys = ["com.twitter", "com.github", "org.telegram",
                "com.discord", "url", "description", "email"]
        try:
            node = self._namehash(ens_name)
            resolver_addr = self._ens_registry.functions.resolver(node).call()
            if resolver_addr == "0x0000000000000000000000000000000000000000":
                return {}
            resolver = self.w3.eth.contract(
                address=Web3.to_checksum_address(resolver_addr),
                abi=self.ENS_RESOLVER_ABI,
            )
            for key in keys:
                try:
                    val = resolver.functions.text(node, key).call()
                    if val:
                        records[key] = val
                except Exception:
                    continue
        except Exception as e:
            logger.debug("Failed to read ENS text records: %s", e)
        return records

    # ------------------------------------------------------------------
    # Farcaster lookup (requires Neynar API key)
    # ------------------------------------------------------------------
    def lookup_farcaster(self, address: str) -> Optional[Dict[str, Any]]:
        """Look up Farcaster profile via Neynar API (requires API key)."""
        if not self.neynar_api_key:
            return None
        try:
            headers = {"api_key": self.neynar_api_key}
            params = {"address": address}
            resp = requests.get(
                "https://api.neynar.com/v2/farcaster/user/by_address",
                headers=headers, params=params,
                timeout=8, proxies=self._proxies(),
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("users"):
                    user = data["users"][0]
                    return {
                        "platform": "farcaster",
                        "username": user.get("username", ""),
                        "fid": user.get("fid", 0),
                        "profile_url": f"https://warpcast.com/{user.get('username', '')}",
                    }
        except Exception as e:
            logger.debug("Farcaster lookup failed: %s", e)
        return None

    # ------------------------------------------------------------------
    # Lens Protocol lookup
    # ------------------------------------------------------------------
    def lookup_lens(self, address: str) -> Optional[Dict[str, Any]]:
        """Look up Lens profile via Lens GraphQL API."""
        try:
            query = """
                query {
                    defaultProfile(request: { address: "%s" }) {
                        id
                        handle { localName }
                        metadata { DisplayName }
                    }
                }
            """ % address
            resp = requests.post(
                self.lens_api_url,
                json={"query": query},
                timeout=8, proxies=self._proxies(),
            )
            if resp.status_code == 200:
                data = resp.json()
                profile = data.get("data", {}).get("defaultProfile")
                if profile:
                    handle = profile.get("handle", {})
                    local_name = handle.get("localName", "") if handle else ""
                    return {
                        "platform": "lens",
                        "username": local_name,
                        "profile_url": f"https://hey.xyz/u/{local_name}",
                    }
        except Exception as e:
            logger.debug("Lens lookup failed: %s", e)
        return None

    # ------------------------------------------------------------------
    # Comprehensive lookup
    # ------------------------------------------------------------------
    def identify_victim(self, address: str) -> Dict[str, Any]:
        """Comprehensive lookup of victim's on-chain identity information."""
        info: Dict[str, Any] = {
            "address": address,
            "ens_name": None,
            "social_accounts": [],
            "ens_text_records": {},
        }

        # 1. ENS reverse lookup
        ens_name = self.resolve_ens(address)
        if ens_name:
            info["ens_name"] = ens_name
            # 2. Read ENS text records
            records = self.get_ens_text_records(ens_name)
            if records:
                info["ens_text_records"] = records
                # Map to social accounts
                mapping = {
                    "com.twitter": ("twitter", "https://twitter.com/"),
                    "com.github": ("github", "https://github.com/"),
                    "org.telegram": ("telegram", "https://t.me/"),
                    "com.discord": ("discord", ""),
                    "url": ("website", ""),
                }
                for key, val in records.items():
                    if key in mapping:
                        platform, url_prefix = mapping[key]
                        info["social_accounts"].append({
                            "platform": platform,
                            "handle": val,
                            "url": f"{url_prefix}{val}" if url_prefix else val,
                        })

        # 3. Farcaster (requires API key)
        fc = self.lookup_farcaster(address)
        if fc:
            info["social_accounts"].append(fc)

        # 4. Lens
        lens = self.lookup_lens(address)
        if lens:
            info["social_accounts"].append(lens)

        return info

    # ------------------------------------------------------------------
    # Generate outreach message
    # ------------------------------------------------------------------
    @staticmethod
    def format_outreach_message(
        victim_info: Dict[str, Any],
        report_url: str,
        report: Any,
    ) -> str:
        """Generate an outreach message for the victim."""
        ens = victim_info.get("ens_name") or ""
        addr = victim_info["address"]
        label = f"{ens} ({addr[:10]}...)" if ens else f"{addr[:10]}...{addr[-6:]}"

        lines = [
            f"📢 {t('en', 'outreach.to')} {label}",
            "",
            f"{t('en', 'outreach.detected')}",
            "",
            f"{t('en', 'outreach.report_summary')}",
            "",
            f"🔗 {t('en', 'outreach.tx')}: {report.victim_tx[:20]}...",
            f"💰 {t('en', 'outreach.estimated_loss')}: {report.victim_loss_native} {report.native_symbol}",
            f"🕵️ {t('en', 'outreach.attacker')}: {report.attacker[:20]}...",
            "",
            f"📋 {t('en', 'outreach.full_report')}: {report_url}",
            "",
            f"{t('en', 'outreach.recommendations')}",
            f"1. {t('en', 'outreach.rec_1')}",
            f"2. {t('en', 'outreach.rec_2')}",
            f"3. {t('en', 'outreach.rec_3')}",
        ]

        socials = victim_info.get("social_accounts", [])
        if socials:
            lines.append("")
            lines.append(f"📱 {t('en', 'outreach.social_verified')}")
            for s in socials:
                lines.append(f"  - {s['platform']}: @{s.get('handle', '')}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Farcaster auto-mention (zero-friction victim outreach)
    # ------------------------------------------------------------------
    def send_farcaster_cast(self, text: str,
                            mentions: Optional[list] = None,
                            embed_url: str = "") -> bool:
        """Send a cast via the Neynar v2 API.

        Requires both NEYNAR_API_KEY and NEYNAR_SIGNER_UUID to be configured.
        Returns True on success, False otherwise (missing config or API failure).
        """
        if not self.neynar_api_key or not self.neynar_signer_uuid:
            logger.debug("Farcaster cast skipped: missing NEYNAR_API_KEY "
                         "or NEYNAR_SIGNER_UUID")
            return False

        payload: Dict[str, Any] = {
            "signer_uuid": self.neynar_signer_uuid,
            "text": text,
        }
        if mentions:
            payload["mentions"] = mentions
        if embed_url:
            payload["embeds"] = [{"url": embed_url}]

        try:
            resp = requests.post(
                "https://api.neynar.com/v2/farcaster/cast",
                headers={
                    "api_key": self.neynar_api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10,
                proxies=self._proxies(),
            )
            if resp.status_code == 200:
                cast_hash = resp.json().get("cast", {}).get("hash", "")
                logger.info("Farcaster cast sent | hash=%s", cast_hash)
                return True
            logger.error("Farcaster cast failed | HTTP %d | %s",
                         resp.status_code, resp.text[:300])
            return False
        except Exception as e:
            logger.error("Farcaster cast exception: %s", e)
            return False

    def outreach_to_farcaster(self, victim_info: Dict[str, Any],
                              report: Any, report_url: str) -> bool:
        """Auto-mention the victim on Farcaster with a diagnostic summary.

        Looks up the victim's Farcaster account (identified during
        identify_victim), then posts a public cast mentioning them, so the
        victim sees the alert next time they open Warpcast — zero opt-in,
        zero subscription, fully passive outreach.
        """
        fc_account = next(
            (s for s in victim_info.get("social_accounts", [])
             if s.get("platform") == "farcaster"),
            None,
        )
        if not fc_account:
            logger.debug("Victim has no Farcaster account; skipping cast")
            return False

        fid = fc_account.get("fid", 0)
        username = fc_account.get("username", "")
        loss_str = f"{report.victim_loss_native:.4f} {report.native_symbol}"
        attacker_tail = (report.attacker[:6] + "..."
                         + report.attacker[-4:])
        tx_tail = report.victim_tx[:10] + "..." + report.victim_tx[-4:]

        # Farcaster cast text limit is 320 characters.
        text = (
            f"@{username} you were hit by a MEV sandwich attack.\n"
            f"Loss: {loss_str} | Attacker: {attacker_tail}\n"
            f"Victim tx: {tx_tail}\n"
            f"Diagnostic report below 👇"
        )
        if len(text) > 320:
            text = text[:317] + "..."

        return self.send_farcaster_cast(
            text=text,
            mentions=[fid] if fid else None,
            embed_url=report_url,
        )
