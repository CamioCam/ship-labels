#!/usr/bin/env python3
"""
UPS shipping labels -> native 4x6 thermal output for a Zebra GC420d.

    python3 ups.py ship shipments/sample.json --format ZPL
    python3 ups.py ship-csv orders.csv --dry-run
    python3 ups.py void 1Z999AA10123456784
    python3 ups.py token

Configuration comes from .env (see .env.example) or the real environment.
Environment variables win over .env, so you can override per-command:

    UPS_ENV=prod python3 ups.py ship shipments/today.json
"""

import argparse
import base64
import csv
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
LABELS = BASE / "labels"
PREVIEWS = BASE / "previews"
LOG = BASE / "shipments_log.csv"
TOKEN_CACHE = BASE / ".token.json"
ENV_FILE = BASE / ".env"
BOXES_FILE = BASE / "boxes.json"

HOSTS = {
    "cie": "https://wwwcie.ups.com",
    "prod": "https://onlinetools.ups.com",
}

SERVICE_CODES = {
    "01": "Next Day Air",
    "02": "2nd Day Air",
    "03": "Ground",
    "12": "3 Day Select",
    "13": "Next Day Air Saver",
    "14": "Next Day Air Early",
    "59": "2nd Day Air A.M.",
    "65": "Worldwide Saver",
}

LABEL_EXT = {"ZPL": "zpl", "EPL": "epl", "GIF": "gif", "PNG": "png", "PDF": "pdf"}


