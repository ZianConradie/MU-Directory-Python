import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from aiohttp import ClientSession
from discord.ext import tasks
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
MUS_FILE = DATA_DIR / "mus.json"
STATE_FILE = DATA_DIR / "state.json"

DEFAULT_CHANNEL_ID = 1440423053099012206
DISCORD_MESSAGE_LIMIT = 2000
OWNER_USER_ID = 1493103010954346626
UPDATE_COMMAND = "./update"
MU_LINK_PATTERN = re.compile(r"https?://(?:app\.)?warera\.io/mu/([A-Za-z0-9_-]+)")

load_dotenv(ROOT_DIR / ".env")

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", str(DEFAULT_CHANNEL_ID)))
UPDATE_HOUR_UTC = int(os.getenv("UPDATE_HOUR_UTC", "12"))
WARERA_GATEWAY_BASE_URL = os.getenv("WARERA_API_BASE_URL", "https://gateway.warerastats.io/trpc").rstrip("/")
WARERA_DIRECT_BASE_URL = "https://api2.warera.io/trpc"
WARERA_API_TOKEN = os.getenv("WARERA_API_TOKEN")
ENABLE_PREFIX_UPDATE = os.getenv("ENABLE_PREFIX_UPDATE", "true").lower() == "true"

DEFAULT_DAMAGE_INPUTS = {
    "pillMode": "all",
    "hpMode": "full",
    "gearMode": "purple",
    "timeframe": "window18h",
    "food": "fish",
    "battleBonusPct": 70,
}

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

FOOD_FACTOR = {"bread": 0.1, "steak": 0.15, "fish": 0.2}

TIER_SCORES = {
    "none": 0.0,
    "unranked": 0.0,
    "bronze": 0.0,
    "silver": 2.5,
    "gold": 5.0,
    "platinum": 7.5,
    "diamond": 10.0,
}

if not TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN. Copy .env.example to .env and add your bot token.")

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = ENABLE_PREFIX_UPDATE

bot = discord.Client(intents=intents)


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user}")

    await update_directory_messages()

    if not daily_directory_update.is_running():
        daily_directory_update.start()


@bot.event
async def on_message(message: discord.Message) -> None:
    if not ENABLE_PREFIX_UPDATE:
        return

    if message.author.bot:
        return

    if message.channel.id != CHANNEL_ID:
        return

    if message.content.strip() != UPDATE_COMMAND:
        return

    if message.author.id != OWNER_USER_ID:
        return

    try:
        await message.delete()
    except discord.HTTPException as error:
        print(f"Could not delete update command message {message.id}: {error}")

    await message.channel.send("MU directory controls", view=DirectoryControlView())


@tasks.loop(hours=24)
async def daily_directory_update() -> None:
    await update_directory_messages()


@daily_directory_update.before_loop
async def before_daily_directory_update() -> None:
    await bot.wait_until_ready()
    delay = seconds_until_next_update()
    next_run = datetime.now(timezone.utc) + timedelta(seconds=delay)
    print(f"Next MU directory update scheduled for {next_run.isoformat()}")
    await asyncio.sleep(delay)


