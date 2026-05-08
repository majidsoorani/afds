"""
IP intelligence — VPN / datacenter / Tor / (optional) GeoLite2 ASN.

Free, offline-friendly replacement for third-party vendor's `ip_details` block. Uses:
  - X4BNet/lists_vpn  ipv4.txt        (~10k VPN CIDRs)
  - X4BNet/lists_vpn  datacenter.txt  (~41k datacenter CIDRs)
  - Tor Project       torbulkexitlist (~1.3k exit nodes)
  - MaxMind GeoLite2-ASN.mmdb (optional — user supplies with license key)

All CIDR files are read once at module import into a py-radix tree for
O(log n) prefix lookup. GeoLite2 is opened lazily if the env var
``AFDS_GEOIP2_DB`` points to a readable file.

Usage:
    from app.services.ip_intel import analyze_ip_extended
    extra = analyze_ip_extended("45.153.160.23")
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"

_vpn_tree = None
_datacenter_tree = None
_tor_exits: frozenset[str] = frozenset()
_geoip_reader = None


def _load_cidr_tree(path: Path, label: str):
    try:
        import radix  # type: ignore
    except ImportError:
        logger.warning("py-radix not installed; %s lookups disabled", label)
        return None
    if not path.exists():
        logger.info("%s CIDR list not vendored: %s", label, path)
        return None
    tree = radix.Radix()
    count = 0
    for line in path.read_text().splitlines():
        cidr = line.strip()
        if not cidr or cidr.startswith("#"):
            continue
        try:
            tree.add(cidr)
            count += 1
        except Exception:
            continue
    logger.info("Loaded %d %s CIDRs", count, label)
    return tree


def _load_tor_exits(path: Path) -> frozenset[str]:
    if not path.exists():
        logger.info("Tor exit list not vendored: %s", path)
        return frozenset()
    exits = {
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    logger.info("Loaded %d Tor exit nodes", len(exits))
    return frozenset(exits)


def _init() -> None:
    global _vpn_tree, _datacenter_tree, _tor_exits, _geoip_reader
    _vpn_tree = _load_cidr_tree(_DATA_DIR / "vpn_cidrs.txt", "VPN")
    _datacenter_tree = _load_cidr_tree(_DATA_DIR / "datacenter_cidrs.txt", "datacenter")
    _tor_exits = _load_tor_exits(_DATA_DIR / "tor_exit_nodes.txt")

    geoip_path = os.getenv("AFDS_GEOIP2_DB")
    if geoip_path and os.path.exists(geoip_path):
        try:
            import geoip2.database  # type: ignore
            _geoip_reader = geoip2.database.Reader(geoip_path)
            logger.info("Opened GeoLite2 DB at %s", geoip_path)
        except ImportError:
            logger.warning("geoip2 not installed; set AFDS_GEOIP2_DB ignored")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to open GeoLite2 DB: %s", exc)


_init()


def analyze_ip_extended(ip: str) -> dict[str, Any]:
    """Return additional IP intel signals. Never raises."""
    out: dict[str, Any] = {
        "is_vpn": False,
        "is_datacenter_cidr": False,
        "is_tor_exit": False,
        "asn": None,
        "asn_org": None,
        "country": None,
    }
    if not ip:
        return out
    ip = ip.strip()

    try:
        if _vpn_tree is not None and _vpn_tree.search_best(ip) is not None:
            out["is_vpn"] = True
    except Exception:
        pass
    try:
        if _datacenter_tree is not None and _datacenter_tree.search_best(ip) is not None:
            out["is_datacenter_cidr"] = True
    except Exception:
        pass
    if ip in _tor_exits:
        out["is_tor_exit"] = True

    if _geoip_reader is not None:
        # Try ASN lookup (GeoLite2-ASN DB)
        try:
            resp = _geoip_reader.asn(ip)
            out["asn"] = resp.autonomous_system_number
            out["asn_org"] = resp.autonomous_system_organization
        except Exception:
            pass
        # Try Country lookup (GeoLite2-Country / GeoLite2-City DB) — Gap 8.
        # Either DB type may be configured; both calls are cheap and safe.
        try:
            resp = _geoip_reader.country(ip)
            if getattr(resp, "country", None) is not None:
                out["country"] = resp.country.iso_code
        except Exception:
            pass

    return out
