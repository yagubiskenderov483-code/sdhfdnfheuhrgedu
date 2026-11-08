"""Apply the reviewed reliability patch atomically; never read production secrets.
This migration script is used only by the isolated fix-branch verification workflow.
"""
from __future__ import annotations
import ast
import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
changes = {}


def get(path):
    return changes.get(path, (ROOT / path).read_text(encoding="utf-8"))


def put(path, source):
    if path.endswith(".py"):
        ast.parse(source, filename=path)
    changes[path] = source


def replace(path, old, new, count=1):
    source = get(path)
    actual = source.count(old)
    if actual != count:
        raise RuntimeError(f"{path}: expected {count} occurrences, got {actual}: {old[:100]!r}")
    put(path, source.replace(old, new))


def function(path, name, body, cls=None):
    source = get(path)
    tree = ast.parse(source)
    parent = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls) if cls else tree
    node = next(n for n in parent.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
    lines = source.splitlines(keepends=True)
    replacement = textwrap.dedent(body).strip() + "\n"
    if cls:
        replacement = textwrap.indent(replacement, "    ")
    put(path, "".join(lines[:start]) + replacement + "".join(lines[node.end_lineno:]))


if "# reliability-fix-v1" in (ROOT / "main.py").read_text(encoding="utf-8"):
    print("Reliability source patch is already applied.")
    raise SystemExit(0)

put("utils/links.py", '''"""Canonical Telegram links. Never invent an NFT slug or a seller username."""
from __future__ import annotations
import re
from urllib.parse import unquote, urlsplit

_SLUG = re.compile(r"[A-Za-z][A-Za-z0-9]*-[1-9][0-9]*\\Z")
_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}\\Z")


def gift_link(slug: str | None) -> str | None:
    value = (slug or "").strip()
    if value.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(value)
        except ValueError:
            return None
        if parsed.hostname not in ("t.me", "telegram.me") or not parsed.path.startswith("/nft/"):
            return None
        value = unquote(parsed.path[5:]).rstrip("/")
    return "https://t.me/nft/" + value if _SLUG.fullmatch(value) else None


def listing_gift_link(listing) -> str | None:
    return gift_link(getattr(listing, "gift_slug", None)) or gift_link(getattr(listing, "gift_link", None))


def normalize_username(username: str | None) -> str | None:
    value = (username or "").strip().lstrip("@")
    return value if _USERNAME.fullmatch(value) else None


def username_link(username: str | None) -> str | None:
    value = normalize_username(username)
    return "https://t.me/" + value if value else None


def user_id_deeplink(user_id: int | None) -> str | None:
    return f"tg://user?id={user_id}" if user_id and user_id > 0 else None
''')

put("tracker/reliability.py", '''"""Pure freshness helpers; lookup and retry must not reset a lot's age."""
from __future__ import annotations
from datetime import datetime, timezone
import time


def detected_timestamp(listing) -> float:
    try:
        stamp = datetime.fromisoformat(listing.first_seen)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return 0.0


def age_seconds(listing, *, now_wall=None, now_mono=None) -> float:
    wall = time.time() if now_wall is None else now_wall
    mono = time.monotonic() if now_mono is None else now_mono
    age = max(0.0, wall - detected_timestamp(listing))
    found = getattr(listing, "t_found", None)
    if found is not None:
        age = max(age, mono - found)
    return age


def age_limit(settings, *, lookup=False) -> float:
    values = [float(settings.max_lot_age)]
    if lookup:
        values.append(float(settings.lookup_max_age))
    positive = [v for v in values if v > 0]
    return min(positive) if positive else 0.0


def expires_at(listing, limit: float) -> float | None:
    return detected_timestamp(listing) + limit if limit > 0 else None
''')

put("filters/price_filter.py", '''"""Price rules using API amounts only; rare-pattern rarity is not an exception."""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from config import settings
from database.models import Listing


@dataclass
class FilterResult:
    passed: bool
    reason: str


def _rare(value: str | None) -> bool:
    text = (value or "").strip().casefold()
    if text in {"rare", "epic", "legendary", "редкая", "эпическая", "легендарная"}:
        return True
    if not text.endswith("%"):
        return False
    try:
        percent = Decimal(text[:-1].replace(",", "."))
        return percent.is_finite() and Decimal(0) < percent <= Decimal(str(settings.rare_attribute_percent))
    except (InvalidOperation, ValueError):
        return False


def premium_exception(l: Listing) -> bool:
    monochrome = any((v or "").strip().casefold() in {"monochrome", "монохром"}
                     for v in (l.model, l.backdrop))
    return monochrome or _rare(l.model_rarity) or _rare(l.backdrop_rarity)


class PriceFilter:
    def check(self, l: Listing) -> FilterResult:
        minimum = settings.collection_min_floor or settings.min_stars
        stars, ton = l.stars_price, l.ton_price
        ton_only = l.ton_only or stars is None
        if ton_only and not settings.allow_ton_only:
            return FilterResult(False, "TON-only listing (ALLOW_TON_ONLY=false)")
        if (stars is None and ton is None) or (stars is not None and stars <= 0) or (ton is not None and ton <= 0):
            return FilterResult(False, "no positive price in API response")
        if l.ton_only and ton is None:
            return FilterResult(False, "TON-only listing has no TON price")
        if stars is not None and stars < minimum:
            return FilterResult(False, f"{stars} Stars < {minimum}")
        if ton is not None and ton < settings.min_ton:
            return FilterResult(False, f"{ton:g} TON < {settings.min_ton:g}")
        threshold = settings.premium_over_floor_percent
        if stars is not None and l.floor_stars and l.floor_stars > 0:
            over_scaled = (stars - l.floor_stars) * 100
            special = premium_exception(l)
            if threshold > 0 and over_scaled >= l.floor_stars * threshold and not special:
                return FilterResult(False, f"premium >= {threshold}% above floor; no rare model/backdrop or Monochrome")
            cap = settings.max_above_floor
            if cap > 0 and over_scaled > l.floor_stars * cap and not (threshold > 0 and special):
                return FilterResult(False, f"{stars} Stars above floor {l.floor_stars} by more than {cap}%")
        return FilterResult(True, "API price and attribute rules passed")
''')

# Existing settings from the previous commit are wired into the real code here.
# No ethnicity inference or name-based ethnicity filter is added.
config = get("config.py")
config = "\n".join(line for line in config.split("\n") if "block_arabic_script" not in line)
config = config.replace("seller_cache_ttl: int = 3600", "seller_cache_ttl: int = 60")
config = config.replace('_int(e("SELLER_CACHE_TTL"), 3600)', '_int(e("SELLER_CACHE_TTL"), 60)')
config = config.replace('    "FETCH_FULL_SELLER": ("fetch_full_seller", _bool_strict),',
    '    "MAX_LOT_AGE": ("max_lot_age", lambda v: max(0, min(86400, int(v)))),\n'
    '    "SELLER_CACHE_TTL": ("seller_cache_ttl", lambda v: max(0, min(3600, int(v)))),\n'
    '    "FETCH_FULL_SELLER": ("fetch_full_seller", _bool_strict),')
config = re.sub(r'^(    max_lot_age: int = 180) .*$', r'\1  # maximum detection-to-delivery age in seconds', config, flags=re.M)
config = re.sub(r'^(    lookup_max_age: int = 180) .*$', r'\1  # includes waiting, captcha, enrichment and delivery', config, flags=re.M)
put("config.py", config)

# Always retain a visible NFT URL and canonical Lot button, even without Telegram's preview.
formatter = get("bot/formatter.py")
formatter = formatter.replace("from config import settings", "from config import settings\nfrom utils.links import listing_gift_link, normalize_username")
formatter = formatter.replace("l.gift_link", "listing_gift_link(l)")
formatter = formatter.replace("quote(offer_text, safe='/')", "quote(offer_text, safe='')")
formatter = formatter.replace('    elif offer_text:\n        url = f"https://t.me/{trade_bot}?text={quote(offer_text, safe=\'\')}"\n', "")
put("bot/formatter.py", formatter)
function("bot/formatter.py", "render", '''
def render(self, l: Listing) -> str:
    text = self.template.format_map(self.variables(l))
    link = listing_gift_link(l)
    if link:
        # A raw URL survives HTML fallback and disabled/unavailable link previews.
        text += "\\n🔗 NFT: " + link
    return text
''', "MessageFormatter")

# Do not guess ownership from a display name in users[].
function("tracker/marketplace.py", "fetch_gift_owner", '''
async def fetch_gift_owner(client: TelegramClient, slug: str) -> Any:
    from telethon.tl.functions.payments import GetUniqueStarGiftRequest
    response = await client(GetUniqueStarGiftRequest(slug=slug))
    gift = getattr(response, "gift", None)
    if gift is None:
        return None
    peer = getattr(gift, "owner_id", None) or getattr(gift, "host_id", None)
    if not isinstance(peer, types.PeerUser):
        return None
    return next((u for u in getattr(response, "users", None) or []
                 if getattr(u, "id", None) == peer.user_id and not getattr(u, "bot", False)), None)
''')

# Lookup: past names and cached bot records are candidates, not proof of ownership.
lookup = "tracker/lookup.py"
replace(lookup, "        for first in known:\n", "        for first in known if not settings.strict_owner_verification else []:\n")
replace(lookup, "        known = await self._known(seller_name)\n", '''        known = await self._known(seller_name)
        if deadline is not None and time.time() >= deadline:
            raise _Expired()
        if known and settings.strict_owner_verification:
            verified = await self._verify(session, known, seller_name, slug, unique_id)
            if verified is not None:
                verified.from_memory = True
                self.stats["found"] += 1
                self.stats["from_memory"] = self.stats.get("from_memory", 0) + 1
                self._track_lookup(seller_name, "from_memory", "exact NFT reverified")
                return verified
''')
replace(lookup, "        trust_without_nft = (settings.lookup_trust_name_match\n", "        trust_without_nft = (not settings.strict_owner_verification and settings.lookup_trust_name_match\n")
replace(lookup, "        if settings.lookup_trust_name_match and not verified_rejected:\n", "        if not settings.strict_owner_verification and settings.lookup_trust_name_match and not verified_rejected:\n")
replace(lookup, "            if settings.lookup_name_check and not same_name(real, seller_name):\n", "            if (settings.lookup_name_check or settings.strict_owner_verification) and not same_name(real, seller_name):\n")
replace(lookup, "            return FoundOwner(peer.user_id, uname, real or seller_name, getattr(u, \"premium\", None), session)\n", '''            if getattr(u, "bot", False) or getattr(u, "deleted", False):
                continue
            if c.user_id and getattr(u, "id", None) != c.user_id:
                continue
            from utils.links import normalize_username
            uname = normalize_username(uname)
            if settings.require_username and not uname:
                continue
            return FoundOwner(peer.user_id, uname, real or seller_name, getattr(u, "premium", None), session)
''')
# A failed NFT verification must not suppress different gifts from the same display name.
replace(lookup, "        neg = self._neg_cache.get(key)\n", "        negative_key = (key, slug or unique_id)\n        neg = self._neg_cache.get(negative_key)\n")
replace(lookup, "time.monotonic() - neg < _NEG_CACHE_TTL", "time.monotonic() - neg < min(_NEG_CACHE_TTL, 60)")
source = get(lookup)
source = source.replace("self._neg_cache[key] = time.monotonic()", "self._neg_cache[negative_key] = time.monotonic()")
source = source.replace("кешируем на час", "кешируем до 60 с для этого NFT")
source = source.replace("кешируем 'не найден' на час", "кешируем 'не найден' до 60 с для этого NFT")
put(lookup, source)
# Prevent a late/edited captcha poll from reading candidates from a previous conversation.
replace(lookup, '            return [m for m in reversed(msgs) if not getattr(m, "out", False)]\n', '''            return [m for m in reversed(msgs)
                    if not getattr(m, "out", False) and m.id > sent_id
                    and getattr(m, "reply_to_msg_id", None) in (None, sent_id)]
''')
replace(lookup, '                msgs = await client.get_messages(ent, limit=5)', '                msgs = await client.get_messages(ent, limit=10, min_id=sent_id)')
replace(lookup, "        self._clear_bot_unresolved(bot)\n        sent_id = sent.id", "        sent_id = sent.id")
replace(lookup, "            if not msgs:\n                continue\n            cap =", "            if not msgs:\n                continue\n            self._clear_bot_unresolved(bot)\n            cap =")
replace(lookup, '            if cap is not None:\n                if not _retry', '            if cap is not None and cap.id == max(m.id for m in msgs):\n                if not _retry')
replace(lookup, '        if not got_answer:\n            log.warning(', '        if not got_answer or (not last_cands and any(w in self._last_response_text.get(session, "").lower() for w in _WAIT_WORDS)):\n            self._warn_unavailable(bot)\n            log.warning(')
# Per-request fallback remains possible, but no bot is globally disabled or promoted automatically.
function(lookup, "_bot_pool", '''
def _bot_pool(self) -> list[str]:
    result, seen = [], set()
    for raw in [settings.lookup_bot_username] + list(settings.lookup_bots or []):
        bot = (raw or "").strip().lstrip("@")
        if bot and bot.lower() not in seen:
            seen.add(bot.lower())
            result.append(bot)
    return result
''', "OwnerLookup")
function(lookup, "_register_bot_unresolved", '''
def _register_bot_unresolved(self, bot: str, session: str | None = None) -> None:
    self._warn_unavailable(bot)

def _warn_unavailable(self, bot: str) -> None:
    # Warning only. A resolve failure is not proof that the bot has been deleted.
    now = time.monotonic()
    notices = getattr(self, "_unavailable_notices", None)
    if notices is None:
        notices = self._unavailable_notices = {}
    if bot.lower() in notices and now - notices[bot.lower()] < 300:
        return
    notices[bot.lower()] = now
    log.warning("Lookup @%s unavailable; tracker continues, primary configuration unchanged", bot)
    if self.on_bot_dead:
        try:
            task = asyncio.get_running_loop().create_task(self.on_bot_dead(bot, None))
        except RuntimeError:
            return
        if not hasattr(self, "_notify_tasks"):
            self._notify_tasks = set()
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)
''', "OwnerLookup")
function(lookup, "load_persisted_bot_deads", '''
async def load_persisted_bot_deads(self) -> int:
    # Old persisted dead marks must not suspend lookup after an upgrade.
    self._bot_dead_until.clear()
    self._bot_dead_notified.clear()
    if self.db is not None:
        for bot in self._bot_pool():
            await self.db.kv_set(f"bot_dead:{bot.lower()}", None)
    return 0
''', "OwnerLookup")
function(lookup, "check_bots_liveness", '''
async def check_bots_liveness(self) -> dict[str, bool | None]:
    results = {}
    try:
        sessions = [w.name for w in self.sessions.usable(include_reserved=True) if w.is_usable()]
    except Exception:
        return results
    if not sessions:
        return results
    for bot in self._bot_pool():
        try:
            async def probe(client, name=bot):
                return await client.get_input_entity(name)
            peer = await asyncio.wait_for(self._run(sessions[0], probe, timeout=5), timeout=6)
            results[bot] = True if peer is not None else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            text = str(exc).lower()
            missing = any(word in text for word in ("no user has", "username_invalid", "username_not_occupied"))
            results[bot] = False if missing else None
            self._warn_unavailable(bot)
    return results
''', "OwnerLookup")
# Back off the failing account briefly, not every bot/account for 15 minutes.
source = get(lookup)
source = re.sub(r'^_UNRESOLVABLE_COOLDOWN\s*=.*$', '_UNRESOLVABLE_COOLDOWN = 15.0  # local retry backoff, never a global dead mark', source, flags=re.M)
source = source.replace('                    for session in order:\n', '                    for session in order[:2]:\n')
put(lookup, source)
# Use stable ID first; a username in an old search database may have changed owners.
replace(lookup, '            for k in ((c.username,) if c.username else ()) + ((c.user_id,) if c.user_id else ()):',
    '            for k in ((c.user_id,) if c.user_id else ((c.username,) if c.username else ())):')
replace(lookup, '                if c.username:\n                    return await client.get_input_entity(c.username)\n                return await client.get_input_entity(types.PeerUser(c.user_id))',
    '                if c.user_id:\n                    return await client.get_input_entity(types.PeerUser(c.user_id))\n                return await client.get_input_entity(c.username)')
replace(lookup, '        if not isinstance(peer, types.InputPeerUser):\n',
    '        if not isinstance(peer, types.InputPeerUser) or (c.user_id and peer.user_id != c.user_id):\n')

# Pipeline: no username exception, no age reset, no unverified-owner exception.
queue = "tracker/queue.py"
replace(queue, "from utils.links import gift_link, username_link", "from utils.links import gift_link, username_link, listing_gift_link, normalize_username\nfrom tracker.reliability import age_seconds, age_limit, expires_at")
source = get(queue)
start = source.index("        # защита от завала:", source.index("    async def process("))
end = source.index('        price_txt = ', start)
source = source[:start] + '''        base = l.t_found if l.t_found is not None else ev.detected_mono
        limit = age_limit(settings, lookup=ev.mode == "lookup" or bool(l.owner_found_via))
        age = age_seconds(l)
        if limit > 0 and age >= limit:
            l.status, l.reason = ListingStatus.ERROR, f"stale: detected {age:.0f}s ago"
            self.stats["stale"] = self.stats.get("stale", 0) + 1
            await self.db.update_listing(l)
            return
''' + source[end:]
start = source.index("        if settings.require_username and l.seller_type is not None and not l.seller_username:", source.index("    async def process("))
end = source.index("        if l.seller_type is None", start + 10)
source = source[:start] + source[end:]
source = source.replace('        if (settings.require_username and not l.seller_username\n                and not getattr(l, "owner_not_found", False)):',
    '        if settings.require_username and not normalize_username(l.seller_username):')
source = source.replace('            if pre.code not in _HIDDEN_HARD_REJECT:\n                t =', '''            if pre.code not in _HIDDEN_HARD_REJECT:
                if len(self._lookup_tasks) >= 100:
                    l.status, l.reason = ListingStatus.FILTERED_SELLER, "lookup queue full; fresh listings continue"
                    l.seller_filter_result = "LOOKUP_BUSY"
                    self.stats["lookup_busy"] = self.stats.get("lookup_busy", 0) + 1
                    await self.db.update_listing(l)
                    return
                t =''')
source = source.replace('                if full.username and not l.seller_username:\n                    l.seller_username = full.username\n                    l.seller_profile_link = username_link(full.username)',
    '                l.seller_username = normalize_username(full.username)\n                l.seller_profile_link = username_link(l.seller_username)')
source = source.replace('        if full.username and not l.seller_username:\n            l.seller_username = full.username\n            l.seller_profile_link = username_link(full.username)',
    '        l.seller_username = normalize_username(full.username)\n        l.seller_profile_link = username_link(l.seller_username)')
source = source.replace('time.monotonic() - cached[0] < settings.seller_cache_ttl', 'time.monotonic() - cached[0] < min(settings.seller_cache_ttl, 60) and not l.owner_found_via')
source = source.replace('        if getattr(l, "owner_not_found", False) and l.seller_type is None:', '''        if settings.strict_owner_verification:
            if getattr(l, "owner_not_found", False) or getattr(l, "owner_trusted_no_resolve", False):
                return SellerResult(False, "OWNER_NOT_FOUND", "owner not verified for this NFT")
            return await self._check_seller_found(l)
        if getattr(l, "owner_not_found", False) and l.seller_type is None:''')
source = source.replace('        if (getattr(l, "seller_lookup_failed", False) and settings.max_seller_level > 0',
    '        if (settings.strict_owner_verification and getattr(l, "seller_lookup_failed", False)):\n            return SellerResult(False, "LOOKUP_FAILED", "seller profile unavailable")\n        if (getattr(l, "seller_lookup_failed", False) and settings.max_seller_level > 0')
put(queue, source)
function(queue, "_lookup_then_process", '''
async def _lookup_then_process(self, l: Listing) -> None:
    limit = age_limit(settings, lookup=True)
    remaining = limit - age_seconds(l) if limit > 0 else 180.0
    if remaining <= 0:
        l.status, l.reason = ListingStatus.ERROR, "stale: expired before owner lookup"
        await self.db.update_listing(l)
        return
    try:
        found = await asyncio.wait_for(self._lookup_owner(l), timeout=min(180.0, remaining))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        found = False
        self.stats["lookup_exception"] = self.stats.get("lookup_exception", 0) + 1
        log.warning("Owner lookup failed for %s: %s", l.listing_id, type(exc).__name__)
    l.lookup_done = True
    if not found:
        l.owner_not_found = True
        l.status, l.reason = ListingStatus.FILTERED_SELLER, "owner not verified for this NFT"
        l.seller_filter_result = "OWNER_NOT_FOUND"
        self.stats["owner_not_found"] = self.stats.get("owner_not_found", 0) + 1
        self.stats["filtered_seller"] += 1
        await self.db.update_listing(l)
        return
    if settings.require_username and not normalize_username(l.seller_username):
        await self._reject_no_username(l)
        return
    try:
        # The original timestamps survive lookup and seller retries.
        await self.process(ListingEvent(l, l.t_found or time.monotonic(), "lookup"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        l.status, l.reason = ListingStatus.ERROR, "processing after lookup: " + type(exc).__name__
        log.warning("Processing after lookup failed for %s: %s", l.listing_id, type(exc).__name__)
        await self.db.update_listing(l)
''', "ListingPipeline")
function(queue, "_still_on_sale", '''
async def _still_on_sale(self, l: Listing) -> bool | None:
    """Revalidate exact gift, primary price, owner and current username, not just resell_amount."""
    from telethon import types
    from telethon.tl.functions.payments import GetUniqueStarGiftRequest
    from tracker.marketplace import parse_price, _owner
    from tracker.lookup import same_name
    link = listing_gift_link(l)
    if not link:
        return False
    async def check(client):
        response = await client(GetUniqueStarGiftRequest(slug=link.rsplit("/", 1)[-1]))
        gift = getattr(response, "gift", None)
        if gift is None:
            return None
        if getattr(gift, "id", None) != l.gift_unique_id or not getattr(gift, "resell_amount", None):
            return False
        if gift_link(getattr(gift, "slug", None)) != link:
            return False
        stars, ton = parse_price(gift.resell_amount)
        if bool(getattr(gift, "resale_ton_only", False)) != bool(l.ton_only):
            return False
        if l.ton_only or l.stars_price is None:
            if ton != l.ton_nano:
                return False
        elif stars != l.stars_price:
            return False
        key, kind, uid = _owner(gift)
        if kind is not None and (kind != l.seller_type or uid != l.seller_id):
            return False
        if kind is None:
            owner_name = getattr(gift, "owner_name", None)
            if not owner_name or not same_name(owner_name, l.seller_name):
                return None if not owner_name else False
        current = next((u for u in getattr(response, "users", None) or []
                        if getattr(u, "id", None) == l.seller_id and not getattr(u, "min", False)), None)
        if current is None and l.seller_type == "user" and l.seller_id and l.seller_id > 0:
            current = await client.get_entity(types.PeerUser(l.seller_id))
        if current is not None:
            if getattr(current, "bot", False) or getattr(current, "deleted", False):
                return False
            l.seller_username = normalize_username(_username_of(current))
            l.seller_profile_link = username_link(l.seller_username)
        return True
    try:
        result, _ = await self.sessions.run(check, prefer=l.session_source, attempts=1, timeout=10, priority=True)
        return result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.debug("Sale validation unavailable for %s: %s", l.listing_id, type(exc).__name__)
        return None
''', "ListingPipeline")
replace(queue, "        if not uid:\n            log.warning(\"Owner lookup returned uid=0", "        if not uid or (settings.strict_owner_verification and (uid < 0 or not owner.session)):\n            log.warning(\"Owner lookup returned unverified uid")
source = get(queue)
start = source.index("            t_checked = time.monotonic()", source.index("    async def _send_stage("))
end = source.index("            if l.t_detect is not None:", start)
source = source[:start] + '''            limit = age_limit(settings, lookup=ev.mode == "lookup" or bool(l.owner_found_via))
            if limit > 0 and age_seconds(l) >= limit:
                raise _Stale("stale: seller check exceeded delivery deadline")
            l.gift_link = listing_gift_link(l)
            if not l.gift_link:
                raise _Stale("invalid or missing NFT link; not sent")
            if settings.lookup_check_onsale and (settings.strict_owner_verification
                    or age_seconds(l) >= settings.verify_sale_after_seconds):
                current = await self._still_on_sale(l)
                if current is not True:
                    raise _Stale("sale/price/owner changed or current state unavailable; not sent")
            if settings.require_username and not normalize_username(l.seller_username):
                await self._reject_no_username(l)
                return
            if limit > 0 and age_seconds(l) >= limit:
                raise _Stale("stale: final validation exceeded delivery deadline")
            t_checked = time.monotonic()
''' + source[end:]
source = source.replace('                                                  priority=self._is_expensive(l))',
    '                                                  priority=self._is_expensive(l),\n                                                  expires_at=expires_at(l, limit))')
put(queue, source)

# Freshness: do not promote uncertain old lots using collection count or weak-name changes.
coord = "tracker/coordinator.py"
replace(coord, '        fast = settings.sync_mode == "fast"\n', '        fast = settings.sync_mode == "fast" and not settings.strict_new_lots\n')
replace(coord, '        max_pages = settings.fast_sync_pages if fast else (settings.initial_sync_pages or 10**9)',
    '        max_pages = settings.fast_sync_pages if fast else (10**9 if settings.strict_new_lots else (settings.initial_sync_pages or 10**9))')
replace(coord, '            self.states[st.gift_id] = st\n', '''            if settings.strict_new_lots and not await self.db.kv_get(f"strict_baseline_v1:{st.gift_id}"):
                st.synced, st.complete, st.needs_full_sync = False, False, True
            self.states[st.gift_id] = st
''')
replace(coord, '        st.synced = True\n', '''        if settings.strict_new_lots:
            # Missing rows are not evidence of new listings, even when count increased.
            partial = len(seen) < count
            complete = reached_end
        st.synced = True
''')
replace(coord, '        await self.db.save_collection_state(st.to_row())\n        self._dirty.discard(gid)',
    '        await self.db.save_collection_state(st.to_row())\n        if settings.strict_new_lots and complete and not partial:\n            await self.db.kv_set(f"strict_baseline_v1:{gid}", "1")\n        self._dirty.discard(gid)')
replace(coord, '        if (cls is not None and not cls.decided and settings.sync_mode == "fast" and st.complete',
    '        if (not settings.strict_new_lots and cls is not None and not cls.decided and settings.sync_mode == "fast" and st.complete')
replace(coord, '        if (st.complete and not st.partial and not was_baseline and count is not None',
    '        if (not settings.strict_new_lots and st.complete and not st.partial and not was_baseline and count is not None')
replace(coord, '        if (st.complete and not was_baseline and count is not None and st.last_count is not None',
    '        if (not settings.strict_new_lots and st.complete and not was_baseline and count is not None and st.last_count is not None')
replace(coord, '        if st.complete and st.last_cursor and not was_baseline and cls.silent:',
    '        if not settings.strict_new_lots and st.complete and st.last_cursor and not was_baseline and cls.silent:')
replace(coord, '        if cls.new and st.partial and settings.sync_mode == "full":', '''        if settings.strict_new_lots and st.partial and cls.new:
            cls.silent += [(it, "incomplete_baseline_unverified") for it in cls.new]
            self.stats["silent"] += len(cls.new)
            cls.new = []
        if cls.new and st.partial and settings.sync_mode == "full":''')

# Sender checks deadline after queue/rate-limit/retry waits as well as before first submission.
sender = "bot/sender.py"
replace(sender, '                           priority: bool = False) -> SendResult:',
    '                           priority: bool = False, expires_at: float | None = None) -> SendResult:')
replace(sender, '                                  priority=priority)\n', '                                  priority=priority, expires_at=expires_at)\n')
replace(sender, '                               fallback_markup, thread_id=thread, priority=priority)',
    '                               fallback_markup, thread_id=thread, priority=priority, expires_at=expires_at)')
replace(sender, '                   thread_id: int | None = None, priority: bool = False) -> SendResult:',
    '                   thread_id: int | None = None, priority: bool = False,\n                   expires_at: float | None = None) -> SendResult:')
replace(sender, '        # Telegram message limit', '''        def expired():
            return expires_at is not None and time.time() >= expires_at
        def bounded_wait(seconds):
            return min(seconds, max(0.0, expires_at - time.time())) if expires_at is not None else seconds
        if expired():
            return SendResult(False, error="stale: delivery deadline expired")
        # Telegram message limit''')
source = get(sender)
source = source.replace('LinkPreviewOptions(url=preview_url, prefer_small_media=True, show_above_text=False)',
    'LinkPreviewOptions(url=preview_url, is_disabled=False, prefer_large_media=True, show_above_text=False)')
source = source.replace('        while True:\n            w = self._window_wait(chat_id)',
    '        while True:\n            if expired():\n                return SendResult(False, error="stale: delivery deadline expired")\n            w = self._window_wait(chat_id)', 1)
source = source.replace('await asyncio.sleep(w)', 'await asyncio.sleep(bounded_wait(w))')
source = source.replace('            while attempt + 1 < attempts:\n                attempt += 1',
    '            while attempt + 1 < attempts:\n                if expired():\n                    return SendResult(False, error="stale: delivery deadline expired")\n                attempt += 1')
source = source.replace('                    await asyncio.sleep(wait)\n                try:',
    '                    await asyncio.sleep(bounded_wait(wait))\n                if expired():\n                    return SendResult(False, error="stale: delivery deadline expired")\n                try:')
source = source.replace('                    while True:\n                        w = self._window_wait(chat_id)',
    '                    while True:\n                        if expired():\n                            return SendResult(False, error="stale: delivery deadline expired")\n                        w = self._window_wait(chat_id)')
source = source.replace('                    await asyncio.sleep(delay)', '                    await asyncio.sleep(bounded_wait(delay))')
put(sender, source)

# Fast acknowledgement and visible errors for matched command/button handlers. No secret inputs logged.
put("bot/reliability.py", '''"""Keep slow control-panel handlers observable without retrying mutations."""
from __future__ import annotations
import asyncio
import logging
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

log = logging.getLogger(__name__)


class ResponsiveHandlers(BaseMiddleware):
    def __init__(self):
        self.active = set()

    async def __call__(self, handler, event, data):
        callback = isinstance(event, CallbackQuery)
        message = event.message if callback else event
        if not isinstance(message, Message):
            return await handler(event, data)
        # Only commands/control buttons are deduplicated, never passwords/codes/FSM inputs.
        text = getattr(event, "data", "") if callback else (message.text or "")
        control = callback or text.startswith("/") or text in {
            "📊 Статус", "⏸ Пауза", "▶️ Продолжить", "👥 Аккаунты", "➕ Добавить аккаунт",
            "📈 Статистика", "🕘 Последние лоты", "📚 Коллекции"}
        if not control:
            return await handler(event, data)
        uid = event.from_user.id if event.from_user else 0
        key = (uid, message.chat.id, text, message.message_id if callback else 0)
        async def notice(text=None):
            try:
                if callback:
                    await event.answer(text or "")
                else:
                    await message.answer(text or "Принято, выполняю. Повторять команду не нужно.")
            except Exception:
                pass
        if key in self.active:
            await notice("Команда уже выполняется, повторять не нужно.")
            return None
        self.active.add(key)
        async def delayed_notice():
            await asyncio.sleep(0.8 if callback else 1.5)
            await notice()
        task = asyncio.create_task(delayed_notice())
        try:
            return await handler(event, data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Control handler failed: %s", type(exc).__name__)
            await notice("Не удалось выполнить команду. Трекер продолжает работать; ошибка записана в журнал.")
            return None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.active.discard(key)
''')
replace("main.py", '        self.dp = Dispatcher(storage=MemoryStorage())\n', '''        self.dp = Dispatcher(storage=MemoryStorage())
        # reliability-fix-v1
        from bot.reliability import ResponsiveHandlers
        responsive = ResponsiveHandlers()
        self.dp.message.middleware(responsive)
        self.dp.callback_query.middleware(responsive)
''')
function("main.py", "_notify_bot_dead", '''
async def _notify_bot_dead(self, bot: str, next_bot: str | None) -> None:
    targets = set(settings.admin_ids or set()) | set(all_admins())
    text = (f"⚠️ Бот поиска @{html.escape(bot)} временно не отвечает или недоступен.\\n"
            "Это может быть сбой, переименование или удаление; причина не подтверждена.\\n"
            "Трекер продолжает работу. Основной бот не изменён, аккаунты не отключаются. "
            "Лоты без подтверждённого владельца и @username не публикуются.")
    for chat in sorted(targets):
        try:
            await self.bot.send_message(chat, text)
        except Exception as exc:
            log.debug("Lookup warning delivery failed: %s", type(exc).__name__)
''', "App")
replace("main.py", '        self.sessions.limiter.set_rate(settings.global_rps)', '''        if not await self._flag("migr_reliability_v1"):
            from config import set_runtime_value
            for key, value in {
                "REQUIRE_USERNAME": "true", "LOOKUP_REQUIRED": "true",
                "STRICT_OWNER_VERIFICATION": "true", "LOOKUP_TRUST_NAME_MATCH": "false",
                "LOOKUP_LANG_FILTER": "true", "STRICT_NEW_LOTS": "true",
                "START_FRESH": "true", "MAX_LOT_AGE": "180", "LOOKUP_MAX_AGE": "180",
                "SELLER_CACHE_TTL": "60", "PREMIUM_OVER_FLOOR_PERCENT": "333",
                "RARE_ATTRIBUTE_PERCENT": "1.5", "LOOKUP_CHECK_ONSALE": "true",
            }.items():
                set_runtime_value(key, value)
            await self._set_flag("migr_reliability_v1")
        self.sessions.limiter.set_rate(settings.global_rps)''')

# All replacements and syntax checks succeeded; now write the complete patch.
for path, source in changes.items():
    destination = ROOT / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(source, encoding="utf-8")
print("Applied reliability source patch:", ", ".join(sorted(changes)))
