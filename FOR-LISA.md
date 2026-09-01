# Making UPS labels — start here

This tool turns the order sheet into UPS shipping labels and prints them on the
Zebra label printer. It writes the tracking numbers back into your copy of the
sheet as it goes.

You type four commands, total. Nothing here requires knowing anything about
programming.

---

## One-time setup

**1. Install it.** Open Terminal (press ⌘-Space, type `Terminal`, press Return),
then paste this line and press Return:

```bash
curl -fsSL https://raw.githubusercontent.com/CamioCam/ship-labels/main/install.sh | bash
```

It will tell you what it did. If it says to close the window and open a new
one, do that.

**2. Get your settings from Carter.** He will send you three files:

| File | What it is |
|---|---|
| `.env` | Your UPS login and the address to copy on notifications |
| `shipper.json` | The return address that prints on every label |
| `boxes.json` | The weight and size of each box type |

Put all three into the `ship-labels` folder in your home folder, replacing the
files already there. In Finder: **Go → Home**, then open **ship-labels**.

**3. Check it worked.** In Terminal:

```bash
ups token
```

You want to see `Rating OK` and `Shipping OK`. This only checks the
connection — it never creates a label and is never billed. If it complains,
send Carter what it printed.

---

## Making labels

**1. Get the orders as a file.** In the order sheet: **File → Download →
Comma Separated Values (.csv)**. It lands in your Downloads folder.

**2. Look before you ship.** Type this, but *don't press Return yet*:

```bash
ups ship-csv 
```

Now drag the downloaded `.csv` file from Finder into the Terminal window — the
path appears by itself. Then type ` --dry-run` on the end and press Return.

This reads every row and shows what it *would* ship. It creates nothing and
costs nothing. Check the names and addresses look right.

**3. Ship for real.** Same line, but `--format EPL` instead of `--dry-run`:

```bash
ups ship-csv <drag the file here> --format EPL
```

It will ask you to type `yes` before doing anything that costs money. Once you
do, each row gets a tracking number, written straight into your copy of the CSV.

**4. Print.** When it finishes it prints a long command starting with `for f in`.
Turn the label printer on, then copy that whole line, paste it into Terminal,
and press Return. All the labels print.

**5. Put the tracking numbers back in the shared sheet.** Open your CSV, copy the
**Tracking Number** column, and paste it into the shared order sheet. The tool
writes to your downloaded copy, not to the sheet everyone sees.

---

## Things worth knowing

**Two modes.** Your `.env` file has a line reading either `UPS_ENV=cie` or
`UPS_ENV=prod`.

- `cie` is practice. Labels are free and fake — they say SAMPLE and have no real
  barcode. Use this while you are learning.
- `prod` is real. **Every label is charged to the UPS account the moment it is
  created**, whether or not you print it.

Carter switches you to `prod` when you're ready.

**Re-running is safe.** A row that already has a tracking number is skipped, so
if something goes wrong halfway you can just run the same command again. It will
only ship the rows that still need it.

**A row can stop the whole run.** If an address can't be read, or a box type
isn't in `boxes.json`, it stops rather than guessing — a wrong address means UPS
charges a correction fee. Send Carter the message and he'll fix the sheet or add
the box.

**Getting a newer version.** Run the install line from step 1 again. It updates
the program and leaves your three settings files alone.

---

## If something goes wrong

| It says | What to do |
|---|---|
| `command not found: ups` | Close the Terminal window, open a new one, try again |
| Anything with `250002` | A UPS login problem — send Carter the message |
| `no ZIP code at the end of…` | That row's address is malformed in the sheet |
| `has no entry in boxes.json` | That box type needs adding — ask Carter |
| `lpr: No such file or directory` | You typed the print command instead of pasting it |

Nothing in this list has cost money. If a command was interrupted partway, the
labels it already made are recorded in your CSV — send that to Carter rather
than re-running blind.
