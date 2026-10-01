# Notion Trading Journal Setup Guide 📓⚡

This guide provides step-by-step instructions for integrating Notion with the **Autonomous Trading Desk (ATD)**.

The Notion integration serves as an automated post-trade ledger and trade journal. Positions deployed via ATD can be logged and reconciled automatically against the real Binance Futures ledger (`/fapi/v1/userTrades` and `/fapi/v2/positionRisk`).

> [!NOTE]
> **Fail-Open Integration Invariant:** The Notion integration is strictly **fail-open**. If Notion credentials are not configured, or if the Notion API times out or returns an error, live trading execution and risk gates will **never be blocked**.

---

## 📋 Database Schema Specification

Create an inline or full-page database named **`Trading Journal - Futures`** with the following properties:

| Property Name | Notion Property Type | Options / Format | Required / Optional | Description |
| :--- | :--- | :--- | :--- | :--- |
| **Name** | `title` | Text (e.g. `BTCUSDT LONG 2026-10-01`) | **Required** | Primary identifier for the trade record. |
| **Symbol** | `select` or `text` | Upper-case ticker (e.g. `BTCUSDT`, `ETHUSDT`) | **Required** | Contract traded on Binance Futures. Used by reconciliation script. |
| **Direction** | `select` | `LONG`, `SHORT` | **Required** | Position direction. |
| **Strategy** | `select` | `Mean Reversion`, `Momentum Trend`, `Support Pullback`, `Breakout & Retest`, `Stat-Arb`, `Funding Arbitrage`, `YOLO Moonshot` | Optional | Quantitative strategy rationale. |
| **Status** | `select` or `status` | `PENDING`, `OPEN`, `TP HIT`, `SL HIT`, `CLOSED` | **Required** | Monitored by `sync_notion_journal.py`. When an `OPEN` position closes on Binance, it automatically transitions to `TP HIT` or `SL HIT`. |
| **Entry Price** | `number` | Currency / 4-6 decimal places | **Required** | Execution fill price. |
| **Stop Loss** | `number` | Currency / 4-6 decimal places | **Required** | Hard algorithmic Stop Loss level. |
| **TP1** | `number` | Currency / 4-6 decimal places | Optional | First take-profit target (+1.8R, fee break-even). |
| **TP2** | `number` | Currency / 4-6 decimal places | Optional | Structural take-profit target (+4.0R). |
| **Leverage** | `number` | Integer (e.g. `3`, `5`, `15`) | Optional | Effective leverage applied. |
| **Realized PnL**| `number` | Currency (USDT), colored by value | **Required** | Populated automatically by `sync_notion_journal.py` from actual Binance trade fills. |
| **Entorno** | `select` | `REAL`, `TESTNET` | Optional | Operating environment identifier (`REAL` = Mainnet, `TESTNET` = Sandbox). |
| **Date** | `date` | Timestamp (UTC) | Optional | Entry execution timestamp. |
| **Notes** | `rich_text` | Text | Optional | Technical thesis, catalyst, or evaluation dossier summary. |

> [!TIP]
> `scripts/sync_notion_journal.py` uses flexible property resolution:
> - **Status property:** Matches any column named `Status` or `Estado` of type `select` or `status`.
> - **PnL property:** Matches any column named `Realized PnL`, `PnL`, `Profit`, or `Ganancia` of type `number`.
> - **Environment property:** Updates `Entorno` with `REAL` or `TESTNET` if present.

---

## 🛠️ Step-by-Step Setup Instructions

### Step 1: Create a Notion Integration & Obtain API Key