class UpsError(RuntimeError):
    """Anything that should stop the run with a readable message."""


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_dotenv():
    """Minimal .env reader. Does not overwrite variables already in the env."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env(name, default=None, required=True):
    val = os.environ.get(name, default)
    if required and not val:
        raise UpsError(
            f"Missing {name}. Copy .env.example to .env and fill it in, "
            f"or export {name} in your shell."
        )
    return val


def host():
    mode = os.environ.get("UPS_ENV", "cie").lower()
    if mode not in HOSTS:
        raise UpsError(f"UPS_ENV must be 'cie' or 'prod', got {mode!r}")
    return HOSTS[mode], mode


def dpmm():
    """Zebra GC420d is 203 dpi = 8 dots/mm. A 300 dpi printer would be 12."""
    return os.environ.get("UPS_DPMM", "8")


# --------------------------------------------------------------------------
# OAuth
# --------------------------------------------------------------------------

def get_token(verbose=False):
    """Client-credentials token, cached on disk until shortly before expiry."""
    base, mode = host()

    if TOKEN_CACHE.exists():
        try:
            cached = json.loads(TOKEN_CACHE.read_text())
            if cached.get("env") == mode and cached.get("expires_at", 0) > time.time() + 60:
                if verbose:
                    left = int(cached["expires_at"] - time.time())
                    print(f"Using cached {mode} token ({left}s remaining)")
                return cached["access_token"]
        except (ValueError, KeyError):
            pass  # corrupt cache, just re-auth

    client_id = env("UPS_CLIENT_ID")
    client_secret = env("UPS_CLIENT_SECRET")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    resp = requests.post(
        f"{base}/security/v1/oauth/token",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise UpsError(f"OAuth failed [{resp.status_code}]: {resp.text[:500]}")

    payload = resp.json()
    token = payload["access_token"]
    TOKEN_CACHE.write_text(json.dumps({
        "access_token": token,
        "expires_at": time.time() + int(payload.get("expires_in", 3600)),
        "env": mode,
    }))
    TOKEN_CACHE.chmod(0o600)
    if verbose:
        print(f"New {mode} token acquired, valid {payload.get('expires_in')}s")
    return token


def auth_headers():
    return {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json",
        "transId": f"lbl-{int(time.time() * 1000)}",
        "transactionSrc": "ups-labels",
    }


def explain_error(resp):
    """UPS buries the real message; dig it out so failures are readable."""
    try:
        body = resp.json()
        errors = (body.get("response") or {}).get("errors") or []
        if errors:
            return "; ".join(f"[{e.get('code')}] {e.get('message')}" for e in errors)
        return json.dumps(body)[:600]
    except ValueError:
        return resp.text[:600]


# --------------------------------------------------------------------------
# Request building
# --------------------------------------------------------------------------

def address_block(party, account=None):
    """Turn a flat dict from our JSON into the nested shape UPS expects."""
    required = ("name", "phone", "address", "city", "state", "zip")
    missing = [f for f in required if not party.get(f)]
    if missing:
        raise UpsError(
            f"Address for {party.get('name', '<unnamed>')} is missing: {', '.join(missing)}"
        )

    block = {
        "Name": party["name"][:35],
        "AttentionName": party.get("attention", party["name"])[:35],
        "Phone": {"Number": "".join(c for c in str(party["phone"]) if c.isdigit())},
        "Address": {
            "AddressLine": [l for l in party["address"] if l][:3],
            "City": party["city"],
            "StateProvinceCode": party["state"],
            "PostalCode": str(party["zip"]),
            "CountryCode": party.get("country", "US"),
        },
    }
    if party.get("residential"):
        block["Address"]["ResidentialAddressIndicator"] = ""
    if account:
        block["ShipperNumber"] = account
    if party.get("email"):
        block["EMailAddress"] = party["email"]
    return block


def package_block(pkg, description):
    # Reached from hand-written shipments/*.json as well as boxes.json, so
    # surface a missing weight as a readable error rather than a KeyError.
    if pkg.get("weight") in (None, ""):
        raise UpsError(
            f"package {pkg.get('description', description)!r} has no weight"
        )
    package = {
        "Description": pkg.get("description", description)[:35],
        "Packaging": {"Code": pkg.get("packaging", "02")},
        "PackageWeight": {
            "UnitOfMeasurement": {"Code": pkg.get("weight_unit", "LBS")},
            "Weight": str(pkg["weight"]),
        },
    }
    if all(pkg.get(d) for d in ("length", "width", "height")):
        package["Dimensions"] = {
            "UnitOfMeasurement": {"Code": "IN"},
            "Length": str(pkg["length"]),
            "Width": str(pkg["width"]),
            "Height": str(pkg["height"]),
        }
    if pkg.get("declared_value"):
        package["PackageServiceOptions"] = {
            "DeclaredValue": {
                "CurrencyCode": "USD",
                "MonetaryValue": str(pkg["declared_value"]),
            }
        }
    return package


# Quantum View Notify codes. An email address sitting in the ShipTo block is
# only contact data - UPS sends nothing unless the request also carries these.
NOTIFY_SHIP = "6"       # shipment confirmation, sent when the label is created
NOTIFY_DELIVERY = "8"   # delivery confirmation
NOTIFY_EXCEPTION = "7"  # delivery exception - not requested, listed for later

MAX_NOTIFY_ADDRESSES = 5  # UPS caps each notification at five recipients


def notify_addresses(ship_to):
    """
    Who hears about this shipment: the customer contacts from the order form,
    whoever submitted that row, and the standing UPS_NOTIFY_EMAIL list.

    UPS caps recipients, so the standing list claims its slots first -
    "always copied" has to survive a row with a lot of contacts on it.
    """
    found, seen = [], set()
    raw = re.split(r"[,;]", os.environ.get("UPS_NOTIFY_EMAIL", ""))
    raw += list(ship_to.get("emails") or [ship_to.get("email", "")])
    raw += list(ship_to.get("notify_also") or [])
    for candidate in raw:
        candidate = (candidate or "").strip()
        if "@" not in candidate or candidate.lower() in seen:
            continue
        seen.add(candidate.lower())
        found.append(candidate)
    return found[:MAX_NOTIFY_ADDRESSES]


def notification_blocks(shipper, ship_to):
    recipients = notify_addresses(ship_to)
    if not recipients:
        return None

    # UPS counts FromName across the whole shipment, not per notification
    # ([120661] "the maximum number of FromName allowed ... is 1"), so the
    # sender fields go on the first block only and the rest carry addresses.
    sender = {"FromName": shipper["name"][:35]}
    if shipper.get("email"):
        sender["FromEMailAddress"] = shipper["email"]
        sender["UndeliverableEMailAddress"] = shipper["email"]

    blocks = []
    for code in (NOTIFY_SHIP, NOTIFY_DELIVERY):
        email = {"EMailAddress": list(recipients)}
        if not blocks:
            email.update(sender)
        blocks.append({
            "NotificationCode": code,
            "EMail": email,
            "Locale": {"Language": "ENG", "Dialect": "US"},
        })
    return blocks


def build_request(shipper, shipment, label_format):
    account = env("UPS_ACCOUNT")
    ship_to = shipment["to"]
    service = str(shipment.get("service", "03"))
    description = shipment.get("description", "Merchandise")

    # "packages" is the multi-box form; "package" is the original single-box
    # shape that shipments/*.json still uses.
    packages = shipment.get("packages") or [shipment["package"]]

    ship = {
        "Description": description[:50],
        "Shipper": address_block(shipper, account=account),
        "ShipFrom": address_block(shipper),
        "ShipTo": address_block(ship_to),
        "PaymentInformation": {
            "ShipmentCharge": {
                "Type": "01",
                "BillShipper": {"AccountNumber": account},
            }
        },
        "Service": {
            "Code": service,
            "Description": SERVICE_CODES.get(service, ""),
        },
        "Package": [package_block(p, description) for p in packages],
    }

    notifications = notification_blocks(shipper, ship_to)
    if notifications:
        ship["ShipmentServiceOptions"] = {"Notification": notifications}

    return {
        "ShipmentRequest": {
            "Request": {
                "SubVersion": "2409",
                "RequestOption": "nonvalidate",
                "TransactionReference": {"CustomerContext": description[:50]},
            },
            "Shipment": ship,
            "LabelSpecification": {
                "LabelImageFormat": {"Code": label_format},
                "LabelStockSize": {"Height": "6", "Width": "4"},
            },
        }
    }


def warn_unprinted_shipper_lines(shipper, source):
    """
    UPS prints one street line in the return-address block. Say so out loud.

    Verified against cie: with ["100 Warehouse Way", "Unit 7"] the label shows
    only "100 WAREHOUSE WAY", and reversing the two shows only "UNIT 7" with no
    street at all. The dropped line still reaches UPS's records, so nothing
    errors - it just silently goes missing from the printed label, and an
    undeliverable package comes back to the wrong door.

    The ShipTo block is unaffected; recipients do get their second line.
    """
    lines = [line for line in shipper.get("address", []) if line]
    if len(lines) < 2:
        return
    dropped = ", ".join(repr(line) for line in lines[1:])
    print(f"  note: UPS prints only the first return-address line, so {dropped} "
          f"will not appear on the label.\n"
          f"        Combine them into one line in {source}, "
          f'e.g. "{" ".join(lines)}".',
          file=sys.stderr)


RATE_PROBE_TO = {
    "name": "Rating Check",
    "phone": "2125551212",
    "address": ["350 Fifth Avenue"],
    "city": "New York",
    "state": "NY",
    "zip": "10118",
}


NO_ENTITLEMENT_HELP = (
    "The credentials are valid; the token just carries no entitlement for that\n"
    "product. On developer.ups.com, open the app and check its Subscription APIs\n"
    "table - Shipping and Rating each have to be listed there, with a check under\n"
    "Test or Prod to match UPS_ENV. A newly added product can take a while to\n"
    "propagate; if you just added it, retry later."
)


# What an entitled account answers the empty probe with: 400, carrying
# [9120004] Missing shipment information. Anything else - 404, 429, 5xx -
# proves nothing either way and must not read as success.
SHIPPING_PROBE_REJECTED = 400


def shipping_probe():
    """
    Prove the token reaches Shipping, without creating anything.

    Rating and Shipping are separate UPS products, so a successful rate quote
    says nothing about whether `ship` will work - that is exactly how this
    project's first credentials failed. The request below carries no Shipment
    at all: no address, no service, no package, so no label can come of it.
    An entitled account answers with a validation complaint, an unentitled
    one with 401, and those are easy to tell apart.
    """
    base, _ = host()
    return requests.post(
        f"{base}/api/shipments/v2409/ship",
        headers=auth_headers(),
        json={"ShipmentRequest": {"Request": {"RequestOption": "nonvalidate"}}},
        timeout=30,
    )


def rating_probe(shipper):
    """
    Rate a 1 lb Ground package as a credentials check.

    Rating creates no shipment and is never billed, which makes it the only
    call that proves the token can actually reach a UPS product. A token that
    OAuth happily issues can still be rejected by every API, so checking OAuth
    alone reports success on credentials that cannot ship anything.
    """
    base, _ = host()
    account = env("UPS_ACCOUNT")
    body = {
        "RateRequest": {
            "Request": {"TransactionReference": {"CustomerContext": "credential check"}},
            "Shipment": {
                "Shipper": address_block(shipper, account=account),
                "ShipFrom": address_block(shipper),
                "ShipTo": address_block(RATE_PROBE_TO),
                "Service": {"Code": "03", "Description": "Ground"},
                "Package": {
                    "PackagingType": {"Code": "02"},
                    "PackageWeight": {
                        "UnitOfMeasurement": {"Code": "LBS"},
                        "Weight": "1",
                    },
                },
            },
        }
    }
    return requests.post(
        f"{base}/api/rating/v2409/Rate",
        headers=auth_headers(),
        json=body,
        timeout=30,
    )


# --------------------------------------------------------------------------
# Label creation
# --------------------------------------------------------------------------

def create_label(shipper, shipment, label_format):
    base, mode = host()
    body = build_request(shipper, shipment, label_format)

    resp = requests.post(
        f"{base}/api/shipments/v2409/ship",
        headers=auth_headers(),
        json=body,
        timeout=60,
    )
    if resp.status_code != 200:
        raise UpsError(f"Ship request failed [{resp.status_code}]: {explain_error(resp)}")

    results = resp.json()["ShipmentResponse"]["ShipmentResults"]
    shipment_id = results.get("ShipmentIdentificationNumber", "")
    packages = results["PackageResults"]
    if isinstance(packages, dict):
        packages = [packages]

    charges = results.get("ShipmentCharges", {}).get("TotalCharges", {})
    negotiated = (
        results.get("NegotiatedRateCharges", {})
        .get("TotalCharge", {})
        .get("MonetaryValue")
    )

    ship_to = shipment["to"]
    service = str(shipment.get("service", "03"))
    out = []
    for i, p in enumerate(packages):
        tracking = p["TrackingNumber"]
        raw = base64.b64decode(p["ShippingLabel"]["GraphicImage"])
        label_path = LABELS / f"{tracking}.{LABEL_EXT[label_format]}"
        label_path.write_bytes(raw)

        out.append({
            "tracking": tracking,
            "shipment_id": shipment_id,
            "label_path": label_path,
            "format": label_format,
            "service": SERVICE_CODES.get(service, service),
            "to_name": ship_to["name"],
            "to_city": f'{ship_to["city"]}, {ship_to["state"]} {ship_to["zip"]}',
            # TotalCharges covers the whole shipment, not one package. Copying
            # it onto every package would triple a 3-box order when the log is
            # summed, so it lands on the first package and the rest read blank.
            "cost": (negotiated or charges.get("MonetaryValue", "")) if i == 0 else "",
            "env": mode,
        })
    return out


def render_preview(label_path, tracking):
    """
    Produce a viewable PNG of the label.

    GIF/PNG/PDF render locally with no network. ZPL and EPL are printer
    languages with no local renderer, so those go to Labelary if it is
    reachable. Losing the preview does not affect printing.
    """
    png = PREVIEWS / f"{tracking}.png"
    suffix = label_path.suffix.lower()

    if suffix in (".gif", ".png"):
        try:
            from PIL import Image
        except ImportError:
            print("  (no preview: pip install Pillow)", file=sys.stderr)
            return None
        img = Image.open(label_path)
        # UPS returns raster labels rotated 90 degrees; stand it upright.
        if img.width > img.height:
            img = img.rotate(-90, expand=True)
        img.convert("RGB").save(png)
        return png

    if suffix == ".pdf":
        if not shutil.which("pdftoppm"):
            print("  (no preview: pdftoppm not installed)", file=sys.stderr)
            return None
        subprocess.run(
            ["pdftoppm", "-png", "-r", "150", "-singlefile",
             str(label_path), str(png.with_suffix(""))],
            check=True, capture_output=True,
        )
        return png if png.exists() else None

    try:
        resp = requests.post(
            f"http://api.labelary.com/v1/printers/{dpmm()}dpmm/labels/4x6/0/",
            headers={"Accept": "image/png"},
            data=label_path.read_bytes(),
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"  (no preview: renderer returned {resp.status_code})", file=sys.stderr)
            return None
        png.write_bytes(resp.content)
        return png
    except requests.RequestException as exc:
        print(f"  (no preview: {type(exc).__name__} - label file is still valid)",
              file=sys.stderr)
        return None


# Only these two are printer languages that want `lpr -o raw`. A GIF or PDF
# label prints through the normal driver, so suggesting raw would be wrong.
PRINTER_LANGUAGES = ("ZPL", "EPL")

# CUPS mangles the model into something like Zebra_Technologies_ZTC_GC420d__EPL_.
# Ranked: a name that says Zebra beats a generic "label", so a DYMO_LabelWriter
# sharing the desk can't shadow the actual thermal printer.
STRONG_PRINTER_HINTS = ("zebra", "gc420", "zdesigner")
WEAK_PRINTER_HINTS = ("label", "thermal")


def pick_queue(queues, default):
    """
    Choose a queue, or nothing. Never choose arbitrarily.

    Whoever pastes the command is standing next to the printers and we are
    not: if two are equally plausible, naming one is worse than saying so,
    because raw EPL sent to the wrong printer produces pages of garbage.
    """
    for hints in (STRONG_PRINTER_HINTS, WEAK_PRINTER_HINTS):
        matches = [q for q in queues if any(h in q.lower() for h in hints)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            # Several candidates at the same rank; the default breaks the tie
            # only if it is one of them.
            return default if default in matches else None
    if default in queues:
        return default
    return queues[0] if len(queues) == 1 else None


def detect_printer():
    """
    Best guess at the CUPS queue for the label printer.

    The queue name is nothing like "Zebra_GC420d", and hand-guessing it is how
    the first print attempt failed with a misleading "No such file or
    directory". Returns None rather than raising - printer discovery must
    never interfere with a shipping run.
    """
    if not shutil.which("lpstat"):
        return None
    try:
        listed = subprocess.run(["lpstat", "-p"], capture_output=True,
                                text=True, timeout=5)
        default = subprocess.run(["lpstat", "-d"], capture_output=True,
                                 text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None

    queues = re.findall(r"^printer (\S+)", listed.stdout, re.M)
    match = re.search(r"destination:\s*(\S+)", default.stdout)
    return pick_queue(queues, match.group(1) if match else None)


def print_instructions(rows, label_format):
    """
    Show one copy-pasteable command that prints every label from this run.

    Absolute paths, because the obvious relative form only works from the
    repo directory. A loop rather than one lpr with many files, so each label
    is its own job and a jam in the middle doesn't take the rest with it.
    """
    if label_format not in PRINTER_LANGUAGES or not rows:
        return

    # shlex.quote, not double quotes: those still expand $variables and run
    # $(command substitution), so a path or queue name containing either
    # would paste as something other than what it says.
    paths = [shlex.quote(str(Path(r["label_path"]).resolve())) for r in rows]
    queue = detect_printer()
    target = f" -P {shlex.quote(queue)}" if queue else ""

    print()
    if queue:
        print(f"Print {len(paths)} label(s) on {queue}:")
    else:
        print(f"Print {len(paths)} label(s) - run `lpstat -p` to find your queue "
              "name, then add -P <queue>:")
    if len(paths) == 1:
        print(f"  lpr -o raw{target} {paths[0]}")
    else:
        print(f'  for f in {" ".join(paths)}; do lpr -o raw{target} "$f"; done')


def log_rows(rows):
    new = not LOG.exists()
    fields = ["created_at", "env", "tracking", "shipment_id", "service",
              "to_name", "to_city", "cost", "format", "label_file", "voided"]
    with LOG.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        if new:
            writer.writeheader()
        for r in rows:
            writer.writerow({
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "env": r["env"],
                "tracking": r["tracking"],
                "shipment_id": r["shipment_id"],
                "service": r["service"],
                "to_name": r["to_name"],
                "to_city": r["to_city"],
                "cost": r["cost"],
                "format": r["format"],
                "label_file": r["label_path"].name,
                "voided": "",
            })


# --------------------------------------------------------------------------
# Order-form CSV
#
# The order submission form exports one row per order. Columns we care about
# are located by name rather than position, because the form grows new
# questions over time and they land in the middle of the sheet.
# --------------------------------------------------------------------------

ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\s*$")
BOX_COL_RE = re.compile(r"\bbox\s+([0-9]{3,5}[a-z])\b")

METHOD_CODES = [
    ("next day air early", "14"),
    ("next day air saver", "13"),
    ("next day", "01"),
    ("overnight", "01"),
    ("2nd day air a m", "59"),
    ("2nd day", "02"),
    ("second day", "02"),
    ("2 day", "02"),
    ("3 day", "12"),
    ("three day", "12"),
    ("ground", "03"),
]


def norm_header(text):
    """Fold a header to lowercase words so punctuation drift doesn't matter."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


