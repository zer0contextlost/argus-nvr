"""
sentinel/services/discovery.py — LAN camera auto-discovery.

Three parallel methods:
  1. ONVIF WS-Discovery (UDP multicast 239.255.255.250:3702)
  2. Hikvision SADP    (UDP broadcast :37020)
  3. Subnet port probe  (TCP 554 / 80 / 8000)

All use short timeouts. Results are merged by IP into DiscoveredDevice dicts.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import struct
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

from sentinel.util.logging import get_logger

log = get_logger("sentinel.discovery")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class DiscoveredDevice:
    ip: str
    mac: str = ""
    model: str = ""
    name: str = ""
    onvif_url: str = ""
    source: str = ""     # "onvif" | "sadp" | "probe"
    open_ports: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ONVIF WS-Discovery
# ---------------------------------------------------------------------------

_ONVIF_MULTICAST_IP = "239.255.255.250"
_ONVIF_MULTICAST_PORT = 3702

_WS_DISCOVERY_PROBE = """\
<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope
    xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
    xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <soap:Header>
    <wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</wsa:Action>
    <wsa:MessageID>uuid:{message_id}</wsa:MessageID>
    <wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>
  </soap:Header>
  <soap:Body>
    <wsd:Probe>
      <wsd:Types>dn:NetworkVideoTransmitter</wsd:Types>
    </wsd:Probe>
  </soap:Body>
</soap:Envelope>"""

_NS = {
    "soap": "http://www.w3.org/2003/05/soap-envelope",
    "wsa":  "http://schemas.xmlsoap.org/ws/2004/08/addressing",
    "wsd":  "http://schemas.xmlsoap.org/ws/2005/04/discovery",
    "dn":   "http://www.onvif.org/ver10/network/wsdl",
    "tds":  "http://www.onvif.org/ver10/device/wsdl",
}


def parse_onvif_probe_match(xml_bytes: bytes) -> Optional[DiscoveredDevice]:
    """
    Parse an ONVIF WS-Discovery ProbeMatch response.

    Returns a DiscoveredDevice or None if the payload is not a valid match.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    # Find ProbeMatch element (namespace-agnostic search).
    match_el = root.find(".//{http://schemas.xmlsoap.org/ws/2005/04/discovery}ProbeMatch")
    if match_el is None:
        return None

    # XAddrs — list of service URLs (e.g. http://192.168.1.64/onvif/device_service).
    xaddrs_el = match_el.find("{http://schemas.xmlsoap.org/ws/2005/04/discovery}XAddrs")
    onvif_url = ""
    ip = ""
    if xaddrs_el is not None and xaddrs_el.text:
        onvif_url = xaddrs_el.text.strip().split()[0]
        # Extract IP from URL.
        m = re.search(r"https?://([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", onvif_url)
        if m:
            ip = m.group(1)

    if not ip:
        return None

    # Scopes — may contain model/name.
    scopes_el = match_el.find("{http://schemas.xmlsoap.org/ws/2005/04/discovery}Scopes")
    model = ""
    name = ""
    if scopes_el is not None and scopes_el.text:
        for scope in scopes_el.text.split():
            if "/hardware/" in scope:
                model = scope.split("/hardware/")[-1]
            if "/name/" in scope:
                name = scope.split("/name/")[-1]

    return DiscoveredDevice(
        ip=ip,
        onvif_url=onvif_url,
        model=model,
        name=name,
        source="onvif",
    )


