# -*- coding: utf-8 -*-
"""
最小限のUPnP IGD (Internet Gateway Device) クライアント。
外部ライブラリに依存せず、標準ライブラリのみでルーターを探索し、
WANポートマッピングの追加/削除を行う。

対応していないルーター(UPnP無効など)の場合は例外を発生させるので、
呼び出し側でtry/exceptして無視すること。
"""

import socket
import re
import urllib.request
import xml.etree.ElementTree as ET


SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_MX = 2
SSDP_ST_LIST = [
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
]


def _ssdp_discover(timeout=3):
    """SSDPでIGDのLocation URLとSTを返すジェネレータ"""
    results = []
    for st in SSDP_ST_LIST:
        msg = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
            'MAN: "ssdp:discover"\r\n'
            f"MX: {SSDP_MX}\r\n"
            f"ST: {st}\r\n\r\n"
        ).encode("utf-8")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        try:
            sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
            while True:
                try:
                    data, _ = sock.recvfrom(65507)
                except socket.timeout:
                    break
                text = data.decode("utf-8", errors="ignore")
                m = re.search(r"LOCATION:\s*(.+)\r\n", text, re.IGNORECASE)
                if m:
                    results.append((m.group(1).strip(), st))
        finally:
            sock.close()

        if results:
            break
    return results


def _get_control_url(location):
    """デバイス記述XMLを取得し、WANIPConnection系のcontrolURLとサービスタイプを返す"""
    with urllib.request.urlopen(location, timeout=5) as resp:
        data = resp.read()

    root = ET.fromstring(data)

    # baseURLの決定
    base_url = None
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    url_base_el = root.find(".//d:URLBase", ns)
    if url_base_el is not None and url_base_el.text:
        base_url = url_base_el.text.strip()
    else:
        m = re.match(r"(https?://[^/]+)", location)
        base_url = m.group(1) if m else location

    # WANIPConnection / WANPPPConnection サービスを探索
    for service in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
        service_type = service.findtext("{urn:schemas-upnp-org:device-1-0}serviceType", "")
        if any(st in service_type for st in SSDP_ST_LIST) or \
           "WANIPConnection" in service_type or "WANPPPConnection" in service_type:
            control_url = service.findtext("{urn:schemas-upnp-org:device-1-0}controlURL", "")
            if control_url:
                if control_url.startswith("http"):
                    full = control_url
                else:
                    full = base_url.rstrip("/") + "/" + control_url.lstrip("/")
                return full, service_type

    raise RuntimeError("WANIPConnection/WANPPPConnectionサービスが見つかりませんでした")


def _soap_request(control_url, service_type, action, body_fields):
    """SOAPリクエストを送信"""
    fields_xml = "".join(
        f"<{k}>{v}</{k}>" for k, v in body_fields.items()
    )
    soap_body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="{service_type}">'
        f"{fields_xml}"
        f"</u:{action}>"
        "</s:Body></s:Envelope>"
    ).encode("utf-8")

    req = urllib.request.Request(
        control_url,
        data=soap_body,
        method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{service_type}#{action}"',
            "Connection": "Close",
        },
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read()


def get_local_ip():
    """デフォルトルートに使われているローカルIPアドレスを取得"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    finally:
        sock.close()
    return ip


def add_port_mapping(port, protocol="TCP", description="PortMgr", local_ip=None, timeout=3):
    """ルーターにポートマッピングを追加する。失敗時は例外を投げる。"""
    if local_ip is None:
        local_ip = get_local_ip()

    devices = _ssdp_discover(timeout=timeout)
    if not devices:
        raise RuntimeError("UPnP対応ルーターが見つかりませんでした")

    last_err = None
    for location, _ in devices:
        try:
            control_url, service_type = _get_control_url(location)
            _soap_request(control_url, service_type, "AddPortMapping", {
                "NewRemoteHost": "",
                "NewExternalPort": str(port),
                "NewProtocol": protocol.upper(),
                "NewInternalPort": str(port),
                "NewInternalClient": local_ip,
                "NewEnabled": "1",
                "NewPortMappingDescription": description,
                "NewLeaseDuration": "0",
            })
            return True
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"ポートマッピングの追加に失敗しました: {last_err}")


def delete_port_mapping(port, protocol="TCP", timeout=3):
    """ルーターのポートマッピングを削除する。失敗時は例外を投げる。"""
    devices = _ssdp_discover(timeout=timeout)
    if not devices:
        raise RuntimeError("UPnP対応ルーターが見つかりませんでした")

    last_err = None
    for location, _ in devices:
        try:
            control_url, service_type = _get_control_url(location)
            _soap_request(control_url, service_type, "DeletePortMapping", {
                "NewRemoteHost": "",
                "NewExternalPort": str(port),
                "NewProtocol": protocol.upper(),
            })
            return True
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"ポートマッピングの削除に失敗しました: {last_err}")


def get_external_ip(timeout=3):
    """ルーターのWAN側グローバルIPアドレスを取得"""
    devices = _ssdp_discover(timeout=timeout)
    if not devices:
        raise RuntimeError("UPnP対応ルーターが見つかりませんでした")

    last_err = None
    for location, _ in devices:
        try:
            control_url, service_type = _get_control_url(location)
            result = _soap_request(control_url, service_type, "GetExternalIPAddress", {})
            text = result.decode("utf-8", errors="ignore")
            m = re.search(r"<NewExternalIPAddress>(.*?)</NewExternalIPAddress>", text)
            if m:
                return m.group(1).strip()
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"外部IPアドレスの取得に失敗しました: {last_err}")