DIMENSIONS = ("length", "width", "height")


def validate_box(code, spec):
    """
    Check a hand-edited box entry before it can reach UPS.

    Partial dimensions are the quiet failure worth catching: package_block()
    only sends Dimensions when all three are present, so a box missing one
    would ship with no dimensions at all rather than complaining.
    """
    where = f"{BOXES_FILE.name}: box {code}"
    if not isinstance(spec, dict):
        raise UpsError(f"{where} must be an object with weight and dimensions")

    try:
        weight = float(spec.get("weight"))
    except (TypeError, ValueError):
        raise UpsError(f"{where} needs a numeric weight in pounds")
    if weight <= 0:
        raise UpsError(f"{where} has a weight of {spec['weight']!r}")

    present = [d for d in DIMENSIONS if spec.get(d) not in (None, "")]
    if present and len(present) != len(DIMENSIONS):
        missing = ", ".join(d for d in DIMENSIONS if d not in present)
        raise UpsError(f"{where} is missing {missing} - give all three or none")
    for d in present:
        try:
            if float(spec[d]) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise UpsError(f"{where} has a {d} of {spec[d]!r}")
    return spec


def load_boxes():
    if not BOXES_FILE.exists():
        raise UpsError(
            f"{BOXES_FILE.name} not found. Copy boxes.example.json to "
            f"{BOXES_FILE.name} and fill in each box's weight and size."
        )
    data = json.loads(BOXES_FILE.read_text())
    return {
        code.upper(): validate_box(code, spec)
        for code, spec in data.items()
        if not code.startswith("_")
    }


