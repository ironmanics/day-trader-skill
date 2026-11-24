import os
import shutil
import tempfile
import time
import unittest
import urllib.error

from _load import load

facts = load("trader-analyst", "facts")
news = load("trader-news", "news")
sg = facts.sec_guard
UA = "Test Person test@example.org"


class Clock(object):
    def __init__(self, now=1000.0):
        self.now, self.slept = now, []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.slept.append(round(s, 3))


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.clock = Clock()

    def tearDown(self):
        shutil.rmtree(self.d)

    def guard(self, **kw):
        kw.setdefault("clock", self.clock)
        kw.setdefault("sleep", self.clock.sleep)
        return sg.Guard(state_dir=self.d, **kw)


class Shared(unittest.TestCase):
    def test_analyst_and_news_share_one_guard_module(self):
        self.assertIs(facts.sec_guard, news.sec_guard)


class Pacing(Base):
    def test_slots_are_spaced_by_the_pause(self):
        g = self.guard(pause=0.25)
        for _ in range(3):
            g.wait()
        self.assertEqual(self.clock.slept, [0.25, 0.5])

    def test_two_guards_share_one_clock(self):
        a, b = self.guard(pause=0.25), self.guard(pause=0.25)
        a.wait()
        b.wait()
        a.wait()
        self.assertEqual(self.clock.slept, [0.25, 0.5])

    def test_a_clock_that_went_backwards_does_not_park_us(self):
        with open(os.path.join(self.d, "sec-last.txt"), "w") as fh:
            fh.write("999999999.0")
        self.guard(pause=0.25).wait()
        self.assertEqual(self.clock.slept, [0.25])

    def test_the_default_stays_well_under_ten_a_second(self):
        self.assertGreaterEqual(sg.PAUSE, 0.2)


class Breaker(Base):
    def test_block_starts_a_cooldown_every_guard_honours(self):
        with self.assertRaises(sg.SecStop):
            self.guard(cooldown=900).blocked(403)
        with self.assertRaises(sg.SecStop) as cm:
            self.guard().check()
        self.assertIn("403", str(cm.exception))
        self.clock.now += 901
        self.guard().check()

    def test_retry_after_wins_when_longer(self):
        with self.assertRaises(sg.SecStop):
            self.guard(cooldown=900).blocked(429, retry_after=3600)
        self.clock.now += 1800
        with self.assertRaises(sg.SecStop):
            self.guard().check()
        self.clock.now += 1900
        self.guard().check()

    def test_only_403_and_429_trip_it(self):
        g = self.guard()
        for code in (404, 500, 503):
            g.after_error(urllib.error.HTTPError("u", code, "x", {}, None))
        for code in (403, 429):
            with self.assertRaises(sg.SecStop):
                g.after_error(urllib.error.HTTPError("u", code, "x", {"Retry-After": "1200"}, None))
            os.remove(os.path.join(self.d, "sec-blocked.json"))

    def test_retry_after_parsing(self):
        self.assertEqual(sg.parse_retry_after("120"), 120)
        for bad in (None, "", "abc", "0", "-5", "999999", "Wed, 21 Oct 2026 07:28:00 GMT"):
            self.assertIsNone(sg.parse_retry_after(bad), bad)

    def test_a_blocked_before_sends_nothing_and_spends_nothing(self):
        g = self.guard(budget=5)
        with self.assertRaises(sg.SecStop):
            g.blocked(403)
        with self.assertRaises(sg.SecStop):
            g.before()
        self.assertEqual(g.spent, 0)
        self.assertEqual(self.clock.slept, [])


class Budget(Base):
    def test_budget_runs_out(self):
        g = self.guard(budget=2)
        g.spend()
        g.spend()
        with self.assertRaises(sg.SecStop) as cm:
            g.spend()
        self.assertIn("budget of 2", str(cm.exception))

    def test_no_budget_means_no_cap(self):
        g = self.guard()
        for _ in range(500):
            g.spend()


class Cache(Base):
    def test_round_trip_and_keys(self):
        g = self.guard(clock=time.time)
        g.cache_put("https://x/a", b'{"a": 1}')
        g.cache_put("https://x/b", b'{"b": 2}')
        self.assertEqual(g.cache_get("https://x/a"), b'{"a": 1}')
        self.assertEqual(g.cache_get("https://x/b"), b'{"b": 2}')
        self.assertIsNone(g.cache_get("https://x/c"))

    def test_expired_and_corrupt_entries_are_misses(self):
        g = self.guard(clock=time.time, ttl=3600)
        g.cache_put("https://x/a", b"data")
        self.assertIsNone(self.guard(clock=lambda: time.time() + 7200, ttl=3600).cache_get("https://x/a"))
        with open(g._cache_file("https://x/a"), "wb") as fh:
            fh.write(b"not gzip")
        self.assertIsNone(g.cache_get("https://x/a"))


