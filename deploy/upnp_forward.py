#!/usr/bin/env python3
"""Ask the home router (via UPnP) to forward web ports to this machine.

Used because the router's settings can't be reached directly. Standard library only.

    upnp_forward.py status          show the router's public IP and its current forwards
    upnp_forward.py add             forward TCP 80 and 443 to this machine (safe to repeat)
    upnp_forward.py remove          remove those forwards

Routers can forget UPnP forwards (for example after a reboot), so `add` runs from cron
every few minutes; it only changes something when a forward is missing.
"""

import re
import socket
import sys
import urllib.error
import urllib.request

PORTS = (80, 443)
DESCRIPTION = "photo-sync web"
SSDP_SEARCH = "\r\n".join([
    "M-SEARCH * HTTP/1.1", "HOST: 239.255.255.250:1900", 'MAN: "ssdp:discover"', "MX: 2",
    "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1", "", "",
]).encode()


def find_router():
    """(control URL, service type) of the router's port-forwarding service."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.settimeout(4)
    s.sendto(SSDP_SEARCH, ("239.255.255.250", 1900))
    try:
        reply = s.recvfrom(65507)[0].decode(errors="replace")
    except socket.timeout:
        sys.exit("No UPnP router answered.")
    location = re.search(r"(?im)^location:\s*(\S+)", reply).group(1)
    base = re.match(r"(https?://[^/]+)", location).group(1)
    desc = urllib.request.urlopen(location, timeout=5).read().decode(errors="replace")
    for block in re.findall(r"<service>(.*?)</service>", desc, re.S):
        stype = re.search(r"<serviceType>([^<]+)", block).group(1)
        if re.search(r"WAN(IP|PPP)Connection", stype):
            ctrl = re.search(r"<controlURL>([^<]+)", block).group(1)
            return (ctrl if ctrl.startswith("http") else base + ctrl), stype
    sys.exit("The router doesn't offer UPnP port forwarding.")


def soap(router, action, **args):
    url, stype = router
    params = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{stype}">{params}</u:{action}></s:Body></s:Envelope>')
    req = urllib.request.Request(url, body.encode(), {
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{stype}#{action}"'})
    try:
        return True, urllib.request.urlopen(req, timeout=5).read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        code = re.search(r"<errorCode>(\d+)", text)
        msg = re.search(r"<errorDescription>([^<]*)", text)
        return False, f"error {code.group(1) if code else e.code}: {msg.group(1) if msg else text[:200]}"


def field(xml, name):
    m = re.search(f"<{name}>([^<]*)", xml)
    return m.group(1) if m else None


def local_ip():
    """This machine's address on the home network (the one used to reach the router)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("192.168.1.1", 9))
    return s.getsockname()[0]


def forwards(router):
    out = []
    for i in range(100):
        ok, xml = soap(router, "GetGenericPortMappingEntry", NewPortMappingIndex=i)
        if not ok:
            break
        out.append({k: field(xml, "New" + k) for k in (
            "Protocol", "ExternalPort", "InternalClient", "InternalPort", "PortMappingDescription",
            "LeaseDuration", "Enabled")})
    return out


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    router = find_router()
    me = local_ip()
    if cmd == "status":
        ok, xml = soap(router, "GetExternalIPAddress")
        print("router public IP:", field(xml, "NewExternalIPAddress") if ok else xml)
        print("this machine:", me)
        current = forwards(router)
        print("forwards:" if current else "forwards: none")
        for f in current:
            print(f"  {f['Protocol']} {f['ExternalPort']} -> {f['InternalClient']}:{f['InternalPort']}"
                  f"  ({f['PortMappingDescription']}, lease {f['LeaseDuration']}s)")
    elif cmd == "add":
        current = {(f["Protocol"], f["ExternalPort"]): f for f in forwards(router)}
        for port in PORTS:
            f = current.get(("TCP", str(port)))
            if f and f["InternalClient"] == me and f["InternalPort"] == str(port):
                continue  # already in place
            if f:
                print(f"TCP {port} is forwarded to {f['InternalClient']} ({f['PortMappingDescription']}); "
                      "leaving it alone.")
                continue
            ok, msg = soap(router, "AddPortMapping", NewRemoteHost="", NewExternalPort=port,
                           NewProtocol="TCP", NewInternalPort=port, NewInternalClient=me,
                           NewEnabled=1, NewPortMappingDescription=DESCRIPTION, NewLeaseDuration=0)
            print(f"TCP {port} -> {me}:{port}: {'added' if ok else msg}")
    elif cmd == "remove":
        for port in PORTS:
            ok, msg = soap(router, "DeletePortMapping", NewRemoteHost="", NewExternalPort=port, NewProtocol="TCP")
            print(f"TCP {port}: {'removed' if ok else msg}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