def map_columns(header):
    """Locate the columns we need; remember every 'How many Box ____' column."""
    norms = [norm_header(h) for h in header]

    def exact(name):
        return norms.index(name) if name in norms else None

    def contains(fragment):
        for i, n in enumerate(norms):
            if fragment in n:
                return i
        return None

    cols = {
        "company": exact("company"),
        "contact": exact("contact person"),
        "address": contains("ship to address"),
        # "email" is the customer; "email address" is whoever submitted the
        # form. The submitter is notified but never goes on the label.
        "email": exact("email"),
        "submitter": exact("email address"),
        "phone": exact("phone"),
        "po": contains("purchase order"),
        "method": contains("shipping method"),
        "tracking": exact("tracking number"),
    }
    if cols["tracking"] is None:
        raise UpsError('No "Tracking Number" column in the CSV - nowhere to write results.')
    if cols["address"] is None:
        raise UpsError('No ship-to address column in the CSV.')

    # Box quantity columns. The form repeats some headers verbatim (there are
    # two "How many Box 1110A" columns), so collect every match, not the first.
    boxes = []
    for i, n in enumerate(norms):
        m = BOX_COL_RE.search(n)
        if m:
            boxes.append((i, m.group(1).upper()))
    if not boxes:
        raise UpsError('No "How many Box ____" columns found in the CSV.')
    cols["boxes"] = boxes
    return cols


