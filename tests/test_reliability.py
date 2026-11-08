"""Offline regression tests: no Telegram connections, production tokens or live messages."""
from __future__ import annotations
import asyncio
import copy
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from telethon import types
from config import settings
from database.db import Database
from database.models import Listing, ListingStatus
from filters.price_filter import PriceFilter
from bot.formatter import MessageFormatter, listing_keyboard
from bot.sender import TelegramSender, SendResult
from tracker.lookup import OwnerLookup, Candidate, FoundOwner
from tracker.queue import ListingPipeline, ListingEvent
from tracker.reliability import age_seconds
from tracker.state import CollectionState, Classification
from tracker.coordinator import MarketplaceCoordinator
from tracker.marketplace import fetch_gift_owner, ResaleItem, ResalePage
from utils.links import gift_link, listing_gift_link, normalize_username


class SettingsMixin:
    def setUp(self):
        self.old = copy.deepcopy(vars(settings))
        settings.strict_owner_verification = True
        settings.strict_new_lots = True
        settings.require_username = True
        settings.lookup_required = True
        settings.lookup_check_onsale = True
        settings.lookup_trust_name_match = True  # strict mode must defeat legacy trust settings
        settings.lookup_name_check = True
        settings.lookup_max_candidates = 3
        settings.lookup_nomatch_check = 1
        settings.max_lot_age = 180
        settings.lookup_max_age = 180
        settings.min_stars = settings.collection_min_floor = 0
        settings.min_ton = 0
        settings.max_above_floor = 500
        settings.premium_over_floor_percent = 333
        settings.rare_attribute_percent = 1.5
        settings.allow_ton_only = True
        settings.unique_gifts = settings.unique_sellers = False
        settings.max_same_collection_streak = 0
        settings.send_interval = 0
        settings.test_mode = False
        settings.lookup_bot_username = "lookupbot"
        settings.lookup_bots = []
        settings.lookup_sessions = []
        settings.lookup_interval = 0
        settings.auto_lookup = True

    def tearDown(self):
        for name, value in self.old.items():
            setattr(settings, name, value)


def lot(**values):
    defaults = dict(listing_id="1:u100", gift_unique_id=1, gift_id=10, gift_name="Gift",
                    gift_slug="Gift-1", gift_link="https://t.me/nft/Gift-1", seller_type="user",
                    seller_id=100, seller_key="u100", seller_name="Seller", seller_username="current_user",
                    seller_profile_link="https://t.me/current_user", stars_price=1000, floor_stars=1000)
    defaults.update(values)
    return Listing(**defaults)


def user(username="current_user", uid=100):
    return types.User(id=uid, first_name="Seller", username=username, access_hash=1)


class Sessions:
    def __init__(self, client=None):
        self.client = client
        self.workers = {"s1": NS(name="s1", is_usable=lambda: True, speed=1.0)}
    def get(self, name):
        return self.workers.get(name)
    def usable(self, include_reserved=False):
        return list(self.workers.values())
    async def run(self, fn, **kwargs):
        return await fn(self.client), "s1"


def pipeline():
    db = NS(update_listing=AsyncMock(), gift_already_sent=AsyncMock(return_value=False),
            seller_already_sent=AsyncMock(return_value=False))
    sender = NS(send_listing=AsyncMock(return_value=SendResult(True, 123)))
    formatter = NS(render=Mock(return_value="Verified lot"))
    p = ListingPipeline(db, Sessions(), sender, formatter, PriceFilter(), Mock())
    p._repeat_reason = AsyncMock(return_value=None)
    p._streak_reason = Mock(return_value=None)
    return p


