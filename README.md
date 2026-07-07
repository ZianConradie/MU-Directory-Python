# MU Directory Bot

A Discord bot for maintaining a **Military Unit directory** for **WarEra**.

This bot keeps a channel updated with a clean MU directory message set. It pulls MU data from WarEra, calculates a **score** and **estimated potential damage** for each MU, renders a leaderboard, and provides owner-only controls to add or remove MUs from the directory.

This is **not an AI bot**. It does not use LLMs, prompts, agents, or model inference. It is a normal Discord bot that talks to the WarEra API, performs deterministic calculations, and edits Discord messages.

---

# What the bot does

The bot has one job:

1. **Store a list of WarEra MUs** in `data/mus.json`
2. **Fetch fresh data** for each saved MU from the WarEra API
3. **Calculate**:

   * a **score** based on MU ranking tiers
   * an **estimated potential damage** value based on member stats
4. **Render a Discord directory** showing:

   * MU name
   * MU link
   * HQ level
   * dorms level
   * commanders
   * a leaderboard with score and estimated damage
5. **Keep the channel clean** by editing/replacing old bot messages instead of spamming new ones
6. Let the owner **add/remove MUs** through Discord controls

---

# Main features

* **Automatic daily MU directory refresh**
* **Manual refresh** via `./update`
* **Owner-only add/remove MU controls**
* **Progress message** during updates
* **WarEra API fallback logic** for multiple payload formats / endpoints
* **Leaderboard generation**
* **Message chunking** for Discord’s 2000 character limit
* **State persistence** so the bot edits existing directory messages instead of reposting everything

---

# What gets shown in Discord

The bot renders a directory that looks roughly like this:

```text
INFO
- explanation of score
- explanation of potential damage
- update instructions

MU's
- MU 1
  - HQ
  - Dorms
  - Commanders
- MU 2
  - HQ
  - Dorms
  - Commanders
- ...

LEADERBOARD + STATS
1. MU Name - Score - Potential Damage
2. MU Name - Score - Potential Damage
3. ...
```

The output is split into multiple Discord messages if needed so it stays under Discord’s 2000-character message limit.

---

# How the bot works

## 1) Startup flow

When the bot logs in, `on_ready()` runs.

It does two things:

1. Calls `update_directory_messages()` immediately
2. Starts the daily update loop if it is not already running

So every time the bot starts, it immediately refreshes the directory.

---

## 2) Manual update flow

The bot listens for a single owner-only text command:

```text
./update
```

Conditions for this to work:

* `ENABLE_PREFIX_UPDATE` must be enabled
* the message must be in the configured MU directory channel
* the author must match `OWNER_USER_ID`

If all of that matches, the bot deletes the command message and sends a **control panel message** with buttons.

---

## 3) Daily update flow

The task loop `daily_directory_update()` runs every 24 hours.

The actual start time is controlled by:

```env
UPDATE_HOUR_UTC
```

Before the loop begins, `before_daily_directory_update()` calculates how many seconds remain until the next configured UTC hour and sleeps until then.

So if `UPDATE_HOUR_UTC=12`, the bot will wait until the next 12:00 UTC and then begin the 24-hour update cycle from there.

---

# Full update process

The heart of the bot is:

```python
update_directory_messages()
```

This function performs the full directory refresh.

## Step-by-step

### Step 1: Read saved MUs

The bot loads `data/mus.json`.

Each MU entry is expected to contain at least:

```json
{
  "id": "warera_mu_id",
  "name": "Optional saved name",
  "url": "https://app.warera.io/mu/..."
}
```

### Step 2: Resolve the target Discord channel

The bot tries to get the configured channel from cache first:

* `bot.get_channel(CHANNEL_ID)`

If that fails, it fetches it from Discord:

* `bot.fetch_channel(CHANNEL_ID)`

If the bot lacks access, it raises a runtime error.

### Step 3: Send a progress message

The bot posts:

```text
Updating... 0%
```

During the update it edits this message to show progress and ETA.

### Step 4: Fetch all saved MUs