def cell(row, idx):
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def parse_address(raw):
    """
    Split the form's single free-text address field into UPS's fields.

    The form accepts anything, so this handles both of the shapes it produces:
        "1 Byron Way, Suite 4, Springfield, IL, 62704"
        "2 Enigma Rd.\\nPortland, OR 97205"
    Anything it can't read confidently raises rather than guessing, since a
    wrong city or state becomes a UPS address-correction surcharge.
    """
    parts = [p.strip(" ,\t") for p in re.split(r"[\n,]+", raw or "") if p.strip(" ,\t")]
    if not parts:
        raise UpsError("ship-to address is blank")

    m = ZIP_RE.search(parts[-1])
    if not m:
        raise UpsError(f"no ZIP code at the end of {raw.strip()!r}")
    postal = m.group(0).strip()
    remainder = parts[-1][:m.start()].strip(" ,")
    if remainder:
        parts[-1] = remainder
    else:
        parts.pop()

    if not parts or not re.fullmatch(r"[A-Za-z]{2}", parts[-1]):
        raise UpsError(f"no two-letter state before the ZIP in {raw.strip()!r}")
    state = parts.pop().upper()

    if not parts:
        raise UpsError(f"no city in {raw.strip()!r}")
    city = parts.pop()

    if not parts:
        raise UpsError(f"no street address in {raw.strip()!r}")
    return parts[:3], city, state, postal


def parse_service(text, default):
    """Map the form's free-text shipping method onto a UPS service code."""
    n = norm_header(text)
    if not n:
        return default
    if re.fullmatch(r"\d{2}", n):
        return n
    for fragment, code in METHOD_CODES:
        if fragment in n:
            return code
    raise UpsError(f"unrecognized shipping method {text.strip()!r}")


def parse_emails(text):
    """The form lets people list several contacts in one cell."""
    return [c.strip(" ,;") for c in re.split(r"[,;\s]+", text or "") if "@" in c]