async def update_directory_messages() -> None:
    saved_mus = read_mus()

    try:
        channel = bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)
    except discord.Forbidden as error:
        raise RuntimeError(
            f"Missing access to Discord channel {CHANNEL_ID}. "
            "Invite the bot to that server and give it View Channel, Send Messages, "
            "Read Message History, and Embed Links permissions for the target channel."
        ) from error

    if not isinstance(channel, discord.abc.Messageable):
        raise RuntimeError(f"Channel {CHANNEL_ID} is not messageable.")

    progress_message = await channel.send("Updating... 0%")

    async def report_progress(percent: int, eta_seconds: float | None = None) -> None:
        await update_progress_message(progress_message, percent, eta_seconds)

    mus = await fetch_saved_mus(saved_mus, report_progress)
    await report_progress(100)

    contents = render_directory_messages(mus)
    state = read_state()
    previous_message_ids = get_previous_message_ids(state)
    next_message_ids = []

    for index, content in enumerate(contents):
        message_id = previous_message_ids[index] if index < len(previous_message_ids) else None

        if message_id is None:
            message = await channel.send(content, suppress_embeds=True)
            next_message_ids.append(message.id)
            continue

        try:
            message = await channel.fetch_message(message_id)
            await message.edit(content=content, suppress=True)
            next_message_ids.append(message.id)
        except discord.NotFound:
            message = await channel.send(content, suppress_embeds=True)
            next_message_ids.append(message.id)
        except discord.HTTPException as error:
            print(f"Could not edit message {message_id}; posting a fresh one: {error}")
            message = await channel.send(content, suppress_embeds=True)
            next_message_ids.append(message.id)

    for stale_message_id in previous_message_ids[len(contents):]:
        try:
            stale_message = await channel.fetch_message(stale_message_id)
            await stale_message.delete()
        except discord.HTTPException as error:
            print(f"Could not delete stale message {stale_message_id}: {error}")

    state["directory_message_ids"] = next_message_ids
    state.pop("directory_message_id", None)
    write_state(state)

    await cleanup_channel_messages(channel, set(next_message_ids) | {progress_message.id})
    print(f"Updated {len(next_message_ids)} MU directory message(s).")

    try:
        await progress_message.delete(delay=5)
    except discord.HTTPException as error:
        print(f"Could not delete progress message {progress_message.id}: {error}")


async def cleanup_channel_messages(channel: discord.abc.Messageable, keep_message_ids: set[int]) -> None:
    if not hasattr(channel, "history"):
        return

    try:
        async for message in channel.history(limit=100):
            if not bot.user or message.author.id != bot.user.id:
                continue

            if message.id in keep_message_ids:
                continue

            try:
                await message.delete()
            except discord.HTTPException as error:
                print(f"Could not delete clutter message {message.id}: {error}")
    except discord.HTTPException as error:
        print(f"Could not clean channel messages: {error}")


def read_mus() -> list[dict]:
    if not MUS_FILE.exists():
        return []

    with MUS_FILE.open("r", encoding="utf-8") as file:
        mus = json.load(file)

    if not isinstance(mus, list):
        raise RuntimeError("data/mus.json must contain a list of MUs.")

    return mus


