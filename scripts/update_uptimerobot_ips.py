#!/usr/bin/env python3
"""Refresh the checked-in UptimeRobot probe address list.

UptimeRobot checks ``monitor.fighthealthinsurance.com/metrics`` from the public
internet, so MetricsAccessMiddleware has to recognize its probes (see
docs/metrics-endpoint-access.md). UptimeRobot rotates these addresses, so
re-run this when probes start getting 404s:

    python scripts/update_uptimerobot_ips.py

It rewrites fighthealthinsurance/uptimerobot_ips.py in place; commit the diff.
"""

import datetime
import ipaddress
import pathlib
import sys
import urllib.request

SOURCE_URL = "https://uptimerobot.com/inc/files/ips/IPv4andIPv6.txt"
TARGET = (
    pathlib.Path(__file__).resolve().parent.parent
    / "fighthealthinsurance"
    / "uptimerobot_ips.py"
)

HEADER = '''"""UptimeRobot probe addresses -- GENERATED, do not edit by hand.

Fetched {date} from
{url}
by ``python scripts/update_uptimerobot_ips.py``; re-run that to refresh.
Consumed by MetricsAccessMiddleware, which lets these addresses through to
/metrics even though they arrive via the ingress.
"""

from typing import Tuple

UPTIMEROBOT_SOURCE_URL = "{url}"

UPTIMEROBOT_IPS: Tuple[str, ...] = (
'''


def fetch() -> list:
    # uptimerobot.com rejects the default urllib User-Agent with a 403.
    request = urllib.request.Request(
        SOURCE_URL, headers={"User-Agent": "fighthealthinsurance-ip-refresh"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
    addresses = []
    for line in body.split():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            print(f"skipping unparseable entry: {candidate!r}", file=sys.stderr)
            continue
        addresses.append(candidate)
    return addresses


def main() -> int:
    addresses = fetch()
    if len(addresses) < 20:
        # The published list has been ~200 entries for years; a near-empty
        # response means the URL moved or we got an error page. Refuse to
        # shrink the allowlist on that basis.
        print(
            f"refusing to write only {len(addresses)} addresses from {SOURCE_URL}",
            file=sys.stderr,
        )
        return 1
    body = HEADER.format(
        url=SOURCE_URL, date=datetime.date.today().isoformat()
    ) + "".join(f'    "{address}",\n' for address in addresses)
    TARGET.write_text(body + ")\n")
    print(f"wrote {len(addresses)} addresses to {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
