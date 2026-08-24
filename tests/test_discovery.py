"""
tests/test_discovery.py — Discovery parser unit tests.

These test the XML/packet parsers with synthetic payloads — no network required.
"""

import os
os.environ.setdefault("SENTINEL_SECRET_KEY", "test-secret-key-for-unit-tests-only")

from sentinel.services.discovery import parse_onvif_probe_match, parse_sadp_response


# ---------------------------------------------------------------------------
# ONVIF ProbeMatch parser
# ---------------------------------------------------------------------------

_ONVIF_PROBE_MATCH = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope
    xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
    xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery"
    xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <soap:Header>
    <wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</wsa:Action>
  </soap:Header>
  <soap:Body>
    <wsd:ProbeMatches>
      <wsd:ProbeMatch>
        <wsa:EndpointReference>
          <wsa:Address>uuid:a1b2c3d4-0000-0000-0000-aabbccddeeff</wsa:Address>
        </wsa:EndpointReference>
        <wsd:Types>dn:NetworkVideoTransmitter</wsd:Types>
        <wsd:Scopes>
          onvif://www.onvif.org/hardware/DS-2CD2143G2-I
          onvif://www.onvif.org/name/FrontDoor
          onvif://www.onvif.org/location/country/US
        </wsd:Scopes>
        <wsd:XAddrs>http://192.168.1.64/onvif/device_service</wsd:XAddrs>
        <wsd:MetadataVersion>1</wsd:MetadataVersion>
      </wsd:ProbeMatch>
    </wsd:ProbeMatches>
  </soap:Body>
</soap:Envelope>"""

_ONVIF_NO_MATCH = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope">
  <soap:Body>
    <NotAProbeMatch/>
  </soap:Body>
</soap:Envelope>"""

_ONVIF_MALFORMED = b"this is not xml at all <<<>>>"


def test_onvif_parser_valid():
    dev = parse_onvif_probe_match(_ONVIF_PROBE_MATCH)
    assert dev is not None
    assert dev.ip == "192.168.1.64"
    assert dev.onvif_url == "http://192.168.1.64/onvif/device_service"
    assert dev.model == "DS-2CD2143G2-I"
    assert dev.name == "FrontDoor"
    assert dev.source == "onvif"


def test_onvif_parser_no_probe_match():
    dev = parse_onvif_probe_match(_ONVIF_NO_MATCH)
    assert dev is None


def test_onvif_parser_malformed_xml():
    dev = parse_onvif_probe_match(_ONVIF_MALFORMED)
    assert dev is None


def test_onvif_parser_empty():
    dev = parse_onvif_probe_match(b"")
    assert dev is None


# ---------------------------------------------------------------------------
# SADP response parser
# ---------------------------------------------------------------------------

_SADP_XML_RESPONSE = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<DeviceInfo>
  <DeviceName>FrontDoor</DeviceName>
  <DeviceModel>DS-2CD2143G2-I</DeviceModel>
  <IPv4Address>192.168.1.64</IPv4Address>
  <MACAddress>AA:BB:CC:DD:EE:FF</MACAddress>
</DeviceInfo>"""

_SADP_ALTERNATE_TAGS = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Name>Driveway</Name>
  <Model>DS-2CD2T47G2-L</Model>
  <IPAddress>192.168.1.65</IPAddress>
  <MAC>11:22:33:44:55:66</MAC>
</Response>"""

_SADP_NO_IP = b"""\
<?xml version="1.0"?>
<Response>
  <MAC>AA:BB:CC:DD:EE:FF</MAC>
</Response>"""


def test_sadp_parser_standard_tags():
    dev = parse_sadp_response(_SADP_XML_RESPONSE)
    assert dev is not None
    assert dev.ip == "192.168.1.64"
    assert dev.mac == "AA:BB:CC:DD:EE:FF"
    assert dev.model == "DS-2CD2143G2-I"
    assert dev.name == "FrontDoor"
    assert dev.source == "sadp"


def test_sadp_parser_alternate_tags():
    dev = parse_sadp_response(_SADP_ALTERNATE_TAGS)
    assert dev is not None
    assert dev.ip == "192.168.1.65"


def test_sadp_parser_no_ip_returns_none():
    dev = parse_sadp_response(_SADP_NO_IP)
    assert dev is None


def test_sadp_parser_binary_garbage():
    # Non-XML binary data should return None gracefully.
    dev = parse_sadp_response(b"\x00\x01\x02\x03\x04\x05")
    assert dev is None


def test_sadp_parser_empty():
    dev = parse_sadp_response(b"")
    assert dev is None