def write_mus(mus: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with MUS_FILE.open("w", encoding="utf-8") as file:
        json.dump(mus, file, indent=2)
        file.write("\n")


async def update_progress_message(message: discord.Message, percent: int, eta_seconds: float | None = None) -> None:
    percent = max(0, min(100, percent))
    eta_text = ""

    if eta_seconds is not None and percent < 100:
        eta_text = f" | ETA: {format_eta(eta_seconds)}"

    try:
        await message.edit(content=f"Updating... {percent}%{eta_text}")
    except discord.HTTPException as error:
        print(f"Could not update progress message {message.id}: {error}")


async def fetch_saved_mus(saved_mus: list[dict], progress_callback=None) -> list[dict]:
    headers = {}
    if WARERA_API_TOKEN:
        headers["Authorization"] = f"Bearer {WARERA_API_TOKEN}"
        headers["X-API-Key"] = WARERA_API_TOKEN

    async with ClientSession(headers=headers) as session:
        total_mus = len(saved_mus)
        started_at = time.monotonic()

        if total_mus == 0:
            if progress_callback:
                await progress_callback(100, 0)
            return []

        tasks_list = [
            asyncio.create_task(fetch_saved_mu_with_index(session, index, saved_mu))
            for index, saved_mu in enumerate(saved_mus)
        ]
        fetched_mus = [None] * total_mus
        completed = 0

        for task in asyncio.as_completed(tasks_list):
            index, fetched_mu = await task
            completed += 1
            fetched_mus[index] = fetched_mu

            if progress_callback:
                elapsed = time.monotonic() - started_at
                remaining = total_mus - completed
                eta_seconds = (elapsed / completed) * remaining if completed > 0 else None
                await progress_callback(round(completed / total_mus * 100), eta_seconds)

        return [mu for mu in fetched_mus if isinstance(mu, dict)]


async def fetch_saved_mu_with_index(session: ClientSession, index: int, saved_mu: dict) -> tuple[int, dict]:
    return index, await fetch_saved_mu(session, saved_mu)


async def fetch_saved_mu(session: ClientSession, saved_mu: dict) -> dict:
    mu_id = saved_mu.get("id") or saved_mu.get("muId")

    if not mu_id:
        return saved_mu

    try:
        mu_data, hq_upgrade, dorms_upgrade = await asyncio.gather(
            trpc_request(session, "mu.getById", [{"muId": str(mu_id)}, {"id": str(mu_id)}]),
            trpc_request_any(
                session,
                "upgrade.getUpgradeByTypeAndEntity",
                [
                    {"upgradeType": "headquarters", "muId": str(mu_id)},
                    {"type": "headquarters", "entityType": "militaryUnit", "entityId": str(mu_id)},
                    {"type": "headquarters", "entityType": "mu", "entityId": str(mu_id)},
                ],
            ),
            trpc_request_any(
                session,
                "upgrade.getUpgradeByTypeAndEntity",
                [
                    {"upgradeType": "dormitories", "muId": str(mu_id)},
                    {"type": "dormitories", "entityType": "militaryUnit", "entityId": str(mu_id)},
                    {"type": "dormitories", "entityType": "mu", "entityId": str(mu_id)},
                ],
            ),
        )
    except Exception as error:
        print(f"Could not fetch MU {mu_id} from WarEra API: {error}")
        return saved_mu

    mu = unwrap_first_dict(mu_data) or {}
    hq = unwrap_first_dict(hq_upgrade) or {}
    dorms = unwrap_first_dict(dorms_upgrade) or {}

    commander_ids = find_commander_ids(mu)
    members = find_member_ids(mu)

    commanders, potential_damage = await asyncio.gather(
        fetch_usernames(session, commander_ids) if commander_ids else async_value(find_commanders(mu)),
        compute_mu_potential_damage(session, str(mu_id), members),
    )

    score = compute_mu_score(mu)
    hq_level = find_level(hq, mu, ["headquarters", "hq", "headquartersLevel", "hqLevel"])
    dorms_level = find_level(dorms, mu, ["dormitories", "dorms", "dormitoriesLevel", "dormsLevel"])
    name = find_first(mu, ["name", "militaryUnitName"], saved_mu.get("name", f"MU {mu_id}"))

    return {
        **saved_mu,
        "name": name,
        "url": saved_mu.get("url") or build_mu_url(mu_id),
        "hqLevel": hq_level,
        "dormsLevel": dorms_level,
        "commanders": commanders or saved_mu.get("commanders", []),
        "score": score,
        "potentialDamage": potential_damage,
    }


async def async_value(value: object) -> object:
    return value


async def compute_mu_potential_damage(
    session: ClientSession,
    mu_id: str,
    member_ids: list[str],
) -> int:
    if not member_ids:
        return 0

    users = await fetch_users(session, member_ids)
    used_damages = [
        compute_damage_potential(user, mu_id)
        for user in users
        if isinstance(user, dict)
    ]
    return sum(used_damages)


async def fetch_users(session: ClientSession, user_ids: list[str], batch_size: int = 20) -> list[dict | None]:
    users = []

    for index in range(0, len(user_ids), batch_size):
        batch = user_ids[index:index + batch_size]
        results = await asyncio.gather(
            *(fetch_user_by_id(session, user_id) for user_id in batch),
            return_exceptions=True,
        )

        for user_id, result in zip(batch, results):
            if isinstance(result, Exception):
                print(f"Could not fetch MU member {user_id} from WarEra API: {result}")
                users.append(None)
                continue

            users.append(unwrap_first_dict(result))

    return users


async def fetch_user_by_id(session: ClientSession, user_id: str) -> object:
    return await trpc_request(
        session,
        "user.getUserById",
        [
            {"userId": str(user_id)},
            {"id": str(user_id)},
        ],
    )


async def fetch_usernames(session: ClientSession, user_ids: list[str]) -> list[str]:
    users = await asyncio.gather(
        *(fetch_user_lite(session, user_id) for user_id in user_ids),
        return_exceptions=True,
    )
    names = []

    for user_id, user in zip(user_ids, users):
        if isinstance(user, Exception):
            print(f"Could not fetch commander {user_id} from WarEra API: {user}")
            names.append(user_id)
            continue

        user_data = unwrap_first_dict(user) or {}
        names.append(str(find_first(user_data, ["username", "name"], user_id)))

    return names


async def fetch_user_lite(session: ClientSession, user_id: str) -> object:
    return await trpc_request(
        session,
        "user.getUserLite",
        [
            {"userId": str(user_id)},
            {"id": str(user_id)},
        ],
    )


async def trpc_request_any(session: ClientSession, endpoint: str, payloads: list[dict]) -> object:
    last_error = None

    for payload in payloads:
        try:
            return await trpc_request(session, endpoint, payload)
        except Exception as error:
            last_error = error

    raise RuntimeError(last_error or f"WarEra request failed for {endpoint}")


async def trpc_request(session: ClientSession, endpoint: str, payload: dict | list[dict]) -> object:
    if isinstance(payload, list):
        return await trpc_request_any(session, endpoint, payload)

    base_urls = unique_urls([WARERA_GATEWAY_BASE_URL, WARERA_DIRECT_BASE_URL])
    inputs = [
        {"json": payload},
        payload,
    ]

    last_error = None

    for base_url in base_urls:
        url = f"{base_url}/{endpoint}"

        for input_payload in inputs:
            try:
                return await trpc_get(session, url, endpoint, input_payload)
            except Exception as error:
                last_error = error

            try:
                return await trpc_post(session, url, endpoint, input_payload)
            except Exception as error:
                last_error = error

    raise RuntimeError(last_error or f"WarEra request failed for {endpoint}")


async def trpc_get(session: ClientSession, url: str, endpoint: str, input_payload: dict) -> object:
    async with session.get(url, params={"input": json.dumps(input_payload)}, timeout=20) as response:
        if response.status == 429:
            await asyncio.sleep(0.3)
            async with session.get(url, params={"input": json.dumps(input_payload)}, timeout=20) as retry_response:
                if retry_response.status < 400:
                    return unwrap_trpc_response(await retry_response.json())
                raise RuntimeError(f"GET {endpoint} returned HTTP {retry_response.status}")

        if response.status < 400:
            return unwrap_trpc_response(await response.json())

        raise RuntimeError(f"GET {endpoint} returned HTTP {response.status}")


async def trpc_post(session: ClientSession, url: str, endpoint: str, input_payload: dict) -> object:
    async with session.post(url, json=input_payload, timeout=20) as response:
        if response.status == 429:
            await asyncio.sleep(0.3)
            async with session.post(url, json=input_payload, timeout=20) as retry_response:
                if retry_response.status < 400:
                    return unwrap_trpc_response(await retry_response.json())
                raise RuntimeError(f"POST {endpoint} returned HTTP {retry_response.status}")

        if response.status < 400:
            return unwrap_trpc_response(await response.json())

        raise RuntimeError(f"POST {endpoint} returned HTTP {response.status}")


def unique_urls(urls: list[str]) -> list[str]:
    seen = set()
    unique = []

    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        unique.append(url)

    return unique


def unwrap_trpc_response(response: object) -> object:
    if isinstance(response, list) and response:
        return unwrap_trpc_response(response[0])

    if not isinstance(response, dict):
        return response

    if "error" in response:
        raise RuntimeError(response["error"])

    value = response
    for key in ("result", "data", "json"):
        if isinstance(value, dict) and key in value:
            value = value[key]

    return value


def unwrap_first_dict(value: object) -> dict | None:
    if isinstance(value, dict):
        return value

    if isinstance(value, list):
        for item in value:
            found = unwrap_first_dict(item)
            if found:
                return found

    return None


def find_first(source: dict, keys: list[str], default: object = None) -> object:
    for key in keys:
        value = deep_get(source, key)
        if value not in (None, ""):
            return value
    return default


def find_level(*sources_and_keys: object) -> int:
    *sources, keys = sources_and_keys

    for source in sources:
        if not isinstance(source, dict):
            continue

        level = find_first(source, ["level", "currentLevel", "upgradeLevel"])
        if level is not None:
            return format_level(level)

        for key in keys:
            value = deep_get(source, key)
            if isinstance(value, dict):
                nested_level = find_first(value, ["level", "currentLevel", "upgradeLevel"])
                if nested_level is not None:
                    return format_level(nested_level)
            elif value is not None:
                return format_level(value)

    return 0


def deep_get(source: dict, dotted_key: str) -> object:
    value = source

    for part in dotted_key.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]

    return value


