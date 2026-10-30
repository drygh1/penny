# Penny

A personal Rocket Money replacement that runs on your own computer. It tracks:

- **Subscriptions and bills.** Penny finds recurring charges automatically and shows each one's monthly and yearly cost, the next charge date, price increases, and subscriptions that stopped charging. Bills you can't cancel (rent, utilities) are listed apart from subscriptions.
- **Spending and income.** Charts by month or year, where your money went by category, where it came from by source, and your top merchants.
- **Net worth.** Every account balance, split into assets and debts, plus a history chart.

Bank data comes from [SimpleFIN Bridge](https://bridge.simplefin.org), which costs about $15 a year.
Everything is stored on your own Mac. Nothing is sent anywhere except the requests to SimpleFIN for your bank data.

## Install

Penny runs on a Mac. You'll need Terminal for the first setup only.

1. Install [uv](https://docs.astral.sh/uv/), which sets up Python for Penny. In Terminal:
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
   Then close Terminal and open it again.
2. Download Penny and put the `penny` folder somewhere it can stay, such as your home folder. With git:
   ```bash
   git clone https://github.com/drygh1/penny.git ~/penny
   ```
3. Set it up and make the app:
   ```bash
   cd ~/penny
   ./run.sh               # installs everything, then opens Penny; press Ctrl+C to stop it
   scripts/make_app.sh    # adds Penny to ~/Applications
   ```

From then on, open Penny like any other app. If you move the `penny` folder, run `scripts/make_app.sh` again.

To update to a newer version later: `cd ~/penny && git pull`, then stop Penny and open it again.

## Run it

Open **Penny** from Spotlight (⌘ Space, type "Penny"), Launchpad, or the Dock. It starts
Penny in the background if needed and opens http://127.0.0.1:8000 in your browser.
To keep it in your Dock, drag `~/Applications/Penny.app` there.

Penny keeps running in the background after you close the tab, so it opens instantly next
time. To shut it down, click **Stop Penny** in the top bar. The server log is in `data/penny.log`.

Penny uses port 8000. If something else on your Mac already uses it, start Penny with another
port: `PENNY_PORT=8001 scripts/launch.sh`.
To run Penny in a terminal instead (handy while changing code), use `./run.sh`.

You can click **Try with demo data** to explore before connecting a real bank.

## First week: teach it your banks

Penny guesses categories from keywords, and the starting keywords were written around one
person's banks. Expect to fix some at first. Every fix sticks for future transactions:

- **Transactions page:** change one purchase's category. Click **Use for all** under it to make that the merchant's default.
- **Merchants page:** set a merchant's default category, or **Combine** names that are really the same merchant (your bank might call your employer "Acme Corp" one month and "Acme Payroll" the next).
- **Subscriptions page:** hide anything that isn't really recurring, and move items between Subscriptions and Bills.
- **Net worth page:** **Rename** accounts to something shorter than the bank's name.

## Connect your bank

1. Sign up at https://bridge.simplefin.org and link your bank accounts there.
2. Create a **Setup Token** (New Connection → copy the token).
3. In Penny, go to **Settings**, paste the token, and click **Connect**.

The first sync pulls up to a year of history. Click **Sync** in the top bar to update.
SimpleFIN Bridge allows about 24 requests per day, so syncing a few times a day is plenty.

## How it works

| File | What it does |
|---|---|
| `app/main.py` | JSON API (`/api/...`) and serves the web UI |
| `app/simplefin.py` | SimpleFIN client: claims the setup token and fetches accounts |
| `app/sync.py` | Saves accounts, transactions and daily balance snapshots |
| `app/categorize.py` | Cleans merchant names and assigns categories by keyword |
| `app/subscriptions.py` | Detects recurring charges from cadence and amount |
| `app/demo.py` | Generates a year of fake data |
| `static/` | Web UI (plain HTML/JS, Chart.js) |

## Importing older history

SimpleFIN only reaches back about 90 days. For older transactions:

1. On your bank's website, open the account's transactions and look for **Download** or **Export**. Choose **CSV** and the longest date range.
2. In Penny, go to **Settings → Import older transactions**, pick the matching account, and choose the file.
3. Check the preview. Spending should show as negative. If the file lists purchases as positive (common for credit cards), tick the box to flip it.

**Duplicates:** transactions Penny already has from SimpleFIN are skipped (same account, same amount, dates within 3 days). Importing the same file twice changes nothing.

**Net worth:** the net worth chart extends back through imported history, rebuilt by tracing transactions backward from the account's balance. Gaps in the imported history make those older points less accurate. Brokerages and crypto wallets usually report a balance but no transactions; for those, Penny works back from deposits it sees leaving your other accounts (Fidelity, Coinbase, Robinhood, Vanguard, Schwab, Betterment, Wealthfront, Acorns and E*TRADE are recognized). It can't see past market gains or losses, so those older points are estimates.

**Bank of America credit card PDFs:** Bank of America only offers recent activity as CSV. You can select its monthly statements (`eStmt_*.pdf`) and year-end summaries (`YearEndSummary_*.pdf`) instead, several at once. Penny checks each monthly statement's transactions against its printed balances and flags any that don't add up. Year-end summaries leave out card payments, so they add to spending history but aren't used for past balances. Import them separately from monthly statements.

**Accounts SimpleFIN can't reach:** choose "New account" and enter today's balance.

**Undo:** remove an import with the **Remove** button under "Imported so far".

## Your data is kept safe

- **Everything stays.** Transactions remain in Penny after they're older than SimpleFIN's 90-day window, and after you disconnect.
- **Originals are kept.** Every raw SimpleFIN download and every CSV or PDF you import is stored (compressed) in the database, exactly as received.
- **Automatic backups.** Penny copies its database once a day (when it starts, syncs or imports) and before anything destructive: deleting data, removing an import, deleting a category, or restoring. It keeps 14 daily and 12 monthly copies, plus the last 20 safety copies, in `~/Library/Application Support/Penny/backups`. That folder is outside the project, readable only by you, never synced to a cloud service, and included in Time Machine if you use it.
- **Restore** any backup from **Settings → Your data → Restore a backup**. The current state is backed up first, so a restore can be undone.

## Merchant rules and pictures

The **Merchants** page lists everyone you've paid. For each merchant you can:

- **Pick a category.** This creates a rule that applies to all past and future transactions from that merchant. Choose "Automatic" to remove the rule.
- **Combine merchants.** Click **Combine…** and tick the other names that are really the same merchant. Their transactions count as this one everywhere, including new ones. The merchant then lists what it includes, each with a **Separate** link to undo.
- **Add a picture.** Click the circle or "Add picture" and choose a PNG or JPEG. The picture is cropped to a square, shrunk, and stored in the database. It shows up everywhere that merchant appears.
- **Treat some descriptions differently.** Click **Descriptions** to see each different description the merchant uses, in full. For example, a payment app might use "...TYPE: CASHOUT..." and "...TYPE: PAYMENT...". Give any of them its own category, or add a rule like "description contains *cashout*". Numbers are ignored when matching, so trace and store numbers don't matter. A description rule beats the merchant's category, and the more specific rule wins.

Changing a category on the **Transactions** page changes just that purchase, for a merchant that's usually one thing but sometimes another. An edited purchase keeps its category through syncs and rule changes. Under it, **Reset** sends it back to the merchant's default, and **Use for all** makes its category the default (or updates the description rule that set it).

## Subscriptions and bills

A merchant counts as recurring when it charges on a regular schedule (weekly to yearly) for a steady amount, after 2 to 3 charges. When one merchant bills for several things (an app store billing for several apps), its charges are grouped by price and each group is checked on its own.

Recurring charges in Housing or Utilities & Bills are listed as **Bills**; everything else is a **Subscription**. Move any of them with **Move to bills** / **Move to subscriptions**, change the category from the dropdown, or mark it **Not recurring** to hide it.

## Dates

Transactions are dated the day you made the purchase, not the day the bank posted it (which can be a few days later). The date comes from SimpleFIN, or from the "Date 10/24/26" some banks write into the description.

## Categories

Add or delete categories under **Settings → Categories**.

- When you delete a category, you choose where its transactions go. Merchant rules pointing to it move too. Future transactions that would have landed in it go to the new category.
- Income, Transfers and Other are built in and can't be deleted. Spending totals leave out Income, Transfers, Credit Card Payments and Investments. Paying off a card isn't spending, because the purchases on it already counted. Money put into Fidelity, Robinhood, Coinbase and similar is still yours, so it counts toward what you saved. Other catches anything unmatched.
- **Credit Card Payments** are recognized from a card issuer's name plus a payment word in checking ("CAPITAL ONE ... ONLINE PMT", "CITI CARD E-PAYMENT"), payment wording on the card itself, and matching amounts moving between your accounts.
- Every category gets its own color, from 20 colors ordered so neighbors stay distinct for colorblind readers too. Deleting a category frees its color for the next one you add; only past 20 categories does one show in gray.
- A new category starts empty. Use the **Merchants** page to assign merchants to it.

## Toward an iPhone app

The UI only talks to the JSON API, so an iPhone app (SwiftUI) can call the same endpoints.
The main work to get there:

1. Host the server somewhere your phone can reach. Options are a home server reached over [Tailscale](https://tailscale.com), or a small cloud VM.
2. Add authentication before exposing it anywhere. Right now the server trusts anyone who can reach it, which is fine only while it listens on localhost.
3. Build the SwiftUI screens against `/api/accounts`, `/api/subscriptions`, `/api/spending/*` and `/api/transactions`.

## Tests

```bash
uv pip install --python .venv -r requirements-dev.txt   # once
.venv/bin/pytest
```

Dependencies are pinned to exact versions in `requirements.txt`, so every install matches what was tested.
