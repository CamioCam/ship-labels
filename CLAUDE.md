# UPS Labels — project context

Generates UPS shipping labels as native 4x6 thermal files for a Zebra printer.
Single-file CLI (`ups.py`), stdlib + `requests`.

## Hardware target

**Zebra GC420d**, 203 dpi (8 dots/mm), max print width 4.09". Supports both
ZPL II and EPL2 — the installed driver is currently the `(EPL)` variant, so
`--format EPL` prints with no printer changes, while `--format ZPL` requires
installing the plain ZDesigner GC420d driver.

The CUPS queue is `Zebra_Technologies_ZTC_GC420d__EPL_` (also the system
default), *not* `Zebra_GC420d`. Print with `lpr -o raw` and an absolute path.
Note that EPL labels get no preview PNG — Labelary renders ZPL only and
returns 404 for EPL, which `render_preview()` reports and ignores.

**The label needs no cropping.** UPS renders it at exactly 4x6 in the printer's
own language. If someone suggests screenshotting, rasterizing, or resizing a
label, that is wrong — it resamples the barcode and can make it unscannable.
Send the bytes to the printer untouched.

## Return address prints one line only

UPS renders exactly one street line in the label's return-address block, and
it is the first `AddressLine`. Verified against `cie`: `["100 Warehouse Way",
"Unit 7"]` printed only `100 WAREHOUSE WAY`, and reversing the two printed only
`UNIT 7` with no street at all. This is a label-layout limit, not a bug —
both lines are sent and UPS keeps them; they just never reach the paper.

So put the unit on the same line in `shipper.json`
(`"100 Warehouse Way Unit 7"`). `warn_unprinted_shipper_lines()` says so before
shipping if anyone splits it again.

**`ShipTo` is not affected** — recipients do get a second line, so a
recipient's suite or unit prints on its own line above the street.

## API facts (verified Aug 2026)

| | |
|---|---|
| OAuth token | `POST {host}/security/v1/oauth/token`, `grant_type=client_credentials`, HTTP Basic with `clientId:clientSecret` |
| Ship | `POST {host}/api/shipments/v2409/ship` |
| Sandbox host | `https://wwwcie.ups.com` |
| Production host | `https://onlinetools.ups.com` |

Label format is set by `ShipmentRequest.LabelSpecification.LabelImageFormat.Code`
(`ZPL`, `EPL`, `GIF`, `PNG`, `PDF`) with `LabelStockSize` of 4x6. Note that
`LabelSpecification` is a **sibling** of `Shipment`, not nested inside it.

The label comes back base64-encoded at
`ShipmentResponse.ShipmentResults.PackageResults[].ShippingLabel.GraphicImage`.
`PackageResults` is a bare object rather than a list when there's one package —
the code normalizes this.

## Credential checks

**A successful OAuth token proves nothing.** UPS issues valid tokens on both
hosts for apps that have no API products attached; every business call then
fails with `250002 Invalid Authentication Information` — Shipping, Rating, and
Tracking alike, in both `cie` and `prod`, regardless of account number. That is
why `cmd_token()` follows the token with a Rating call, which creates no
shipment and is never billed. Keep that second step; without it the command
reports success on credentials that cannot ship.