def find_commanders(mu: dict) -> list[str]:
    for key in ("commanders", "leaders", "managers"):
        commanders = deep_get(mu, key)
        names = extract_names(commanders)
        if names:
            return names

    for key in ("members", "users"):
        members = deep_get(mu, key)
        if not isinstance(members, list):
            continue

        names = [
            name
            for member in members
            for name in extract_names(member)
            if isinstance(member, dict) and is_commander(member)
        ]
        if names:
            return names

    return []


def find_commander_ids(mu: dict) -> list[str]:
    commanders = deep_get(mu, "roles.commanders")
    if not isinstance(commanders, list):
        return []
    return [str(commander_id) for commander_id in commanders if commander_id]


def find_member_ids(mu: dict) -> list[str]:
    members = deep_get(mu, "members")
    if not isinstance(members, list):
        return []
    return [str(member_id) for member_id in members if member_id]


def compute_mu_score(mu: dict) -> float:
    rankings = deep_get(mu, "rankings")

    if not isinstance(rankings, dict):
        return 0.0

    scores = []

    for ranking in rankings.values():
        if not isinstance(ranking, dict):
            continue

        tier = str(ranking.get("tier", "")).strip().lower()
        scores.append(TIER_SCORES.get(tier, 0.0))

    if not scores:
        return 0.0

    return sum(scores) / len(scores)