1. Navigate to [Notion Developers Integrations](https://www.notion.so/profile/integrations).
2. Click **"+ New integration"**.
3. Fill in the details:
   - **Name:** `Autonomous Trading Desk`
   - **Associated workspace:** Select your workspace
   - **Type:** `Internal`
4. Set Capabilities:
   - ✅ Read content
   - ✅ Update content
   - ✅ Insert content
5. Click **Save** and copy the **Internal Integration Secret** (starts with `secret_` or `ntn_`).

---

### Step 2: Create the Database in Your Notion Workspace

1. Open Notion and create a new page named **`Trading Journal - Futures`**.
2. Type `/database inline` and select **Database - Inline** (or full-page).
3. Configure the properties matching the table above.
4. Add the initial options to `Status`:
   - `PENDING`
   - `OPEN`
   - `TP HIT`
   - `SL HIT`
   - `CLOSED`
5. Add the initial options to `Direction`:
   - `LONG`
   - `SHORT`
6. Add the initial options to `Entorno`:
   - `REAL`
   - `TESTNET`

---

### Step 3: Connect Integration to the Database

By default, Notion integrations cannot access any pages until explicitly granted access:

1. Open your `Trading Journal - Futures` page in Notion.
2. Click the **`···`** (Options) icon in the top right corner.
3. Scroll down and click **"Connect to"** (or **"Add connections"**).
4. Search for and select your integration: **`Autonomous Trading Desk`**.
5. Confirm access.

---

### Step 4: Extract Database ID

1. In Notion, click **Share** -> **Copy link** on your database page (or copy the URL from your browser).
2. The URL looks like:
   ```
   https://www.notion.so/workspace/a8b9c1d2e3f4a5b6c7d8e9f0a1b2c3d4?v=...
   ```
3. The Database ID is the **32-character hexadecimal string** between the slash and the question mark (`?`):
   - Example: `a8b9c1d2e3f4a5b6c7d8e9f0a1b2c3d4`
   - Or with dashes: `a8b9c1d2-e3f4-a5b6-c7d8-e9f0a1b2c3d4` (both formats are accepted).

---

### Step 5: Configure Credentials

You can provide your credentials in either `.env` or `config/user_context.json`.

#### Option A: In `.env` (Recommended for Local Secrets)
Add your Notion API key and database ID to your local `.env`:
```ini
# Notion Trading Journal Configuration
NOTION_API_KEY=secret_your_notion_api_token_here
NOTION_DATABASE_ID=a8b9c1d2e3f4a5b6c7d8e9f0a1b2c3d4
```

#### Option B: In `config/user_context.json`
If you prefer organizing non-secret identifiers in `config/user_context.json`:
```json
{
  "notion": {
    "collection_id": "a8b9c1d2e3f4a5b6c7d8e9f0a1b2c3d4",
    "database_id": "a8b9c1d2e3f4a5b6c7d8e9f0a1b2c3d4"
  }
}
```
*(Note: `NOTION_API_KEY` must still be placed in `.env` or exported as an environment variable to prevent secret leaks.)*

---

### Step 6: Test and Reconcile

Run the reconciliation script in `--dry-run` mode to test connectivity without modifying your database:

```bash
# Test connection with dry-run
python3 scripts/sync_notion_journal.py --dry-run

# Test specifically for TESTNET sandbox
python3 scripts/sync_notion_journal.py --dry-run --env testnet

# Perform live reconciliation against Binance
python3 scripts/sync_notion_journal.py
```

Expected output:
```text
=================================================================
🔄 RECONCILIACIÓN NOTION JOURNAL vs BINANCE FUTURES LEDGER
Target Env: TESTNET | Notion DB: a8b9c1d2...
=================================================================
• Binance Ledger: 0 posiciones activas | 4 símbolos con historial.
• Propiedades detectadas en Notion: Status='Status', PnL='Realized PnL'
• Filas encontradas en Notion: 4
=================================================================
🎯 RECONCILIACIÓN COMPLETADA: 0 posiciones reconciliadas.
=================================================================
```

---

## 🔄 Automated Reconciliation Logic

`scripts/sync_notion_journal.py` executes the following deterministic reconciliation workflow:

1. **Query Binance Ledger:** Fetches active positions via `/fapi/v2/positionRisk` and recent trades via `/fapi/v1/userTrades`.
2. **Query Notion Database:** Scans up to 100 recent rows where `Status` is marked `OPEN`, `ACTIVE`, `PENDING`, or `IN PROGRESS`.
3. **Discrepancy Detection:** If a row is marked `OPEN` in Notion, but the symbol has `positionAmt == 0` on Binance:
   - Sums realized PnL from exit trades in `userTrades`.
   - Transitions `Status` to **`TP HIT`** if realized PnL > 0.
   - Transitions `Status` to **`SL HIT`** if realized PnL < 0.
   - Transitions `Status` to **`CLOSED`** if realized PnL == 0.
   - Updates `Realized PnL` with the exact numerical dollar amount.
   - Stamps `Entorno` with `REAL` or `TESTNET`.