def build_order(row, cols, boxes, default_service):
    """One CSV row -> one shipment dict, or a reason it can't ship."""
    wanted = []
    for idx, code in cols["boxes"]:
        raw = cell(row, idx)
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            raise UpsError(f"box {code} quantity {raw!r} is not a number")
        # Truncating would quietly under-ship the order, so anything that
        # isn't a whole count of boxes stops the run. "2.0" is still fine.
        if not value.is_integer():
            raise UpsError(f"box {code} quantity {raw!r} is not a whole number of boxes")
        if value < 0:
            raise UpsError(f"box {code} quantity {raw!r} is negative")
        if value:
            wanted.append((code, int(value)))
    if not wanted:
        return None, "no boxes ordered"

    packages = []
    for code, qty in wanted:
        spec = boxes.get(code)
        if not spec:
            raise UpsError(
                f"Box {code} has no entry in {BOXES_FILE.name} - "
                "add its weight and dimensions before shipping it"
            )
        for _ in range(qty):
            packages.append(dict(spec, description=spec.get("description", f"Box {code}")))

    lines, city, state, postal = parse_address(cell(row, cols["address"]))
    company = cell(row, cols["company"])
    contact = cell(row, cols["contact"])
    if not company and not contact:
        raise UpsError("no company or contact person")

    contents = " ".join(f"{code}x{qty}" for code, qty in wanted)
    po = cell(row, cols["po"])
    description = f"PO {po} {contents}".strip() if po else contents

    emails = parse_emails(cell(row, cols["email"]))
    shipment = {
        "description": description,
        "service": parse_service(cell(row, cols["method"]), default_service),
        "to": {
            "name": company or contact,
            "attention": contact or company,
            "phone": cell(row, cols["phone"]),
            # "email" is the single address UPS puts in the ShipTo block;
            # "emails" is everyone the form listed, for notifications.
            "email": emails[0] if emails else "",
            "emails": emails,
            # Notified, but deliberately kept off the label.
            "notify_also": parse_emails(cell(row, cols["submitter"])),
            "address": lines,
            "city": city,
            "state": state,
            "zip": postal,
        },
        "packages": packages,
    }
    return shipment, None


def read_orders(path, default_service):
    """Parse the whole sheet up front so problems surface before any billing."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        raise UpsError(f"{path} is empty")

    header, body = rows[0], rows[1:]
    cols = map_columns(header)
    boxes = load_boxes()

    orders = []
    for i, row in enumerate(body):
        if not any((c or "").strip() for c in row):
            continue
        entry = {"index": i, "row": row, "shipment": None, "skip": None, "error": None}
        if cell(row, cols["tracking"]):
            entry["skip"] = f"already shipped ({cell(row, cols['tracking'])})"
        else:
            try:
                entry["shipment"], entry["skip"] = build_order(row, cols, boxes, default_service)
            except UpsError as exc:
                entry["error"] = str(exc)
        orders.append(entry)
    return header, body, cols, orders


def preflight_destination(path):
    """
    Prove the checkpoint can be written before a single label is billed.

    write_orders() creates its temp file beside the destination, so a missing
    or unwritable parent raises only after UPS has already charged for the
    label - and the tracking number for that label is then lost. Fail here
    instead, while nothing has been spent.
    """
    parent = Path(path).parent
    if not parent.is_dir():
        raise UpsError(
            f"{parent} does not exist, so tracking numbers could not be saved. "
            "Create it before shipping."
        )
    try:
        fd, probe = tempfile.mkstemp(dir=parent, prefix=".ups-probe-", suffix=".tmp")
    except OSError as exc:
        raise UpsError(f"cannot write next to {path}: {exc}")
    os.close(fd)
    Path(probe).unlink(missing_ok=True)


def write_orders(path, header, body):
    """
    Replace the sheet atomically.

    Writing in place would truncate the operator's file the instant it opens,
    so a crash mid-write loses every tracking number recorded so far - for
    labels that are already billed. Write beside it, then rename over.
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ups-", suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            writer.writerows(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_ship_csv(args):
    LABELS.mkdir(exist_ok=True)
    PREVIEWS.mkdir(exist_ok=True)

    csv_path = Path(args.orders)
    if not csv_path.exists():
        raise UpsError(f"{csv_path} not found")
    out_path = Path(args.out) if args.out else csv_path

    # Skip-if-already-shipped reads the INPUT file, so with --out the previous
    # run's tracking numbers are somewhere this run never looks. Re-running the
    # same command would see pristine rows and bill them a second time.
    if out_path.resolve() != csv_path.resolve() and out_path.exists():
        raise UpsError(
            f"{out_path.name} already exists, and it - not {csv_path.name} - holds the "
            f"tracking numbers from the earlier run. Shipping from {csv_path.name} again "
            f"would re-bill those rows.\nPass {out_path.name} as the input to continue, "
            "or delete it to start over."
        )

    shipper_path = Path(args.shipper)
    if not shipper_path.exists():
        raise UpsError(
            f"{shipper_path.name} not found. "
            "Copy shipper.example.json to shipper.json and fill in your address."
        )
    shipper = json.loads(shipper_path.read_text())
    warn_unprinted_shipper_lines(shipper, shipper_path.name)

    header, body, cols, orders = read_orders(csv_path, args.service)

    ready = [o for o in orders if o["shipment"]]
    if args.limit:
        ready = ready[:args.limit]
    ready_ids = {id(o) for o in ready}

    print(f"{csv_path.name}: {len(orders)} order row(s)\n")
    for o in orders:
        label = f"  row {o['index'] + 2:>3}  "
        if o["error"]:
            print(f"{label}ERROR    {o['error']}", file=sys.stderr)
        elif o["skip"]:
            print(f"{label}skipped  {o['skip']}")
        elif id(o) not in ready_ids:
            print(f"{label}skipped  past --limit {args.limit}")
        else:
            s = o["shipment"]
            to = s["to"]
            print(f"{label}{to['name']} - {to['city']}, {to['state']} {to['zip']}  "
                  f"{len(s['packages'])} pkg  {SERVICE_CODES.get(s['service'], s['service'])}")
    print()

    errors = [o for o in orders if o["error"]]
    if errors and not args.skip_errors:
        raise UpsError(
            f"{len(errors)} row(s) could not be parsed (see above). "
            "Fix the sheet, or pass --skip-errors to ship the rest."
        )
    if not ready:
        print("Nothing to ship.")
        return 1 if errors else 0

    # Before anything is billed, not after the first label comes back.
    preflight_destination(out_path)

    _, mode = host()
    if mode == "prod" and not args.yes:
        print(f"About to create PRODUCTION labels for {len(ready)} order(s).")
        print("These are billed to your UPS account immediately.")
        if input("Type 'yes' to continue: ").strip().lower() != "yes":
            print("Aborted.")
            return 1
        print()

    if args.dry_run:
        print("Dry run - no labels created.")
        return 0

    banner = "test sandbox, no charges" if mode == "cie" else "*** PRODUCTION - real charges ***"
    print(f"UPS {mode.upper()}  ({banner})\n")

    if out_path == csv_path:
        backup = csv_path.with_suffix(csv_path.suffix + ".bak")
        shutil.copy2(csv_path, backup)
        print(f"Backed up original to {backup.name}\n")

    rows, failed = [], 0
    for n, o in enumerate(ready, 1):
        shipment = o["shipment"]
        print(f"[{n}/{len(ready)}] row {o['index'] + 2}  {shipment['to']['name']} ...")
        try:
            created = create_label(shipper, shipment, args.format)
        except UpsError as exc:
            print(f"  FAILED: {exc}\n", file=sys.stderr)
            failed += 1
            continue

        # These labels are billed the moment create_label() returns, so the
        # sheet is updated before anything optional runs. After this point a
        # failed preview or a Ctrl-C costs a preview, not the only record that
        # a shipment exists.
        row = o["row"]
        if len(row) <= cols["tracking"]:
            row.extend([""] * (cols["tracking"] + 1 - len(row)))
        row[cols["tracking"]] = ", ".join(r["tracking"] for r in created)
        write_orders(out_path, header, body)
        log_rows(created)
        rows.extend(created)

        for r in created:
            print(f"  tracking {r['tracking']}   {r['service']}"
                  + (f"   ${r['cost']}" if r["cost"] else ""))
            try:
                preview = render_preview(r["label_path"], r["tracking"])
            except Exception as exc:
                # Pillow and pdftoppm raise their own errors; none of them are
                # worth aborting a run over once the label is paid for.
                print(f"  (no preview: {type(exc).__name__})", file=sys.stderr)
                preview = None
            print(f"  label    {r['label_path'].name}"
                  + (f"   preview {preview.name}" if preview else ""))
        print()

    print(f"Wrote {len(rows)} tracking number(s) to {out_path.name}")
    print(f"Logged {len(rows)} label(s) to {LOG.name}")
    print_instructions(rows, args.format)
    return 1 if failed or errors else 0