def compute_damage_potential(
    user: dict,
    mu_id: str,
    inputs: dict | None = None,
    config: dict | None = None,
) -> int:
    inputs = inputs or DEFAULT_DAMAGE_INPUTS
    config = config or DEFAULT_DAMAGE_CONFIG
    skills = user.get("skills") or {}
    gear = config["purpleGear"]
    is_purple = inputs["gearMode"] == "purple"

    has_buff = bool(deep_get(user, "buffs.buffCodes")) and is_future_date(deep_get(user, "buffs.buffEndAt"))
    has_debuff = bool(deep_get(user, "buffs.debuffCodes")) and is_future_date(deep_get(user, "buffs.debuffEndAt"))
    del has_buff, has_debuff, mu_id

    max_health = num(deep_get(skills, "health.value"), 100)
    max_hunger = int(num(deep_get(skills, "hunger.value"), 100))
    current_health = num(deep_get(skills, "health.currentBarValue"))
    current_hunger = int(num(deep_get(skills, "hunger.currentBarValue")))

    precision_value = num(deep_get(skills, "precision.value"))
    precision_gear = gear["glovesPrecision"] if is_purple else num(deep_get(skills, "precision.equipment"))
    precision_total = clamp(precision_value + precision_gear, 0, 100)
    precision_overflow = max(precision_value + precision_gear - 100, 0)

    crit_chance_value = num(deep_get(skills, "criticalChance.value"))
    crit_chance_gear = gear["weaponCrit"] if is_purple else num(deep_get(skills, "criticalChance.weapon"))
    crit_chance_total = clamp(crit_chance_value + crit_chance_gear, 0, 100)
    crit_chance_overflow = max(crit_chance_value + crit_chance_gear - 100, 0)

    crit_damage_value = num(deep_get(skills, "criticalDamages.value"))
    crit_damage_gear = gear["helmetCritDmg"] if is_purple else num(deep_get(skills, "criticalDamages.equipment"))
    crit_damage_total = crit_damage_value + crit_damage_gear + crit_chance_overflow * 4

    if is_purple:
        armor_raw = num(deep_get(skills, "armor.value")) + gear["armorGear"]
        armor_eff = armor_raw / (armor_raw + 40) if armor_raw + 40 else 0
        dodge_raw = num(deep_get(skills, "dodge.value")) + gear["bootsDodge"]
        dodge_eff = dodge_raw / (dodge_raw + 40) if dodge_raw + 40 else 0
    else:
        armor_eff = num(deep_get(skills, "armor.totalAfterSoftCap")) / 100
        dodge_eff = num(deep_get(skills, "dodge.totalAfterSoftCap")) / 100

    attack_value = num(deep_get(skills, "attack.value"))
    attack_weapon = gear["weaponAttack"] if is_purple else num(deep_get(skills, "attack.weapon"))
    base_sum = attack_value + attack_weapon + precision_overflow * 4
    ammo_pct = gear["ammoPct"] if is_purple else num(deep_get(skills, "attack.ammoPercent"))
    ammo_mult = 1 + ammo_pct / 100
    rank_mult = 1 + num(deep_get(skills, "attack.militaryRankPercent")) / 100

    if inputs["pillMode"] == "all":
        pill_mult = 1.6
    elif inputs["pillMode"] == "sober":
        pill_mult = 1.0
    else:
        buffs_pct = num(deep_get(skills, "attack.buffsPercent"))
        debuffs_pct = num(deep_get(skills, "attack.debuffsPercent"))
        pill_mult = (1 + buffs_pct / 100) * (1 - debuffs_pct / 100)

    bonus_mult = 1 + num(inputs.get("battleBonusPct")) / 100
    attack = base_sum * ammo_mult * pill_mult * rank_mult * bonus_mult

    crit_hit = attack * (1 + crit_damage_total / 100)
    hit_chance = precision_total / 100
    crit_chance = crit_chance_total / 100
    avg_damage = (
        hit_chance * (attack * (1 - crit_chance) + crit_hit * crit_chance)
        + (1 - hit_chance) * (attack * 0.5)
    )

    food_factor = FOOD_FACTOR[inputs["food"]]
    if inputs["timeframe"] == "window18h":
        pool = max_health * 1.8 + max_hunger * 1.8 * food_factor * max_health + 10
    elif inputs["hpMode"] == "real":
        pool = current_health + current_hunger * food_factor * max_health
    else:
        pool = max_health + max_hunger * food_factor * max_health + 10

    health_per_hit = (100 - clamp(armor_eff * 100, 0, 99.99)) / 10
    total_hits = pool / health_per_hit / (1 - clamp(dodge_eff, 0, 0.9999))
    return round(total_hits * avg_damage * config["safetyMargin"])


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def num(value: object, default: float = 0) -> float:
    if value is None:
        return default

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def is_future_date(value: object) -> bool:
    if not value:
        return False

    try:
        raw = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(raw).timestamp() > datetime.now(timezone.utc).timestamp()
    except ValueError:
        return False


