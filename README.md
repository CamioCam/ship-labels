# UPS Labels

Create UPS shipping labels as native 4x6 thermal files for a Zebra printer.
No screenshotting, no cropping, no scaling — UPS renders the label at exactly
4x6 in the printer's own language and you send the bytes straight to the Zebra.

Built for a **Zebra GC420d** (203 dpi, speaks both ZPL II and EPL2).

---

## Setup

### 1. Get UPS API credentials

1. Sign in at <https://developer.ups.com> with your existing UPS.com login.
2. **Apps** → **Add App**.
3. When asked why you need credentials, choose integrating UPS into your own
   business (not "on behalf of a client").
4. Select or add your **UPS account number** — this is what gets billed.
5. Name the app. If a callback/redirect URL is required, use `https://localhost`;
   it's unused by the client-credentials flow.
6. Under products select **Shipping**. Also add **Rating**, **Address
   Validation**, and **Tracking** — free to enable and useful later.
7. Submit, and copy the **Client Secret** immediately. UPS shows it once.

### 2. Install

```bash
pip3 install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env                    # then fill in your credentials
cp shipper.example.json shipper.json    # then fill in your ship-from address
cp boxes.example.json boxes.json        # then fill in your box weights and sizes
```

### 4. Verify credentials work

```bash
python3 ups.py token
```

This fetches a token **and then proves it** with a Ground rate quote, which
creates no shipment and is never billed. Success looks like:

```
New cie token acquired, valid 14399s
Rating   OK - quoted 24.85 USD for 1 lb Ground to New York, NY
Shipping OK - the ship endpoint authorized the token (the probe carried no shipment, so nothing was created)

Credentials can rate and ship in cie.
```

Those two checks are the point. UPS will issue a perfectly valid token for an
app that has no API products attached, so a token on its own proves nothing —
every call still fails with `250002 Invalid Authentication Information`. And
Rating and Shipping are **separate** UPS products, so passing the rate quote
does not mean you can create a label; the shipping probe sends a request with
no shipment in it, which cannot produce a label but does prove authorization.

If either line fails, the credentials are right and the app configuration is
wrong: check that Shipping and Rating are listed on the app at
developer.ups.com. Newly created apps can also take a while to propagate.

`--no-verify` skips both probes if you only want to refresh the token.

---

## Use

```bash
# Free test label — validates the whole pipeline, no charges
python3 ups.py ship shipments/sample.json --format GIF

# Real label, ready for the Zebra
UPS_ENV=prod python3 ups.py ship shipments/sample.json --format ZPL

# A file containing a list creates a batch in one run
UPS_ENV=prod python3 ups.py ship shipments/batch-example.json
```

Production runs prompt for confirmation. Pass `--yes` to skip it once you
trust the setup.

### Shipping from the order form CSV

`ship-csv` takes the CSV exported from the order submission form, ships one
order per row, and writes the resulting tracking numbers back into the sheet's
**Tracking Number** column.

```bash
# Always look first — parses every row, creates nothing
python3 ups.py ship-csv orders.csv --dry-run

# Ship one row for real, to prove the flow end to end
UPS_ENV=prod python3 ups.py ship-csv orders.csv --limit 1

# Ship the rest — rows that already have a tracking number are skipped
UPS_ENV=prod python3 ups.py ship-csv orders.csv
```

- **Box sizes come from `boxes.json`** (your private copy of
  `boxes.example.json`), keyed by the code in the form's
  `How many Box ____?` columns. A box with no entry there stops the run rather
  than shipping at a guessed weight. Quantity 3 means three packages and three
  tracking numbers, comma-separated in the one cell.
- **Rows that already have a tracking number are skipped**, so re-running after
  a partial failure only ships what's left.
- The sheet is updated **after every order**, not at the end, so an interrupted
  run can't lose the tracking number for a label that's already been billed.
  The original is copied to `orders.csv.bak` first; `--out` writes elsewhere
  instead of in place.
- Unreadable addresses stop the run before anything is billed. Pass
  `--skip-errors` to ship the good rows and leave the bad ones for later.
- `Shipping Method` is honored when filled in; blank rows fall back to
  `--service` (default `03` Ground).

### Email notifications

