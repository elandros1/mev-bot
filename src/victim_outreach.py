"""
受害者触达模块
--------------
当检测到夹子攻击时：
1. 提取受害者钱包地址
2. 反查 ENS 名称
3. 查找 Web3 社交账号（Farcaster / Lens / ENS text records）
4. 通过可用渠道（ntfy / Telegram）将诊断报告推送给受害者
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

import requests
from web3 import Web3

logger = logging.getLogger(__name__)


class VictimOutreach:
    """受害者自动识别与触达"""

    # ENS Registry 合约地址（以太坊主网）
    ENS_REGISTRY = "0x00000000000C2E074eC69A0dFb2997BA6C7d2e1e"

    # ENS Registry ABI（仅 resolver 函数）
    ENS_REGISTRY_ABI = [
        {"constant": True, "inputs": [{"name": "node", "type": "bytes32"}],
         "name": "resolver", "outputs": [{"name": "", "type": "address"}],
         "type": "function"},
    ]

    # ENS Resolver ABI（name + text 函数）
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
                 lens_api_url: str = "https://api.lens.dev/graphql"):
        self.w3 = w3
        self.neynar_api_key = neynar_api_key or os.getenv("NEYNAR_API_KEY", "")
        self.lens_api_url = lens_api_url
        self._ens_registry = None

        if w3 is not None:
            try:
                self._ens_registry = w3.eth.contract(
                    address=Web3.to_checksum_address(self.ENS_REGISTRY),
                    abi=self.ENS_REGISTRY_ABI,
                )
                logger.info("ENS 反解模块已加载（原生合约调用）")
            except Exception as e:
                logger.warning("ENS 合约初始化失败: %s", e)

    @staticmethod
    def _proxies() -> Optional[dict]:
        proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                 or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
        return {"http": proxy, "https": proxy} if proxy else None

    @staticmethod
    def _namehash(name: str) -> bytes:
        """计算 ENS namehash"""
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
        """计算地址的反向解析 node"""
        addr_lower = address.lower().replace("0x", "")
        reverse_name = f"{addr_lower}.addr.reverse"
        return VictimOutreach._namehash(reverse_name)

    # ------------------------------------------------------------------
    # ENS 反解
    # ------------------------------------------------------------------
    def resolve_ens(self, address: str) -> Optional[str]:
        """通过 ENS Registry 合约反查 ENS 名称（地址 → name）"""
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
                logger.info("ENS 反解: %s → %s", address[:10] + "...", name)
                return name
        except Exception as e:
            logger.debug("ENS 反解失败 %s: %s", address[:10] + "...", e)
        return None

    # ------------------------------------------------------------------
    # ENS text records（社交账号）
    # ------------------------------------------------------------------
    def get_ens_text_records(self, ens_name: str) -> Dict[str, str]:
        """读取 ENS 的 text records（Twitter, GitHub, Farcaster 等）"""
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
            logger.debug("ENS text records 读取失败: %s", e)
        return records

    # ------------------------------------------------------------------
    # Farcaster 查找（需要 Neynar API key）
    # ------------------------------------------------------------------
    def lookup_farcaster(self, address: str) -> Optional[Dict[str, Any]]:
        """通过 Neynar API 查找 Farcaster 资料（需 API key）"""
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
            logger.debug("Farcaster 查找失败: %s", e)
        return None

    # ------------------------------------------------------------------
    # Lens Protocol 查找
    # ------------------------------------------------------------------
    def lookup_lens(self, address: str) -> Optional[Dict[str, Any]]:
        """通过 Lens GraphQL 查找 Lens 资料"""
        try:
            query = """
                query {
                    defaultProfile(request: { address: "%s" }) {
                        id
                        handle { localName } 
                        metadata { displayName }
                    }
                }
            """ % address
            resp = requests.post(
                self.lens_api_url,
                json={"query": query},
                timeout=8, proxies=self._proxies(),
            )
            if resp.status_code == 200:
                # Lens v2 returns JSON if the endpoint is correct
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
            logger.debug("Lens 查找失败: %s", e)
        return None

    # ------------------------------------------------------------------
    # 综合查找
    # ------------------------------------------------------------------
    def identify_victim(self, address: str) -> Dict[str, Any]:
        """综合查找受害者的链上身份信息"""
        info: Dict[str, Any] = {
            "address": address,
            "ens_name": None,
            "social_accounts": [],
            "ens_text_records": {},
        }

        # 1. ENS 反解
        ens_name = self.resolve_ens(address)
        if ens_name:
            info["ens_name"] = ens_name
            # 2. 读取 ENS text records
            records = self.get_ens_text_records(ens_name)
            if records:
                info["ens_text_records"] = records
                # 映射到社交账号
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

        # 3. Farcaster（需要 API key）
        fc = self.lookup_farcaster(address)
        if fc:
            info["social_accounts"].append(fc)

        # 4. Lens
        lens = self.lookup_lens(address)
        if lens:
            info["social_accounts"].append(lens)

        return info

    # ------------------------------------------------------------------
    # 生成触达消息
    # ------------------------------------------------------------------
    @staticmethod
    def format_outreach_message(
        victim_info: Dict[str, Any],
        report_url: str,
        report: Any,
    ) -> str:
        """生成发送给受害者的触达消息"""
        ens = victim_info.get("ens_name") or ""
        addr = victim_info["address"]
        label = f"{ens} ({addr[:10]}...)" if ens else f"{addr[:10]}...{addr[-6:]}"

        lines = [
            f"📢 致 {label}",
            "",
            "我们检测到您的一笔链上交易疑似遭遇 MEV 夹子攻击（Sandwich Attack）。",
            "以下是诊断报告摘要：",
            "",
            f"🔗 交易: {report.victim_tx[:20]}...",
            f"💰 估计损失: {report.victim_loss_native} {report.native_symbol}",
            f"🕵️ 攻击者: {report.attacker[:20]}...",
            "",
            f"📋 完整诊断报告: {report_url}",
            "",
            "建议您检查该交易，并采取以下防护措施：",
            "1. 使用 Flashbots Protect / MEV-Share 提交隐私交易",
            "2. 降低滑点容忍度至 < 0.5%",
            "3. 大额交易拆分多笔执行",
        ]

        socials = victim_info.get("social_accounts", [])
        if socials:
            lines.append("")
            lines.append("📱 您的社交账号已通过链上身份验证：")
            for s in socials:
                lines.append(f"  - {s['platform']}: @{s.get('handle', '')}")

        return "\n".join(lines)