Seen `250002` everywhere? It is app configuration on developer.ups.com (no
products attached, or a new app that hasn't propagated), not a bug here.

## Email notifications

An address in the `ShipTo` block is contact data only — UPS sends nothing for
it. Notifications require `ShipmentServiceOptions.Notification` with Quantum
View codes: `6` ship confirmation, `8` delivery, `7` exception (unused so far).

Recipients are the customer contacts from the `Email` column (that cell often
holds two), the row's submitter from `Email Address`, and the standing
`UPS_NOTIFY_EMAIL` list in `.env`. UPS caps this at five, so the standing list
is assembled first and can't be evicted by a row with many contacts; the
submitter is last and is the first to be dropped if a row somehow overflows.

**`FromName` is counted per shipment, not per notification block.** Repeating
it across both blocks fails with `[120661]`, so `notification_blocks()` puts
the sender fields on the first block only. Verified against `cie`.

## Known unknowns

- **Void endpoint is unverified.** `cmd_void()` uses
  `DELETE /api/shipments/v1/void/cancel/{id}`, built from convention, not from
  UPS's published OpenAPI spec (which omits it). Test against `cie` before
  relying on it. A 404 means the path needs correcting.
- **Labelary preview.** ZPL/EPL have no local renderer, so previews for those
  formats call `api.labelary.com`. Optional — failure is non-fatal. GIF/PNG/PDF
  previews render locally via Pillow / pdftoppm.

## Order form CSV

`ship-csv` reads the CSV exported from the order submission form. Two traps in
that sheet, both handled in `map_columns()` — don't "simplify" them away:

- **`Email` is the customer; `Email Address` is the form submitter.** Two
  different columns, and the submitter varies per row. The customer goes on
  the label and is notified; the submitter is notified but never appears on
  the label. Match both headers exactly — `email` is a prefix of
  `email address`, so prefix matching silently swaps them.
- **Headers repeat.** There are two `How many Box 1110A` columns, so
  `csv.DictReader` would silently drop one. The code reads by index and sums
  every matching column.

Box weights and dimensions live in `boxes.json`, keyed by box code. That file
is gitignored private config; `boxes.example.json` is the tracked template. Only
one box code is filled in so far — the other eight the
form offers still need real numbers. An unlisted box raises rather than
shipping at a guessed weight, which would under-bill and invite a UPS
adjustment. Each box is its own package, so quantity 3 gets three tracking
numbers.

Addresses arrive as one free-text field in two shapes (comma-run and embedded
newline). `parse_address()` handles both and **raises instead of guessing** —
a wrong city or state turns into an address-correction surcharge.

## Tests

`python3 -m unittest discover -s tests -t .` — stdlib only, and CI runs it on
every PR.

`UpsTestCase` patches `LABELS`, `PREVIEWS`, `LOG`, and `BOXES_FILE` to a temp
directory and replaces `requests.post/get/delete` with something that raises.
**Keep that.** A test that reaches the network against `prod` would bill a
real label. If a test needs a response, stub it explicitly with `mock.patch`.

Fixtures in `tests/fixtures/orders.csv` are synthetic but mirror the real
form's shape — duplicate `1110A` headers, an embedded-newline address, a row
that is already shipped, a box missing from `boxes.json`. Keep real customer
data out of the repo.

When fixing a bug here, add the test that fails without the fix. The suite was
validated by mutation — reintroducing each past bug and confirming a failure.

## No real data in tracked files

Addresses, names, emails, and box specs in source, tests, docs, and **commit
messages** must be fictional. Use the conventions already in
`tests/fixtures/orders.csv` — Ada Lovelace / Bletchley Fitness / `example.com` /
`555-01xx` — and invented streets like `100 Warehouse Way`.

This is not hypothetical tidiness: real customer addresses reached `ups.py`'s
`parse_address()` docstring and the test fixtures by being copied from live
orders while debugging, and had to be scrubbed before any of it could be
published. Real data belongs in the gitignored files (`.env`, `shipper.json`,
`boxes.json`, `shipments_log.csv`, `labels/`) and nowhere else.

Check before committing by grepping the tree for your own real strings — the
ship-from street, city and ZIP, the UPS account number, and any customer name
or address you were debugging with. Do not write that list into a tracked file:
an enumeration of the things you scrubbed leaks them just as effectively as the
originals did. The real values live in the gitignored config; read them from
there.

## Conventions

- Secrets live in `.env` (gitignored). Real environment variables take
  precedence, so `UPS_ENV=prod python3 ups.py ...` overrides for one command.
- `shipper.json` is gitignored (contains a real address); the tracked template
  is `shipper.example.json`.
- Generated `labels/`, `previews/`, `shipments_log.csv`, and `.token.json` are
  gitignored.
- Every UPS failure should surface UPS's own error code and message via
  `explain_error()`, never a raw JSON dump or a bare status code.

## Safety

Production labels are **billed the moment they're created**. `cmd_ship` prompts
for confirmation when `UPS_ENV=prod` unless `--yes` is passed. Keep that
prompt. Default `UPS_ENV` to `cie` everywhere.

## Likely next work

- Verify and fix the void endpoint.
- Address Validation API before shipping, so bad addresses fail fast instead of
  becoming UPS address-correction surcharges.
- Rating API for rate shopping across services before committing to one.
- Actually sending to the printer. `print_instructions()` deliberately only
  *shows* the `lpr -o raw` command after a run — physical output stays a
  manual step. Adding `--print` would change that; ask first.
- Fill in the remaining box types in `boxes.json`.
