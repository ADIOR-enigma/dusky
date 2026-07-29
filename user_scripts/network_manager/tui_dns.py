#!/usr/bin/env python3
"""
===============================================================================
DUSKY TUI: SYSTEMD-RESOLVED DNS SCHEMA
===============================================================================
Target: /etc/systemd/resolved.conf.d/99-dns-tui.conf
Engine: systemd_dns (Atomic POSIX / resolvectl)
===============================================================================
"""

import sys
from pathlib import Path

# Bootstrap the Python path to locate the core TUI modules
_DUSKY_TUI_ROOT = Path.home() / "user_scripts" / "dusky_tui"
if str(_DUSKY_TUI_ROOT) not in sys.path:
    sys.path.insert(0, str(_DUSKY_TUI_ROOT))

from python.frontend.core_types import ConfigItem

# =============================================================================
# 1. CORE APPLICATION ROUTING
# =============================================================================
ENGINE_TYPE = "systemd_dns"
TARGET_FILE = "/etc/systemd/resolved.conf.d/99-dns-tui.conf"
REQUIRE_ROOT = True
APP_TITLE = "Arch Linux DNS & Resolver Configurator"

# =============================================================================
# 2. UI & ENVIRONMENT BEHAVIOR
# =============================================================================
DEFAULT_MODE = "auto"
THEME_FILE = "~/.config/matugen/generated/dusky_tui.json"
ENABLE_USER_PRESETS = True
USER_PRESETS_TAB = "⚡ Top-Tier Presets"

# =============================================================================
# 3. TABS DEFINITION
# =============================================================================
TABS = {
    0: "⚡ Top-Tier Presets",
    1: "🛡️ Privacy & Protocols",
    2: "🌐 Custom Upstreams",
    3: "⚙️ Local Network Subsystems",
}

# =============================================================================
# 4. HARDENED PRESET PAYLOADS
# =============================================================================
FALLBACK_QUAD9 = "9.9.9.9#dns.quad9.net 149.112.112.112#dns.quad9.net 2620:fe::fe#dns.quad9.net 2620:fe::9#dns.quad9.net"

PRESET_CLOUDFLARE = {
    "Resolve.DNS": "1.1.1.1#cloudflare-dns.com 1.0.0.1#cloudflare-dns.com 2606:4700:4700::1111#cloudflare-dns.com 2606:4700:4700::1001#cloudflare-dns.com",
    "Resolve.FallbackDNS": FALLBACK_QUAD9,
    "Resolve.DNSOverTLS": "opportunistic",
    "Resolve.DNSSEC": "no",
}

PRESET_QUAD9 = {
    "Resolve.DNS": "9.9.9.9#dns.quad9.net 149.112.112.112#dns.quad9.net 2620:fe::fe#dns.quad9.net 2620:fe::9#dns.quad9.net",
    "Resolve.FallbackDNS": "1.1.1.1#cloudflare-dns.com 1.0.0.1#cloudflare-dns.com",
    "Resolve.DNSOverTLS": "yes",
    "Resolve.DNSSEC": "allow-downgrade",
}

PRESET_MULLVAD = {
    "Resolve.DNS": "194.242.2.2#dns.mullvad.net 194.242.2.3#dns.mullvad.net 2a07:e180:2::1#dns.mullvad.net",
    "Resolve.FallbackDNS": FALLBACK_QUAD9,
    "Resolve.DNSOverTLS": "yes",
    "Resolve.DNSSEC": "no",
}

PRESET_ADGUARD = {
    "Resolve.DNS": "94.140.14.14#dns.adguard-dns.com 94.140.15.15#dns.adguard-dns.com 2a10:50c0::ad1:ff#dns.adguard-dns.com 2a10:50c0::ad2:ff#dns.adguard-dns.com",
    "Resolve.FallbackDNS": FALLBACK_QUAD9,
    "Resolve.DNSOverTLS": "opportunistic",
    "Resolve.DNSSEC": "no",
}