The bot calls:

```python
fetch_saved_mus(saved_mus, report_progress)
```

This fetches every saved MU, updates its data, and reports progress after each MU finishes.

### Step 5: Render the directory messages

Once all MUs are processed, the bot turns the MU list into formatted directory text using:

* `render_directory_lines(mus)`
* `chunk_lines(lines)`

### Step 6: Edit or create Discord directory messages

The bot loads previous message IDs from `data/state.json`.

For each rendered chunk:

* if an old directory message exists at that position, it edits it
* if not, it sends a new message

If there are extra old directory messages left over from a previous longer render, it deletes them.

### Step 7: Save the new message IDs

The updated message IDs are written back to `data/state.json`.

### Step 8: Clean up bot clutter

The bot scans recent channel history and deletes its own old messages that are not part of the current directory set or progress message.

### Step 9: Delete the progress message

Finally, the progress message is deleted.

---

# How MU fetching works

Each MU is processed by:

```python
fetch_saved_mu(session, saved_mu)
```

## What it fetches

For each MU, the bot attempts to fetch:

1. **MU data**

   * endpoint: `mu.getById`

2. **HQ upgrade**

   * endpoint: `upgrade.getUpgradeByTypeAndEntity`
   * tries several payload shapes to match different possible API expectations

3. **Dormitories upgrade**

   * same endpoint, different upgrade type

After that, it extracts:

* MU name
* commander IDs
* member IDs
* rankings
* HQ level
* dorms level

Then it fetches:

4. **Commander usernames**
5. **All member profiles** for damage estimation

If the MU fetch fails, the bot logs the error and falls back to the saved MU data instead of crashing the whole update.

---

# WarEra API behavior

The bot supports two base URLs:

* `WARERA_GATEWAY_BASE_URL`
* `WARERA_DIRECT_BASE_URL`

By default:

* gateway: `https://gateway.warerastats.io/trpc`
* direct: `https://api2.warera.io/trpc`

The request helper tries multiple combinations:

1. gateway URL
2. direct URL

For each base URL it tries:

* GET with one payload style
* POST with one payload style
* GET with another payload style
* POST with another payload style

This is handled by:

* `trpc_request()`
* `trpc_request_any()`
* `trpc_get()`
* `trpc_post()`

The goal is resilience against small API differences.

---

# Score calculation

The MU score is calculated by:

```python
compute_mu_score(mu)
```

## How it works

The bot looks for:

```python
mu["rankings"]
```

Each ranking entry is expected to have a `tier`.

These tiers are mapped to numeric values using `TIER_SCORES`:

```python
TIER_SCORES = {
    "none": 0.0,
    "unranked": 0.0,
    "bronze": 0.0,
    "silver": 2.5,
    "gold": 5.0,
    "platinum": 7.5,
    "diamond": 10.0,
}
```

The MU score is:

> **the average of all usable ranking tier scores**

## Example

If an MU has rankings with tiers:

* Gold
* Platinum
* Silver

then the score is:

```text
(5.0 + 7.5 + 2.5) / 3 = 5.0
```

If no usable rankings are found, the score is `0.0`.

---

# Potential damage calculation

The estimated MU potential damage is calculated by:

```python
compute_mu_potential_damage(session, mu_id, member_ids)
```

## High-level idea

1. Fetch all MU member profiles
2. Run `compute_damage_potential(user, mu_id)` for each user
3. Sum the results

So the MU’s estimated potential damage is:

> **sum of the estimated damage output of all fetched members**

---

# How member damage is estimated

The heavy lifting happens in:

```python
compute_damage_potential(user, mu_id, inputs=None, config=None)
```

This is a deterministic formula based on the member’s skill values and a set of fixed assumptions.

## Default assumptions

The bot uses these defaults:

### Damage inputs

```python
DEFAULT_DAMAGE_INPUTS = {
    "pillMode": "all",
    "hpMode": "full",
    "gearMode": "purple",
    "timeframe": "window18h",
    "food": "fish",
    "battleBonusPct": 70,
}
```