def extract_names(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]

    if isinstance(value, dict):
        name = find_first(value, ["username", "name", "user.username", "user.name"])
        return [str(name)] if name else []

    if isinstance(value, list):
        names = []
        for item in value:
            names.extend(extract_names(item))
        return names

    return []


def is_commander(member: dict) -> bool:
    role = str(find_first(member, ["role", "rank", "position", "membershipRole"], "")).lower()
    return "commander" in role or "leader" in role or role in {"owner", "admin"}


def build_mu_url(mu_id: object) -> str:
    return f"https://app.warera.io/mu/{mu_id}"


def parse_mu_link(link: str) -> tuple[str, str]:
    match = MU_LINK_PATTERN.search(link.strip())

    if not match:
        raise ValueError("That does not look like a WarEra MU link.")

    mu_id = match.group(1)
    return mu_id, f"https://app.warera.io/mu/{mu_id}"


def add_saved_mu(mu_id: str, url: str, name: str | None = None) -> bool:
    mus = read_mus()

    if any(str(mu.get("id") or mu.get("muId")) == mu_id for mu in mus):
        return False

    mus.append({"id": mu_id, "name": name or f"MU {mu_id}", "url": url})
    write_mus(mus)
    return True


def remove_saved_mu(mu_id: str) -> bool:
    mus = read_mus()
    kept_mus = [mu for mu in mus if str(mu.get("id") or mu.get("muId")) != mu_id]

    if len(kept_mus) == len(mus):
        return False

    write_mus(kept_mus)
    return True