PRESET_GOOGLE = {
    "Resolve.DNS": "8.8.8.8#dns.google 8.8.4.4#dns.google 2001:4860:4860::8888#dns.google 2001:4860:4860::8844#dns.google",
    "Resolve.FallbackDNS": FALLBACK_QUAD9,
    "Resolve.DNSOverTLS": "opportunistic",
    "Resolve.DNSSEC": "no",
}

PRESET_CONTROLD = {
    "Resolve.DNS": "76.76.2.0#p0.freedns.controld.com 76.76.10.0#p0.freedns.controld.com 2606:1a40::#p0.freedns.controld.com",
    "Resolve.FallbackDNS": FALLBACK_QUAD9,
    "Resolve.DNSOverTLS": "opportunistic",
    "Resolve.DNSSEC": "no",
}

PRESET_CLOUDFLARE_FAMILY = {
    "Resolve.DNS": "1.1.1.3#family.cloudflare-dns.com 1.0.0.3#family.cloudflare-dns.com 2606:4700:4700::1113#family.cloudflare-dns.com 2606:4700:4700::1003#family.cloudflare-dns.com",
    "Resolve.FallbackDNS": FALLBACK_QUAD9,
    "Resolve.DNSOverTLS": "opportunistic",
    "Resolve.DNSSEC": "no",
}

PRESET_OPENDNS = {
    "Resolve.DNS": "208.67.222.222#dns.opendns.com 208.67.220.220#dns.opendns.com 2620:119:35::35#dns.opendns.com 2620:119:53::53#dns.opendns.com",
    "Resolve.FallbackDNS": FALLBACK_QUAD9,
    "Resolve.DNSOverTLS": "opportunistic",
    "Resolve.DNSSEC": "no",
}

PRESET_DHCP = {
    "Resolve.DNS": "",
    "Resolve.FallbackDNS": "",
    "Resolve.Domains": "",
    "Resolve.DNSOverTLS": "no",
    "Resolve.DNSSEC": "no",
}