class FakeResp(object):
    headers = {}

    def __init__(self, body):
        self.body = body

    def read(self, n=-1):
        return self.body[:n] if n >= 0 else self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Wiring(Base):
    def setUp(self):
        Base.setUp(self)
        self.calls = 0
        self.saved = facts.urllib.request.urlopen

    def tearDown(self):
        facts.urllib.request.urlopen = self.saved
        Base.tearDown(self)

    def serve(self, *answers):
        """Each call returns the next answer: bytes, or an HTTP status int to raise."""
        it = iter(answers)

        def fake(req, timeout=None):
            self.calls += 1
            a = next(it)
            if isinstance(a, int):
                raise urllib.error.HTTPError(req.full_url, a, "x", {}, None)
            return FakeResp(a)
        facts.urllib.request.urlopen = fake

    def test_facts_403_stops_everything_and_sends_no_more(self):
        self.serve(403, b"{}")
        f = facts.Fetcher(ua=UA, guard=self.guard(pause=0))
        with self.assertRaises(facts.SecStopped):
            f.json("https://data.sec.gov/a", "a")
        with self.assertRaises(facts.SecStopped):
            f.json("https://data.sec.gov/b", "b")
        self.assertEqual(self.calls, 1)

    def test_facts_other_errors_do_not_trip_the_breaker(self):
        self.serve(404, b'{"ok": 1}')
        f = facts.Fetcher(ua=UA, guard=self.guard(pause=0))
        with self.assertRaises(facts.FactsError) as cm:
            f.json("https://data.sec.gov/a", "a")
        self.assertNotIsInstance(cm.exception, facts.SecStopped)
        self.assertEqual(f.json("https://data.sec.gov/b", "b"), {"ok": 1})

    def test_facts_cache_means_a_repeat_sends_nothing(self):
        self.serve(b'{"v": 1}', b'{"v": 2}')
        f = facts.Fetcher(ua=UA, guard=self.guard(pause=0, clock=time.time))
        self.assertEqual(f.json("https://data.sec.gov/a", "a", cache=True), {"v": 1})
        self.assertEqual(f.json("https://data.sec.gov/a", "a", cache=True), {"v": 1})
        self.assertEqual(f.json("https://data.sec.gov/a", "a"), {"v": 2})  # no cache flag: live again
        self.assertEqual(self.calls, 2)

    def test_facts_budget_stops_live_requests_but_not_cached_ones(self):
        self.serve(b'{"v": 1}', b'{"v": 2}')
        f = facts.Fetcher(ua=UA, guard=self.guard(pause=0, budget=1, clock=time.time))
        f.json("https://data.sec.gov/a", "a", cache=True)
        self.assertEqual(f.json("https://data.sec.gov/a", "a", cache=True), {"v": 1})
        with self.assertRaises(facts.SecStopped):
            f.json("https://data.sec.gov/b", "b", cache=True)
        self.assertEqual(self.calls, 1)

    def test_news_sec_calls_use_the_guard_and_yahoo_calls_do_not(self):
        self.serve(b"<rss/>", b"<feed/>", 429, b"<feed/>")
        g = self.guard(pause=0)
        f = news.Fetcher(sec_ua=UA, pause=0, guard=g)
        self.assertEqual(f.get("https://feeds.finance.yahoo.com/x", "x"), b"<rss/>")
        self.assertEqual(g.spent, 0)
        self.assertEqual(f.get("https://www.sec.gov/a", "a", sec=True), b"<feed/>")
        self.assertEqual(g.spent, 1)
        with self.assertRaises(news.SecStopped):
            f.get("https://www.sec.gov/b", "b", sec=True)
        with self.assertRaises(news.SecStopped):
            f.get("https://www.sec.gov/c", "c", sec=True)
        self.assertEqual(self.calls, 3)
        self.assertEqual(f.get("https://feeds.finance.yahoo.com/y", "y"), b"<feed/>")  # Yahoo still works

    def test_news_collect_stops_sec_once_and_keeps_headlines(self):
        self.serve(b"<rss><channel/></rss>", b"<rss><channel/></rss>", 403)
        f = news.Fetcher(sec_ua=UA, pause=0, guard=self.guard(pause=0))
        state = {"ciks": {"AAA": "0000000001", "BBB": "0000000002"}}
        errors = []
        news.collect(f, ["AAA", "BBB"], state, errors=errors)
        self.assertEqual(self.calls, 3)  # two headline feeds, one SEC call, then nothing
        self.assertEqual(sum("stopped" in e for e in errors), 1)


if __name__ == "__main__":
    unittest.main()