def render_directory_messages(mus: list[dict]) -> list[str]:
    return chunk_lines(render_directory_lines(mus))


def render_directory_lines(mus: list[dict]) -> list[str]:
    lines = [
        "**INFO**",
        "> Score: average ranking tier score across MU rankings. Bronze 0, Silver 2.5, Gold 5, Platinum 7.5, Diamond 10.",
        "> Potential Damage: estimated member damage using full/purple gear, all pilled, 18h window, fish food, 70% battle bonus, and a 0.9 safety margin.",
        '> Contact Donut to update mu-dir or ask them to run "./update" with your MU link.',
        "",
        "**MU's**",
    ]

    if not mus:
        lines.append("No MUs saved yet.")

    for mu in mus:
        lines.extend(
            [
                "",
                f"> **[{mu.get('name', 'Unknown MU')}](<{mu.get('url', '')}>)**",
                f"> HQ: {format_level(mu.get('hqLevel'))}/4",
                f"> Dorms: {format_level(mu.get('dormsLevel'))}/5",
                f"> Commanders: {format_commanders(mu.get('commanders'))}",
            ]
        )

    lines.extend(["", "**LEADERBOARD + STATS**"])

    if not mus:
        lines.append("No leaderboard entries yet.")

    leaderboard_mus = sorted(
        mus,
        key=lambda mu: (float(mu.get("score", 0.0)), int(mu.get("potentialDamage", 0))),
        reverse=True,
    )

    lines.append("> MU NAME - Score - Potential Damage (Estimation. Check with commanders first)")

    for index, mu in enumerate(leaderboard_mus, start=1):
        lines.append(
            f"> {index}. **{mu.get('name', 'Unknown MU')}** - "
            f"{format_score(mu.get('score'))} - {format_damage(mu.get('potentialDamage'))}"
        )

    return lines


def chunk_lines(lines: list[str]) -> list[str]:
    chunks = []
    current = ""

    for line in lines:
        next_chunk = line if not current else f"{current}\n{line}"

        if len(next_chunk) <= DISCORD_MESSAGE_LIMIT:
            current = next_chunk
            continue

        if current:
            chunks.append(current)

        while len(line) > DISCORD_MESSAGE_LIMIT:
            chunks.append(line[:DISCORD_MESSAGE_LIMIT])
            line = line[DISCORD_MESSAGE_LIMIT:]

        current = line

    if current:
        chunks.append(current)

    return chunks


def format_commanders(commanders: object) -> str:
    if not isinstance(commanders, list) or not commanders:
        return "Unknown"

    return ", ".join(str(commander) for commander in commanders)


def format_level(level: object) -> int:
    try:
        return int(level)
    except (TypeError, ValueError):
        return 0


def format_score(score: object) -> str:
    try:
        return f"{float(score):.1f}"
    except (TypeError, ValueError):
        return "0.0"


def format_damage(damage: object) -> str:
    try:
        return f"{int(damage):,}"
    except (TypeError, ValueError):
        return "0"


def format_eta(seconds: float) -> str:
    seconds = max(0, round(seconds))

    if seconds < 60:
        return f"{seconds}s"

    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remainder}s"

    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def read_state() -> dict:
    if not STATE_FILE.exists():
        return {}

    with STATE_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with STATE_FILE.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
        file.write("\n")


def get_previous_message_ids(state: dict) -> list[int]:
    message_ids = state.get("directory_message_ids")

    if isinstance(message_ids, list):
        return [int(message_id) for message_id in message_ids]

    message_id = state.get("directory_message_id")
    if message_id:
        return [int(message_id)]

    return []


def seconds_until_next_update() -> float:
    now = datetime.now(timezone.utc)
    update_hour = max(0, min(23, UPDATE_HOUR_UTC))
    next_run = now.replace(hour=update_hour, minute=0, second=0, microsecond=0)

    if next_run <= now:
        next_run += timedelta(days=1)

    return (next_run - now).total_seconds()