# =============================================================================
# 5. SCHEMA DEFINITION
# =============================================================================
SCHEMA = {
    # -------------------------------------------------------------------------
    # TAB 0: PRESETS
    # -------------------------------------------------------------------------
    0: [
        ConfigItem(
            label="Cloudflare Standard (1.1.1.1 - Max Speed)",
            key="preset_cloudflare",
            type_="preset",
            default=False,
            scope="DEFAULT",
            preset_payload=PRESET_CLOUDFLARE,
            extended_help="Configures Cloudflare high-performance global DNS with TLS SNI authentication (#cloudflare-dns.com). Highly recommended for standard setups.",
            exists_in_target=True
        ),
        ConfigItem(
            label="Cloudflare Family (1.1.1.3 - Malware & Adult Blocking)",
            key="preset_cloudflare_family",
            type_="preset",
            default=False,
            scope="DEFAULT",
            preset_payload=PRESET_CLOUDFLARE_FAMILY,
            extended_help="Cloudflare Family DNS filtering out known malicious sites, phishing domains, and adult content at the DNS level.",
            exists_in_target=True
        ),
        ConfigItem(
            label="Quad9 (9.9.9.9 - Threat & Malware Blocking)",
            key="preset_quad9",
            type_="preset",
            default=False,
            scope="DEFAULT",
            preset_payload=PRESET_QUAD9,
            extended_help="Configures Quad9 with strict DNS-over-TLS encryption. Automatically drops queries for known malicious, phishing, and botnet domains.",
            exists_in_target=True
        ),
        ConfigItem(
            label="Mullvad Public (Zero-Log Privacy)",
            key="preset_mullvad",
            type_="preset",
            default=False,
            scope="DEFAULT",
            preset_payload=PRESET_MULLVAD,
            extended_help="Routes lookups through Mullvad's audited, zero-knowledge, zero-log public DNS servers. Forces Strict DoT.",
            exists_in_target=True
        ),
        ConfigItem(
            label="AdGuard (Ad & Tracker Null-Routing)",
            key="preset_adguard",
            type_="preset",
            default=False,
            scope="DEFAULT",
            preset_payload=PRESET_ADGUARD,
            extended_help="Blocks advertisements, tracking domains, and analytics networks at the resolver level before they hit the browser.",
            exists_in_target=True
        ),
        ConfigItem(
            label="OpenDNS / Cisco Umbrella (208.67.222.222)",
            key="preset_opendns",
            type_="preset",
            default=False,
            scope="DEFAULT",
            preset_payload=PRESET_OPENDNS,
            extended_help="Enterprise-grade reliable global DNS from Cisco Umbrella with DoT SNI support.",
            exists_in_target=True
        ),
        ConfigItem(
            label="Google Public DNS (8.8.8.8)",
            key="preset_google",
            type_="preset",
            default=False,
            scope="DEFAULT",
            preset_payload=PRESET_GOOGLE,
            extended_help="Standard Google Public DNS with IPv4 and IPv6 resolvers. Stable, but logs basic metadata.",
            exists_in_target=True
        ),
        ConfigItem(
            label="ControlD Free (Uncensored Route)",
            key="preset_controld",
            type_="preset",
            default=False,
            scope="DEFAULT",
            preset_payload=PRESET_CONTROLD,
            extended_help="ControlD's free unfiltered resolver network with DoT hostname verification.",
            exists_in_target=True
        ),
        ConfigItem(
            label="DHCP / Network Default (Clear Static DNS)",
            key="preset_dhcp",
            type_="preset",
            default=False,
            scope="DEFAULT",
            preset_payload=PRESET_DHCP,
            extended_help="Wipes the static override drop-in file completely, returning full DNS control to local DHCP, NetworkManager, or VPN (like Tailscale) assignment.",
            exists_in_target=True
        ),
    ],

    # -------------------------------------------------------------------------
    # TAB 1: SECURITY & PROTOCOLS
    # -------------------------------------------------------------------------
    1: [
        ConfigItem(
            label="DNS-over-TLS (DoT Encryption)",
            key="DNSOverTLS",
            type_="cycle",
            default="opportunistic",
            scope="Resolve",
            options=["opportunistic", "yes", "no"],
            extended_help=(
                "Controls encryption of DNS queries over TLS (Port 853).\n"
                "  • yes: Strict mode. Will refuse to resolve if TLS fails.\n"
                "  • opportunistic: Upgrades to TLS if the server supports it, falls back to UDP/53.\n"
                "  • no: Plaintext UDP/53 only."
            ),
        ),
        ConfigItem(
            label="DNSSEC Validation",
            key="DNSSEC",
            type_="cycle",
            default="no",
            scope="Resolve",
            options=["no", "allow-downgrade", "yes"],
            warning_msg="Setting DNSSEC to 'yes' on broken captive portals (like hotel Wi-Fi) will completely break the internet.",
            extended_help=(
                "Enables cryptographic signature verification of DNS records.\n"
                "  • allow-downgrade: Validates if supported, permits fallback if the domain isn't signed.\n"
                "  • yes: Strict enforcement. Highly vulnerable to broken upstream network behavior.\n"
                "  • no: Disabled (Fastest, relies on upstream provider for security)."
            ),
        ),
        ConfigItem(
            label="Local DNS Cache Mode",
            key="Cache",
            type_="cycle",
            default="yes",
            scope="Resolve",
            options=["yes", "no-negative", "no"],
            extended_help=(
                "Controls local DNS response caching in systemd-resolved.\n"
                "  • yes: Full caching of positive and negative DNS responses.\n"
                "  • no-negative: Caches positive lookups only (ignores NXDOMAIN errors).\n"
                "  • no: Disables local cache entirely."
            ),
        ),
        ConfigItem(
            label="Fallback DNS Servers",
            key="FallbackDNS",
            type_="string",
            default=FALLBACK_QUAD9,
            scope="Resolve",
            extended_help="A space-separated list of fallback resolvers used ONLY if primary interface/static DNS servers fail entirely.",
        ),
    ],

    # -------------------------------------------------------------------------
    # TAB 2: CUSTOM SERVERS
    # -------------------------------------------------------------------------
    2: [
        ConfigItem(
            label="Static Primary & Secondary DNS Map",
            key="DNS",
            type_="string",
            default=PRESET_CLOUDFLARE["Resolve.DNS"],
            scope="Resolve",
            extended_help=(
                "Space-separated explicit DNS servers.\n"
                "Format: IP#HOSTNAME for authenticated DoT (e.g. 1.1.1.1#cloudflare-dns.com 9.9.9.9#dns.quad9.net)."
            ),
        ),
        ConfigItem(
            label="Routing & Search Domains",
            key="Domains",
            type_="string",
            default="",
            scope="Resolve",
            extended_help=(
                "Space-separated list of domains used for search suffixes and routing.\n"
                "Prefix with '~' to create a routing-only domain (e.g. '~.' routes ALL queries through these global DNS servers)."
            ),
        ),
    ],

    # -------------------------------------------------------------------------
    # TAB 3: SYSTEM & LOCAL STUB
    # -------------------------------------------------------------------------
    3: [
        ConfigItem(
            label="DNS Stub Listener (127.0.0.53:53)",
            key="DNSStubListener",
            type_="cycle",
            default="yes",
            scope="Resolve",
            options=["yes", "no", "udp", "tcp"],
            extended_help=(
                "Controls systemd-resolved's local stub listener.\n"
                "  • yes: Full local stub enabled (/etc/resolv.conf targets stub-resolv.conf).\n"
                "  • no: Stub disabled entirely (/etc/resolv.conf targets resolv.conf). Required if running a conflicting local DNS server (e.g., dnsmasq, Bind, or Pi-hole)."
            ),
            warning_msg="Disabling the stub listener without an active local DNS proxy in place will break standard system name resolution.",
        ),
        ConfigItem(
            label="MulticastDNS (mDNS / UDP 5353)",
            key="MulticastDNS",
            type_="cycle",
            default="no",
            scope="Resolve",
            options=["no", "resolve", "yes"],
            extended_help=(
                "Controls systemd-resolved mDNS handling.\n"
                "Set this to 'no' if you are running Avahi Daemon to prevent UDP port 5353 contention. Leave 'no' for standard security."
            ),
        ),
        ConfigItem(
            label="LLMNR (Link-Local Multicast)",
            key="LLMNR",
            type_="cycle",
            default="no",
            scope="Resolve",
            options=["no", "resolve", "yes"],
            extended_help="Legacy local multicast resolution. Deprecated and vulnerable to network spoofing. Keep this disabled ('no').",
        ),
        ConfigItem(
            label="Flush Local Systemd DNS Cache",
            key="flush_dns_cache",
            type_="action",  
            default="resolvectl flush-caches",
            scope="DEFAULT",
            exists_in_target=True,
            confirm_message="Flush all cached DNS records across all network interfaces?",
            extended_help="Executes `resolvectl flush-caches` in the shell to immediately purge all cached queries and force fresh upstream lookups.",
        ),
    ],
}

# =============================================================================
# 6. DIRECT EXECUTION HANDLER
# =============================================================================
if __name__ == "__main__":
    import sys, subprocess
    from pathlib import Path

    script_path = Path(__file__).resolve()
    main_router = Path.home() / "user_scripts" / "dusky_tui" / "python" / "main" / "main.py"

    if main_router.exists():
        sys.exit(subprocess.run([sys.executable, str(main_router), str(script_path)] + sys.argv[1:]).returncode)
    else:
        print(f"[-] Error: Main Dusky TUI router not found at {main_router}", file=sys.stderr)
        sys.exit(1)