def cmd_ship(args):
    LABELS.mkdir(exist_ok=True)
    PREVIEWS.mkdir(exist_ok=True)

    shipper_path = Path(args.shipper)
    if not shipper_path.exists():
        raise UpsError(
            f"{shipper_path.name} not found. "
            "Copy shipper.example.json to shipper.json and fill in your address."
        )
    shipper = json.loads(shipper_path.read_text())
    warn_unprinted_shipper_lines(shipper, shipper_path.name)

    data = json.loads(Path(args.shipments).read_text())
    shipments = data if isinstance(data, list) else [data]

    _, mode = host()
    if mode == "prod" and not args.yes:
        print(f"About to create {len(shipments)} PRODUCTION label(s).")
        print("These are billed to your UPS account immediately.")
        if input("Type 'yes' to continue: ").strip().lower() != "yes":
            print("Aborted.")
            return 1
        print()

    banner = "test sandbox, no charges" if mode == "cie" else "*** PRODUCTION - real charges ***"
    print(f"UPS {mode.upper()}  ({banner})\n")

    rows, shipped = [], 0
    for i, shipment in enumerate(shipments, 1):
        print(f"[{i}/{len(shipments)}] {shipment['to']['name']} ...")
        try:
            created = create_label(shipper, shipment, args.format)
        except UpsError as exc:
            print(f"  FAILED: {exc}\n", file=sys.stderr)
            continue
        shipped += 1
        for r in created:
            try:
                preview = render_preview(r["label_path"], r["tracking"])
            except Exception as exc:
                print(f"  (no preview: {type(exc).__name__})", file=sys.stderr)
                preview = None
            print(f"  tracking {r['tracking']}   {r['service']}"
                  + (f"   ${r['cost']}" if r["cost"] else ""))
            print(f"  label    {r['label_path'].name}"
                  + (f"   preview {preview.name}" if preview else ""))
        rows.extend(created)
        print()

    if rows:
        log_rows(rows)
        print(f"Logged {len(rows)} label(s) to {LOG.name}")
        print_instructions(rows, args.format)
    # Count shipments, not packages - one shipment can return several labels.
    return 0 if shipped == len(shipments) else 1


def cmd_void(args):
    """
    Cancel a shipment so it is not billed.

    NOTE: the void path is not published in UPS's public OpenAPI spec, so this
    is built from the documented convention and has NOT been verified against
    the live API. Run it once against UPS_ENV=cie before trusting it. If the
    path is wrong you'll get a 404 - check the current Void Shipment reference
    on developer.ups.com and correct VOID_PATH below.
    """
    VOID_PATH = "/api/shipments/v1/void/cancel/{id}"

    base, mode = host()
    url = base + VOID_PATH.format(id=args.identifier)

    resp = requests.delete(url, headers=auth_headers(), timeout=30)
    if resp.status_code == 404:
        raise UpsError(
            f"Void endpoint returned 404 for {url}\n"
            "The path convention may have changed - see the note in cmd_void()."
        )
    if resp.status_code != 200:
        raise UpsError(f"Void failed [{resp.status_code}]: {explain_error(resp)}")

    print(f"Voided {args.identifier} in {mode}.")
    print(json.dumps(resp.json(), indent=2)[:800])
    return 0