class DirectoryControlView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=120)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != OWNER_USER_ID:
            await interaction.response.send_message("You cannot use these controls.", ephemeral=True)
            return False

        if interaction.channel_id != CHANNEL_ID:
            await interaction.response.send_message("Use this in the MU directory channel.", ephemeral=True)
            return False

        return True

    @discord.ui.button(label="Add MU", style=discord.ButtonStyle.green)
    async def add_mu(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AddMuModal(interaction.message))

    @discord.ui.button(label="Remove MU", style=discord.ButtonStyle.red)
    async def remove_mu(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        saved_mus = read_mus()

        if not saved_mus:
            await interaction.response.send_message("There are no saved MUs to remove.", ephemeral=True)
            return

        fetched_mus = await fetch_saved_mus(saved_mus)
        await interaction.response.send_message(
            "Pick an MU to remove.",
            view=RemoveMuView(fetched_mus, control_message=interaction.message),
            ephemeral=True,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await update_directory_messages()
        await delete_control_message(interaction.message)
        await interaction.followup.send("MU directory refreshed.", ephemeral=True)


class AddMuModal(discord.ui.Modal, title="Add MU"):
    mu_link = discord.ui.TextInput(
        label="WarEra MU link",
        placeholder="https://app.warera.io/mu/69cf764cf18f2f6578e948e8",
        required=True,
        max_length=200,
    )

    def __init__(self, control_message: discord.Message | None) -> None:
        super().__init__()
        self.control_message = control_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            mu_id, url = parse_mu_link(str(self.mu_link.value))
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        saved_mu = {"id": mu_id, "url": url}
        fetched_mu = (await fetch_saved_mus([saved_mu]))[0]
        added = add_saved_mu(mu_id, url, str(fetched_mu.get("name") or f"MU {mu_id}"))

        if not added:
            await interaction.followup.send("That MU is already saved.", ephemeral=True)
            return

        await update_directory_messages()
        await delete_control_message(self.control_message)
        await interaction.followup.send(f"Added {fetched_mu.get('name', 'MU')}.", ephemeral=True)


class RemoveMuView(discord.ui.View):
    PAGE_SIZE = 20

    def __init__(self, mus: list[dict], page: int = 0, control_message: discord.Message | None = None) -> None:
        super().__init__(timeout=120)
        self.mus = mus
        self.page = page
        self.control_message = control_message
        self.add_mu_buttons()

    def add_mu_buttons(self) -> None:
        start = self.page * self.PAGE_SIZE
        end = start + self.PAGE_SIZE

        for mu in self.mus[start:end]:
            mu_id = str(mu.get("id") or mu.get("muId"))
            label = str(mu.get("name") or f"MU {mu_id}")[:80]
            self.add_item(RemoveMuButton(label, mu_id, self.control_message))

        if self.page > 0:
            self.add_item(RemovePageButton("Previous", self.page - 1, self.control_message))

        if end < len(self.mus):
            self.add_item(RemovePageButton("Next", self.page + 1, self.control_message))


class RemoveMuButton(discord.ui.Button):
    def __init__(self, label: str, mu_id: str, control_message: discord.Message | None) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.danger)
        self.mu_id = mu_id
        self.control_message = control_message

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != OWNER_USER_ID:
            await interaction.response.send_message("You cannot use these controls.", ephemeral=True)
            return

        removed = remove_saved_mu(self.mu_id)

        if not removed:
            await interaction.response.send_message("That MU was already removed.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await update_directory_messages()
        await delete_control_message(self.control_message)
        await interaction.edit_original_response(content="Removed MU.", view=None)


class RemovePageButton(discord.ui.Button):
    def __init__(self, label: str, page: int, control_message: discord.Message | None) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.page = page
        self.control_message = control_message

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != OWNER_USER_ID:
            await interaction.response.send_message("You cannot use these controls.", ephemeral=True)
            return

        mus = await fetch_saved_mus(read_mus())
        await interaction.response.edit_message(
            content="Pick an MU to remove.",
            view=RemoveMuView(mus, self.page, self.control_message),
        )


async def delete_control_message(message: discord.Message | None) -> None:
    if message is None:
        return

    try:
        await message.delete()
    except discord.HTTPException as error:
        print(f"Could not delete MU control message {message.id}: {error}")


bot.run(TOKEN)