### Purple gear config

```python
DEFAULT_DAMAGE_CONFIG = {
    "safetyMargin": 0.9,
    "purpleGear": {
        "weaponAttack": 120,
        "ammoPct": 40,
        "glovesPrecision": 23,
        "weaponCrit": 0,
        "helmetCritDmg": 85,
        "armorGear": 56,
        "bootsDodge": 23,
    },
}
```

## What those assumptions mean

The estimate assumes members are effectively fighting under a standard scenario:

* **all pilled**
* **purple gear**
* **18 hour damage window**
* **fish food**
* **70% battle bonus**
* a **0.9 safety margin** to reduce the final estimate a bit

This is **not a live battle simulator** and it is **not guaranteed to match real output exactly**. It is a standardized estimate so MUs can be compared in one consistent way.

---

# Damage formula overview

The function pulls various values from the user profile, including things like:

* health
* hunger
* precision
* critical chance
* critical damage
* armor
* dodge
* attack
* military rank bonus
* buffs/debuffs where applicable

It then estimates:

1. **base attack**
2. **critical hit damage**
3. **hit chance**
4. **average damage per hit**
5. **effective HP / fight pool**
6. **estimated number of hits**
7. **final estimated total damage**

At the end:

```python
return round(total_hits * avg_damage * config["safetyMargin"])
```

So the final number is essentially:

> **estimated total number of hits × average damage per hit × safety margin**

---

# Commander handling

The bot tries to determine commanders in two ways.

## Preferred path

If the MU data contains:

```python
roles.commanders
```

the bot uses those user IDs and fetches usernames through the API.

## Fallback path

If commander IDs are not available, the bot tries to infer commander names from MU data by checking fields such as:

* `commanders`
* `leaders`
* `managers`

and, if needed, scanning member-like objects for roles such as:

* commander
* leader
* owner
* admin

If nothing usable is found, the displayed commander list becomes:

```text
Unknown
```

---

# Add / remove MU controls

When the owner runs `./update`, the bot posts a control message with buttons.

## Buttons

### Add MU

Opens a modal asking for a WarEra MU link.

Accepted format:

```text
https://app.warera.io/mu/<mu_id>
```

The bot:

1. extracts the MU ID from the link
2. fetches that MU once to get its current data
3. saves it to `data/mus.json`
4. refreshes the directory

If the MU is already saved, it does not add a duplicate.

---

### Remove MU

Fetches the current saved MUs and opens an owner-only paginated remove view.

The owner can click an MU button to remove it. The bot then:

1. removes it from `data/mus.json`
2. refreshes the directory
3. deletes the control message

---

### Cancel

Closes out the control flow by refreshing the directory and deleting the control message.

---

# Files used by the bot

## `data/mus.json`

Stores the list of tracked MUs.

Example:

```json
[
  {
    "id": "69cf764cf18f2f6578e948e8",
    "name": "Example MU",
    "url": "https://app.warera.io/mu/69cf764cf18f2f6578e948e8"
  }
]
```

## `data/state.json`

Stores the Discord message IDs of the currently active directory messages so the bot can edit them later.

Example:

```json
{
  "directory_message_ids": [
    123456789012345678,
    123456789012345679
  ]
}
```

---

# Environment variables

The bot uses the following environment variables.

## Required

### `DISCORD_TOKEN`

The bot token.

Example:

```env
DISCORD_TOKEN=your_bot_token_here
```

---

## Optional

### `DISCORD_CHANNEL_ID`

The target channel for the MU directory.

If not set, the bot falls back to the hardcoded `DEFAULT_CHANNEL_ID`.

Example:

```env
DISCORD_CHANNEL_ID=123456789012345678
```

### `UPDATE_HOUR_UTC`

The UTC hour when the daily update cycle should begin.

Example:

```env
UPDATE_HOUR_UTC=12
```

### `WARERA_API_BASE_URL`

Override for the WarEra gateway TRPC base URL.

Default:

```env
https://gateway.warerastats.io/trpc
```

### `WARERA_API_TOKEN`