def cmd_token(args):
    get_token(verbose=True)
    if args.no_verify:
        return 0

    _, mode = host()
    # Falling back is a convenience for the default path only. If someone
    # named a shipper file explicitly, a typo has to fail rather than quietly
    # rate from a different origin than they think they're checking.
    shipper_path = Path(args.shipper) if args.shipper else BASE / "shipper.json"
    if not shipper_path.exists():
        if args.shipper:
            raise UpsError(f"{shipper_path} not found")
        shipper_path = BASE / "shipper.example.json"
        print(f"(no shipper.json - rating from the {shipper_path.name} address)")
    shipper = json.loads(shipper_path.read_text())

    resp = rating_probe(shipper)
    if resp.status_code != 200:
        sys.stdout.flush()  # keep the diagnostic below the token line when piped
        print(f"\nRating   FAILED [{resp.status_code}]: {explain_error(resp)}",
              file=sys.stderr)
        if resp.status_code == 401:
            print(NO_ENTITLEMENT_HELP, file=sys.stderr)
        return 1

    rated = resp.json()["RateResponse"]["RatedShipment"]
    if isinstance(rated, list):
        rated = rated[0]
    charge = rated["TotalCharges"]
    print(f"Rating   OK - quoted {charge['MonetaryValue']} {charge['CurrencyCode']} "
          f"for 1 lb Ground to New York, NY")

    # Shipping is a separate product. Stopping here would report success on
    # credentials that can price a shipment but not create one.
    resp = shipping_probe()
    if resp.status_code == 401:
        sys.stdout.flush()
        print(f"\nShipping FAILED [401]: {explain_error(resp)}", file=sys.stderr)
        print(NO_ENTITLEMENT_HELP, file=sys.stderr)
        return 1
    if resp.status_code != SHIPPING_PROBE_REJECTED:
        # Not refused, but not the answer that proves access either. A 500 or
        # a 404 says nothing about entitlement, and reporting it as success is
        # how the old OAuth-only check misled us in the first place.
        sys.stdout.flush()
        print(f"\nShipping UNPROVEN [{resp.status_code}]: {explain_error(resp)}\n"
              "Authorization was not refused, but UPS did not answer with the expected\n"
              "complaint about the empty probe either, so shipping access is unconfirmed.\n"
              "Retry; if it persists, check developer.ups.com before shipping.",
              file=sys.stderr)
        return 1
    print("Shipping OK - the ship endpoint authorized the token and rejected the "
          "probe on its contents (nothing was created)")
    print(f"\nCredentials can rate and ship in {mode}.")
    return 0


def main():
    load_dotenv()

    ap = argparse.ArgumentParser(
        prog="ups.py",
        description="Create UPS 4x6 thermal shipping labels.",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_ship = sub.add_parser("ship", help="create labels from a shipments JSON file")
    p_ship.add_argument("shipments", help="JSON file: one shipment object or a list")
    p_ship.add_argument("--shipper", default=str(BASE / "shipper.json"))
    p_ship.add_argument("--format", default="ZPL",
                        choices=["ZPL", "EPL", "GIF", "PNG", "PDF"])
    p_ship.add_argument("--yes", action="store_true",
                        help="skip the production confirmation prompt")
    p_ship.set_defaults(func=cmd_ship)

    p_csv = sub.add_parser(
        "ship-csv",
        help="create labels from an order-form CSV and write back tracking numbers",
    )
    p_csv.add_argument("orders", help="CSV exported from the order submission form")
    p_csv.add_argument("--shipper", default=str(BASE / "shipper.json"))
    p_csv.add_argument("--format", default="ZPL",
                       choices=["ZPL", "EPL", "GIF", "PNG", "PDF"])
    p_csv.add_argument("--service", default="03",
                       help="service code for rows with no shipping method (default 03 Ground)")
    p_csv.add_argument("--out", help="write the updated CSV here instead of in place")
    p_csv.add_argument("--limit", type=int,
                       help="only ship the first N eligible rows")
    p_csv.add_argument("--dry-run", action="store_true",
                       help="parse and report, create no labels")
    p_csv.add_argument("--skip-errors", action="store_true",
                       help="ship the good rows even if some rows fail to parse")
    p_csv.add_argument("--yes", action="store_true",
                       help="skip the production confirmation prompt")
    p_csv.set_defaults(func=cmd_ship_csv)

    p_void = sub.add_parser("void", help="cancel a shipment (see caveat in source)")
    p_void.add_argument("identifier", help="shipment identification or tracking number")
    p_void.set_defaults(func=cmd_void)

    p_token = sub.add_parser(
        "token",
        help="verify credentials: fetch a token, then prove it with a free rate quote",
    )
    p_token.add_argument("--shipper", default=None,
                         help="ship-from address to rate against (default shipper.json)")
    p_token.add_argument("--no-verify", action="store_true",
                         help="only fetch the token, skip the rating check")
    p_token.set_defaults(func=cmd_token)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except UpsError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as exc:
        print(f"\nNetwork error reaching UPS: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