async def discover_onvif(timeout: float = 3.0) -> list[DiscoveredDevice]:
    """Send a WS-Discovery Probe and collect ProbeMatch responses."""
    probe = _WS_DISCOVERY_PROBE.format(message_id=str(uuid.uuid4())).encode()
    devices: dict[str, DiscoveredDevice] = {}

    loop = asyncio.get_event_loop()

    def _send_recv() -> list[bytes]:
        responses: list[bytes] = []
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            sock.settimeout(timeout)
            sock.sendto(probe, (_ONVIF_MULTICAST_IP, _ONVIF_MULTICAST_PORT))
            end = asyncio.get_event_loop().time() + timeout
            while True:
                try:
                    data, _ = sock.recvfrom(4096)
                    responses.append(data)
                except socket.timeout:
                    break
        except OSError as exc:
            log.debug("ONVIF discovery socket error: %s", exc)
        finally:
            try:
                sock.close()
            except Exception:
                pass
        return responses

    try:
        responses = await loop.run_in_executor(None, _send_recv)
    except Exception as exc:
        log.debug("ONVIF discovery error: %s", exc)
        return []

    for data in responses:
        dev = parse_onvif_probe_match(data)
        if dev and dev.ip not in devices:
            devices[dev.ip] = dev

    log.info("ONVIF discovery found %d device(s)", len(devices))
    return list(devices.values())


# ---------------------------------------------------------------------------
# Hikvision SADP
# ---------------------------------------------------------------------------

_SADP_PORT = 37020
_SADP_BROADCAST = "255.255.255.255"
_SADP_PROBE = b"\x20\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"  # minimal probe


def parse_sadp_response(data: bytes) -> Optional[DiscoveredDevice]:
    """
    Parse a Hikvision SADP response packet.

    SADP responses are XML embedded in UDP datagrams (newer firmware) or
    binary (older). We handle the XML variant; binary is best-effort.
    """
    # Try XML first (newer Hikvision firmware sends XML in SADP).
    try:
        text = data.decode("utf-8", errors="ignore")
        if "<" in text and ">" in text:
            # Isolate the XML fragment.
            start = text.index("<")
            xml_str = text[start:]
            root = ET.fromstring(xml_str)
            ip = _xml_text(root, ".//IPv4Address") or _xml_text(root, ".//IPAddress") or ""
            mac = _xml_text(root, ".//MACAddress") or _xml_text(root, ".//MAC") or ""
            model = _xml_text(root, ".//DeviceModel") or _xml_text(root, ".//Model") or ""
            name = _xml_text(root, ".//DeviceName") or _xml_text(root, ".//Name") or ""
            if ip:
                return DiscoveredDevice(ip=ip, mac=mac, model=model, name=name, source="sadp")
    except Exception:
        pass
    return None


def _xml_text(root: ET.Element, path: str) -> str:
    el = root.find(path)
    if el is not None and el.text:
        return el.text.strip()
    return ""


async def discover_sadp(timeout: float = 3.0) -> list[DiscoveredDevice]:
    """Broadcast a SADP probe and collect Hikvision camera responses."""
    devices: dict[str, DiscoveredDevice] = {}
    loop = asyncio.get_event_loop()

    def _send_recv() -> list[bytes]:
        responses: list[bytes] = []
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(timeout)
            sock.bind(("", 0))
            sock.sendto(_SADP_PROBE, (_SADP_BROADCAST, _SADP_PORT))
            while True:
                try:
                    data, _ = sock.recvfrom(8192)
                    responses.append(data)
                except socket.timeout:
                    break
        except OSError as exc:
            log.debug("SADP discovery socket error: %s", exc)
        finally:
            try:
                sock.close()
            except Exception:
                pass
        return responses

    try:
        responses = await loop.run_in_executor(None, _send_recv)
    except Exception as exc:
        log.debug("SADP discovery error: %s", exc)
        return []

    for data in responses:
        dev = parse_sadp_response(data)
        if dev and dev.ip not in devices:
            devices[dev.ip] = dev

    log.info("SADP discovery found %d device(s)", len(devices))
    return list(devices.values())


# ---------------------------------------------------------------------------
# Subnet port probe
# ---------------------------------------------------------------------------

_PROBE_PORTS = [554, 80, 8000]
_PROBE_TIMEOUT = 0.5   # per host/port