Optional token sent as both:

* `Authorization: Bearer <token>`
* `X-API-Key: <token>`

If the API does not require it for your setup, it can be left unset.

### `ENABLE_PREFIX_UPDATE`

Controls whether the owner can trigger the `./update` message command.

Values:

* `true`
* `false`

Example:

```env
ENABLE_PREFIX_UPDATE=true
```

---

# Permissions the bot needs

In the target channel, the bot should have at least:

* **View Channel**
* **Send Messages**
* **Read Message History**
* **Manage Messages** or the ability to delete its own messages where applicable
* **Embed Links** if you want Discord to format links nicely, though the bot suppresses embeds on directory messages

If the bot cannot access the channel, the update will fail.

---

# Message editing behavior

The bot does **not** just post a fresh directory every time.

Instead it tries to keep the directory stable by editing existing messages.

## How it decides what to do

* It loads old message IDs from `data/state.json`
* It renders the new directory into chunks
* It matches chunk 1 to old message 1, chunk 2 to old message 2, and so on
* If a matching old message exists, it edits it
* If not, it sends a new one
* If there are old leftover directory messages from a previous longer render, it deletes them

This keeps the channel cleaner and avoids endless duplicate directory posts.

---

# Progress updates

During a refresh, the bot posts a progress message and updates it as MUs finish processing.

Example:

```text
Updating... 45% | ETA: 12s
```

ETA is estimated from the average completion time of already-finished MUs.

---

# Error handling philosophy

The bot is designed to be fairly stubborn rather than fragile.

## If one MU fetch fails

It does **not** crash the entire update.
Instead it logs the error and falls back to the saved MU data for that MU.

## If a user fetch fails during damage estimation

That specific user is skipped and logged.

## If an old Discord message no longer exists

The bot sends a fresh replacement message instead of failing.

## If cleanup fails

The bot logs the error and moves on.

---

# Project structure

A typical structure would look like this:

```text
project/
├─ data/
│  ├─ mus.json
│  └─ state.json
├─ src/
│  └─ main.py
├─ .env
└─ README.md
```

Your exact layout may differ, but the code assumes the script lives one level below the project root and that the `data/` folder is at the root.

---

# Setup

## 1) Install dependencies

Example:

```bash
pip install discord.py aiohttp python-dotenv
```

## 2) Create a `.env`

Example:

```env
DISCORD_TOKEN=your_bot_token_here
DISCORD_CHANNEL_ID=123456789012345678
UPDATE_HOUR_UTC=12
ENABLE_PREFIX_UPDATE=true
WARERA_API_BASE_URL=https://gateway.warerastats.io/trpc
WARERA_API_TOKEN=
```

## 3) Create `data/mus.json`

Example:

```json
[]
```

## 4) Start the bot

Run your bot file normally with Python.

Example:

```bash
python src/main.py
```

On startup it will immediately attempt to refresh the MU directory.

---

# Limitations and caveats

## 1) Potential damage is an estimate

The damage number is a **standardized estimate**, not a promise of real battle output.

It depends on:

* the available WarEra user data
* the assumptions baked into `DEFAULT_DAMAGE_INPUTS`
* the assumptions baked into `DEFAULT_DAMAGE_CONFIG`

If you change those assumptions, the leaderboard values can change a lot.

## 2) Large MUs are heavier to update

Potential damage requires fetching member profiles. If an MU has a lot of members, that MU is more expensive to process.

## 3) The bot depends on WarEra API structure

If the WarEra API changes endpoint names, payload formats, or response shapes, the bot may need updates.

## 4) Commander detection is best-effort

If the API response does not clearly expose commander IDs or commander-like fields, the bot may show `Unknown`.

---

# Summary

This bot is a **WarEra MU directory maintainer** for Discord.

It:

* tracks saved MUs
* fetches fresh MU data
* calculates a score from ranking tiers
* estimates MU damage from member profiles
* renders a directory and leaderboard in Discord
* lets the owner add/remove MUs
* updates existing messages instead of spamming new ones