UPS emails a ship confirmation when the label is created and a delivery
confirmation on arrival. Recipients are everyone in the order form's `Email`
column, the person who submitted that row from its `Email Address` column,
and whatever `UPS_NOTIFY_EMAIL` names in `.env`:

```
UPS_NOTIFY_EMAIL=alerts@example.com
```

Comma-separate for several. UPS allows five recipients per notification.
Leave it blank and only the customer is notified; if there's no customer
address either, the request goes out with no notification block at all.

Note that an email address alone in a shipment JSON does **not** trigger
anything — notifications are a separate part of the request. See `CLAUDE.md`.

### Output

| Path | What it is |
|---|---|
| `labels/<tracking>.zpl` | Raw printer data. Send straight to the Zebra. |
| `previews/<tracking>.png` | Rendered image, to eyeball before printing. |
| `shipments_log.csv` | Running log: date, tracking, service, destination, cost. |

---

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

Stdlib `unittest`, no extra dependencies, and **no network**: the base case
replaces `requests` outright and fails any test that reaches for it, so a
stub that drifts out of sync with UPS shows up as a failure rather than a
live call. Fixtures are synthetic — no real customer addresses in the repo.

The suite leans toward what costs money: billing a row twice, losing the
tracking number for a label that already exists, under-shipping an order, and
guessing at an address. It was checked by reintroducing each bug this code has
actually had and confirming a test fails.

---

## Printing

**Do not open the label in an image editor or PDF viewer before printing.**
It's already exactly 4x6 at the printer's native resolution; resampling it
degrades the barcode and can make it unscannable.

**You don't have to write the command yourself.** Every run that produces ZPL
or EPL ends by printing one you can paste, with your queue name filled in and
absolute paths, covering every label from that run:

```
Print 7 label(s) on Zebra_Technologies_ZTC_GC420d__EPL_:
  for f in "/…/labels/1Z….epl" "/…/labels/1Z….epl" …; do lpr -o raw -P "Zebra_Technologies_ZTC_GC420d__EPL_" "$f"; done
```

Connect the printer, paste, done. Each label is its own job, so a jam partway
through doesn't take the rest with it.

The queue name comes from `lpstat`. It is **not** `Zebra_GC420d` — CUPS mangles
the model into something like `Zebra_Technologies_ZTC_GC420d__EPL_`, and
guessing produces a misleading `lpr: No such file or directory`. If the queue
can't be identified, the command is still shown without `-P` and you're told to
run `lpstat -p`.

To print one label by hand:

```bash
lpstat -p                                        # list queue names
lpr -o raw -P <queue> labels/1Z999AA10123456784.epl
lpr -o raw labels/1Z999AA10123456784.epl         # if it's your default

# Network-attached Zebra, raw TCP
nc printer.local 9100 < labels/1Z999AA10123456784.zpl
```

### ZPL or EPL?

Your GC420d speaks both, and your current driver is the EPL variant.

- **Zero setup:** use `--format EPL`. Prints as-is.
- **Recommended:** install the plain ZDesigner GC420d (ZPL) driver and use
  `--format ZPL`. Better documented, better supported everywhere, and the only
  one with free online preview rendering.

---

## Reference

**Formats** — `ZPL` and `EPL` for thermal printing · `GIF` for visual
verification (renders to a preview with no extra tools) · `PDF` for a normal
laser/inkjet printer.

**Service codes** — `01` Next Day Air · `02` 2nd Day Air · `03` Ground ·
`12` 3 Day Select · `13` Next Day Air Saver · `14` Next Day Air Early ·
`59` 2nd Day Air A.M. · `65` Worldwide Saver

**Packaging codes** — `02` your own box (default) · `01` UPS Letter ·
`03` Tube · `04` PAK · `21` Express Box

---

## Warnings

- **Production labels are billed the moment they're created**, whether or not
  you print them. Test in `cie` first.
- `python3 ups.py void <tracking>` exists but is **unverified** — the void
  endpoint isn't in UPS's published spec, so the path is built from convention.
  Test it in `cie` before you need it. See `CLAUDE.md`.
- `.env`, `shipper.json`, and everything in `labels/` and `previews/` are
  gitignored. Keep it that way.