async def _probe_host(ip: str, ports: list[int], timeout: float) -> Optional[DiscoveredDevice]:
    """Try connecting to a host on the given ports; return device if any open."""
    open_ports: list[int] = []
    for port in ports:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port),
                timeout=timeout,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            open_ports.append(port)
        except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
            pass
    if open_ports:
        return DiscoveredDevice(ip=ip, open_ports=open_ports, source="probe")
    return None


def _get_local_subnet() -> list[str]:
    """
    Return all host IPs in the local /24 subnet(s).
    Skips loopback and link-local addresses.
    """
    candidates: list[str] = []
    try:
        hostname = socket.gethostname()
        all_ips = socket.getaddrinfo(hostname, None, socket.AF_INET)
        for info in all_ips:
            ip_str = info[4][0]
            if ip_str.startswith("127.") or ip_str.startswith("169.254."):
                continue
            # Build a /24 network and enumerate all hosts.
            network = ipaddress.IPv4Network(f"{ip_str}/24", strict=False)
            for host in network.hosts():
                h = str(host)
                if h != ip_str:  # skip self
                    candidates.append(h)
    except Exception as exc:
        log.debug("subnet probe could not determine local IPs: %s", exc)
    return candidates


async def discover_subnet(
    timeout: float = _PROBE_TIMEOUT,
    max_concurrent: int = 50,
) -> list[DiscoveredDevice]:
    """
    Probe all hosts in the local /24 subnet on ports 554/80/8000.
    Uses a semaphore to cap concurrency.
    """
    hosts = _get_local_subnet()
    if not hosts:
        log.warning("subnet probe: no local subnet detected")
        return []

    log.info("subnet probe: checking %d hosts on ports %s", len(hosts), _PROBE_PORTS)
    sem = asyncio.Semaphore(max_concurrent)

    async def _guarded(ip: str) -> Optional[DiscoveredDevice]:
        async with sem:
            return await _probe_host(ip, _PROBE_PORTS, timeout)

    results = await asyncio.gather(*[_guarded(ip) for ip in hosts])
    devices = [r for r in results if r is not None]
    log.info("subnet probe found %d device(s) with open ports", len(devices))
    return devices


# ---------------------------------------------------------------------------
# Master discovery — run all three in parallel and merge
# ---------------------------------------------------------------------------

async def discover_all(
    onvif_timeout: float = 3.0,
    sadp_timeout: float = 3.0,
    probe_timeout: float = 0.5,
) -> list[DiscoveredDevice]:
    """
    Run ONVIF, SADP, and subnet probe in parallel; merge by IP.
    """
    onvif_task = asyncio.create_task(discover_onvif(timeout=onvif_timeout))
    sadp_task  = asyncio.create_task(discover_sadp(timeout=sadp_timeout))
    probe_task = asyncio.create_task(discover_subnet(timeout=probe_timeout))

    onvif_devs, sadp_devs, probe_devs = await asyncio.gather(
        onvif_task, sadp_task, probe_task, return_exceptions=True
    )

    merged: dict[str, DiscoveredDevice] = {}

    def _add(devs, source: str = "") -> None:
        if isinstance(devs, BaseException):
            log.warning("discovery [%s] failed: %s", source, devs)
            return
        if isinstance(devs, list):
            for d in devs:
                if d.ip not in merged:
                    merged[d.ip] = d
                else:
                    # Enrich existing entry.
                    existing = merged[d.ip]
                    if d.mac and not existing.mac:
                        existing.mac = d.mac
                    if d.model and not existing.model:
                        existing.model = d.model
                    if d.name and not existing.name:
                        existing.name = d.name
                    if d.onvif_url and not existing.onvif_url:
                        existing.onvif_url = d.onvif_url
                    if d.open_ports:
                        existing.open_ports = list(set(existing.open_ports + d.open_ports))
                    existing.source = f"{existing.source}+{d.source}"

    _add(onvif_devs, "onvif")
    _add(sadp_devs, "sadp")
    _add(probe_devs, "subnet")

    log.info("discovery complete: %d unique device(s) found", len(merged))
    return list(merged.values())