class PriceAndLinks(SettingsMixin, unittest.TestCase):
    def test_333_percent_boundary(self):
        self.assertTrue(PriceFilter().check(lot(stars_price=4329)).passed)
        self.assertFalse(PriceFilter().check(lot(stars_price=4330)).passed)
        self.assertFalse(PriceFilter().check(lot(stars_price=5000)).passed)
    def test_rare_model_exception(self):
        self.assertTrue(PriceFilter().check(lot(stars_price=7000, model_rarity="1.5%")).passed)
    def test_rare_background_exception(self):
        self.assertTrue(PriceFilter().check(lot(stars_price=7000, backdrop_rarity="редкая")).passed)
    def test_pattern_alone_is_not_exception(self):
        self.assertFalse(PriceFilter().check(lot(stars_price=4330, symbol_rarity="0.1%")).passed)
    def test_monochrome_exception(self):
        self.assertTrue(PriceFilter().check(lot(stars_price=7000, backdrop="Monochrome")).passed)
    def test_bad_rarity_does_not_bypass(self):
        for rarity in (None, "", "unknown", "nan%", "-1%", "0%", "2%"):
            with self.subTest(rarity=rarity):
                self.assertFalse(PriceFilter().check(lot(stars_price=4330, model_rarity=rarity)).passed)
    def test_ton_only_uses_same_premium_rule(self):
        self.assertFalse(PriceFilter().check(lot(stars_price=4330, ton_nano=10**9, ton_only=True)).passed)
    def test_positive_api_prices_required(self):
        self.assertFalse(PriceFilter().check(lot(stars_price=0)).passed)
        self.assertFalse(PriceFilter().check(lot(ton_only=True, ton_nano=None)).passed)
    def test_reconstruct_link_from_api_slug(self):
        l = lot(gift_link=None)
        self.assertEqual(listing_gift_link(l), "https://t.me/nft/Gift-1")
        urls = [b.url for row in listing_keyboard(l).inline_keyboard for b in row if b.url]
        self.assertIn("https://t.me/nft/Gift-1", urls)
    def test_url_not_invented_from_title(self):
        self.assertIsNone(listing_gift_link(lot(gift_slug=None, gift_link=None)))
    def test_malformed_links_rejected(self):
        for value in ("https://evil.example/nft/Gift-1", "Gift-0", "Gift-1/evil", "Gift-1?evil", "a\n1"):
            self.assertIsNone(gift_link(value))
        self.assertEqual(gift_link(" https://t.me/nft/Gift-1 "), "https://t.me/nft/Gift-1")
    def test_offer_contains_canonical_nft_link(self):
        from urllib.parse import parse_qs, urlsplit
        settings.trade_bot_username = "tradebot"
        keyboard = listing_keyboard(lot(gift_link=None))
        write = next(b for row in keyboard.inline_keyboard for b in row if b.text == "Написать")
        text = parse_qs(urlsplit(write.url).query)["text"][0]
        self.assertIn("https://t.me/nft/Gift-1", text)
        self.assertTrue(write.url.startswith("https://t.me/current_user?"))
    def test_fallback_does_not_masquerade_trade_bot_as_seller(self):
        settings.trade_bot_username = "tradebot"
        buttons = [b for row in listing_keyboard(lot(), with_seller=False).inline_keyboard for b in row]
        self.assertFalse(any(b.text == "Написать" for b in buttons))
        self.assertTrue(any(b.text == "Лот" for b in buttons))
    def test_raw_nft_url_survives_no_preview(self):
        f = MessageFormatter.__new__(MessageFormatter)
        f.template = "{gift_title_link}"
        text = f.render(lot(gift_link=None))
        self.assertIn("\n🔗 NFT: https://t.me/nft/Gift-1", text)
    def test_username_validation(self):
        self.assertEqual(normalize_username("@current_user"), "current_user")
        self.assertIsNone(normalize_username("user?start=evil"))
    def test_age_uses_original_time(self):
        l = lot(first_seen=(datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat())
        l.t_found = time.monotonic()
        self.assertGreaterEqual(age_seconds(l), 199)


class PipelineTests(SettingsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_lookup_timeout_never_posts_without_owner(self):
        p = pipeline()
        p._lookup_owner = AsyncMock(side_effect=TimeoutError)
        l = lot(seller_type=None, seller_id=None, seller_username=None)
        await p._lookup_then_process(l)
        self.assertEqual(l.seller_filter_result, "OWNER_NOT_FOUND")
        p.sender.send_listing.assert_not_awaited()
    async def test_no_username_even_when_owner_found(self):
        p = pipeline()
        p._lookup_owner = AsyncMock(return_value=True)
        l = lot(seller_username=None)
        await p._lookup_then_process(l)
        self.assertEqual(l.seller_filter_result, "NO_USERNAME")
        p.sender.send_listing.assert_not_awaited()
    async def test_lookup_mode_cannot_reset_age(self):
        p = pipeline()
        l = lot(first_seen=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())
        await p.process(ListingEvent(l, time.monotonic(), "lookup"))
        self.assertIn("stale", l.reason)
        p.sender.send_listing.assert_not_awaited()
    async def test_sale_unknown_fails_closed(self):
        p = pipeline()
        p._still_on_sale = AsyncMock(return_value=None)
        l = lot()
        await p._send_stage(ListingEvent(l, time.monotonic(), "live"), l, time.monotonic(), time.monotonic())
        p.sender.send_listing.assert_not_awaited()
        self.assertFalse(p._reserved_gifts)
    async def test_missing_username_rechecked_before_send(self):
        p = pipeline()
        p._still_on_sale = AsyncMock(return_value=True)
        l = lot(seller_username=None, owner_not_found=True)
        await p._send_stage(ListingEvent(l, time.monotonic(), "live"), l, time.monotonic(), time.monotonic())
        p.sender.send_listing.assert_not_awaited()
        self.assertEqual(l.seller_filter_result, "NO_USERNAME")
    async def test_valid_lot_sent_with_deadline(self):
        p = pipeline()
        p._still_on_sale = AsyncMock(return_value=True)
        l = lot()
        await p._send_stage(ListingEvent(l, time.monotonic(), "live"), l, time.monotonic(), time.monotonic())
        p.sender.send_listing.assert_awaited_once()
        self.assertGreater(p.sender.send_listing.call_args.kwargs["expires_at"], time.time())
        self.assertEqual(l.status, ListingStatus.SENT)
    async def test_verified_lookup_cannot_bypass_seller_filter(self):
        from filters.seller_filter import SellerResult
        p = pipeline()
        p._check_seller_found = AsyncMock(return_value=SellerResult(False, "FOREIGN_TEXT", "profile language mismatch"))
        result = await p._check_seller(lot(owner_found_via="lookupbot"))
        self.assertFalse(result.passed)
        self.assertEqual(result.code, "FOREIGN_TEXT")
    async def test_expired_sender_does_not_touch_telegram(self):
        bot = NS(send_message=AsyncMock())
        sender = TelegramSender(bot)
        result = await sender.send(1, "test", expires_at=time.time() - 1)
        self.assertFalse(result.ok)
        bot.send_message.assert_not_awaited()
    async def test_expiry_while_waiting_for_rate_limit(self):
        bot = NS(send_message=AsyncMock())
        sender = TelegramSender(bot)
        sender._flood_until[1] = time.monotonic() + 10
        result = await asyncio.wait_for(sender.send(1, "test", expires_at=time.time() + .01), .5)
        self.assertFalse(result.ok)
        bot.send_message.assert_not_awaited()


class LookupTests(SettingsMixin, unittest.IsolatedAsyncioTestCase):
    def verifier(self, has_gift):
        lk = OwnerLookup.__new__(OwnerLookup)
        lk.stats = {}
        lk._input_user = AsyncMock(return_value=types.InputPeerUser(user_id=100, access_hash=1))
        client = NS(get_entity=AsyncMock(return_value=user("new_username")))
        async def run(session, fn, **kwargs):
            return await fn(client)
        lk._run = run
        return lk
    async def test_name_match_is_not_ownership(self):
        lk = self.verifier(False)
        with patch("tracker.lookup.profile_has_gift", AsyncMock(return_value=False)):
            result = await lk._verify("s1", [Candidate(username="old_user", name="Seller")], "Seller", "Gift-1", 1)
        self.assertIsNone(result)
    async def test_current_username_after_exact_nft_verification(self):
        lk = self.verifier(True)
        with patch("tracker.lookup.profile_has_gift", AsyncMock(return_value=True)):
            result = await lk._verify("s1", [Candidate(username="old_user", name="Seller")], "Seller", "Gift-1", 1)
        self.assertEqual(result.username, "new_username")
        self.assertEqual(result.user_id, 100)
    async def test_resolve_failure_does_not_create_pseudo_id(self):
        lk = self.verifier(False)
        lk._input_user = AsyncMock(return_value=None)
        self.assertIsNone(await lk._verify("s1", [Candidate(username="old_user", name="Seller")], "Seller", "Gift-1", 1))
    async def test_mismatched_candidate_id_is_rejected(self):
        lk = self.verifier(True)
        with patch("tracker.lookup.profile_has_gift", AsyncMock(return_value=True)):
            result = await lk._verify("s1", [Candidate(username="old_user", user_id=999, name="Seller")], "Seller", "Gift-1", 1)
        self.assertIsNone(result)
    async def test_private_inventory_not_trusted(self):
        from tracker.lookup import ProfilePrivate
        lk = self.verifier(False)
        with patch("tracker.lookup.profile_has_gift", AsyncMock(side_effect=ProfilePrivate("private"))):
            result = await lk._verify("s1", [Candidate(username="old_user", name="Seller")], "Seller", "Gift-1", 1)
        self.assertIsNone(result)
    async def test_saved_owner_is_reverified(self):
        lk = OwnerLookup(Sessions())
        lk._pick_sessions = Mock(return_value=["s1"])
        lk._known = AsyncMock(return_value=[Candidate(user_id=100, username="old_user", name="Seller")])
        lk._verify = AsyncMock(return_value=FoundOwner(100, "new_username", "Seller", session="s1"))
        lk._ask_bot = AsyncMock()
        result = await lk._find("Seller", "Gift-1", 1)
        lk._verify.assert_awaited_once()
        lk._ask_bot.assert_not_awaited()
        self.assertEqual(result.username, "new_username")
    async def test_old_dead_mark_does_not_disable_primary(self):
        lk = OwnerLookup(Sessions())
        lk._bot_dead_until["lookupbot"] = time.monotonic() + 900
        self.assertEqual(lk._bot_pool(), ["lookupbot"])
        self.assertFalse(lk.all_bots_dead())
    async def test_unavailability_warns_without_changing_primary(self):
        lk = OwnerLookup(Sessions())
        lk.on_bot_dead = AsyncMock()
        for n in range(4):
            lk._register_bot_unresolved("lookupbot", f"s{n}")
        await asyncio.gather(*getattr(lk, "_notify_tasks", []))
        lk.on_bot_dead.assert_awaited_once_with("lookupbot", None)
        self.assertFalse(lk._bot_dead_until)
        self.assertEqual(settings.lookup_bot_username, "lookupbot")
    async def test_bot_history_does_not_supply_old_candidates(self):
        def message(mid, text, reply_to=None):
            return NS(id=mid, message=text, out=False, entities=None, reply_markup=None,
                      edit_date=None, reply_to_msg_id=reply_to, media=None)
        client = NS(get_input_entity=AsyncMock(return_value="lookupbot"),
                    send_message=AsyncMock(return_value=NS(id=100)),
                    get_messages=AsyncMock(return_value=[message(103, "01. Seller (@right_user)", 100),
                        message(102, "01. Seller (@wrong_reply)", 98), message(99, "01. Seller (@old_user)")]))
        lk = OwnerLookup(Sessions(client))
        lk._started.add(("s1", "lookupbot"))
        result = await asyncio.wait_for(lk._ask_bot_locked("s1", "lookupbot", "Seller"), 5)
        self.assertEqual([c.username for c in result], ["right_user"])
    async def test_gift_users_list_name_does_not_prove_ownership(self):
        class Client:
            async def __call__(self, request):
                return NS(gift=NS(owner_id=None, host_id=None, owner_name="Seller"), users=[user()])
        self.assertIsNone(await fetch_gift_owner(Client(), "Gift-1"))


class SaleTests(SettingsMixin, unittest.IsolatedAsyncioTestCase):
    async def sale(self, *, stars=1000, uid=100, username="new_username", selling=True):
        gift = NS(id=1, slug="Gift-1", resell_amount=[types.StarsAmount(amount=stars, nanos=0)] if selling else [],
                  resale_ton_only=False, owner_id=types.PeerUser(uid), host_id=None,
                  owner_name="Seller", owner_address=None)
        class Client:
            async def __call__(self, req):
                return NS(gift=gift, users=[user(username, uid)])
        p = pipeline()
        p.sessions = Sessions(Client())
        l = lot()
        return await p._still_on_sale(l), l
    async def test_price_change_is_rejected(self):
        self.assertIs((await self.sale(stars=1001))[0], False)
    async def test_owner_change_is_rejected(self):
        self.assertIs((await self.sale(uid=101))[0], False)
    async def test_sold_lot_is_rejected(self):
        self.assertIs((await self.sale(selling=False))[0], False)
    async def test_current_handle_replaces_old_handle(self):
        result, l = await self.sale()
        self.assertIs(result, True)
        self.assertEqual(l.seller_username, "new_username")
    async def test_removed_handle_clears_old_handle(self):
        result, l = await self.sale(username=None)
        self.assertIs(result, True)
        self.assertIsNone(l.seller_username)
        self.assertIsNone(l.seller_profile_link)


class CoordinatorTests(SettingsMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        await self.db.connect()
        self.items = [ResaleItem(unique_id=n, gift_id=10, title="Gift", slug=f"Gift-{n}", num=n,
                model=None, backdrop=None, symbol=None, seller_key=f"u{100+n}", owner_type="user",
                owner_id=100+n, owner_name=None, owner_address=None, stars=1000, ton_nano=None, ton_only=False)
                for n in range(150, 0, -1)]
        self.pipeline = NS(admit=AsyncMock(return_value=1))
        self.coord = MarketplaceCoordinator(self.db, Sessions(), self.pipeline)
        async def fetch(gid, offset, limit):
            start = int(offset or 0)
            end = start + limit
            return ResalePage(self.items[start:end], str(end) if end < len(self.items) else None, len(self.items)), "s1"
        self.coord._fetch = fetch
        settings.sync_mode = "fast"
        settings.fast_sync_pages = 1
        settings.initial_sync_pages = 1
        settings.sync_passes = 2
        self.st = CollectionState(gift_id=10, title="Gift")
        await self.coord.full_sync(self.st, "initial")
    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()
    async def test_strict_baseline_is_complete_not_one_page(self):
        self.assertEqual(await self.db.count_market_items(), 150)
        self.assertTrue(self.st.baseline_pending)
    async def test_old_deep_lot_price_change_not_new(self):
        await self.coord.poll_collection(self.st, "live")  # silent post-sync baseline
        old = self.items.pop()
        old.stars = 1200
        self.items.insert(0, old)
        await self.coord.poll_collection(self.st, "live")
        self.pipeline.admit.assert_not_awaited()
    async def test_count_growth_without_anchor_is_not_proof(self):
        self.st.baseline_pending = False
        c = Classification(decided=False, silent=[(self.items[0], "no_anchor")])
        count = await self.coord._apply(self.st, c, self.items[:1], "s1", "live", 151)
        self.assertEqual(count, 0)
        self.assertTrue(self.st.needs_full_sync)
        self.pipeline.admit.assert_not_awaited()
    async def test_incomplete_baseline_cannot_promote_unknown(self):
        self.st.baseline_pending = False
        self.st.partial = True
        c = Classification(decided=True, new=[self.items[0]], reason="above_anchor")
        count = await self.coord._apply(self.st, c, self.items[:1], "s1", "live", 151)
        self.assertEqual(count, 0)
        self.pipeline.admit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
