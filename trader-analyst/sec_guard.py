#!/usr/bin/env python3
"""Shared SEC request guard for trader-analyst and trader-news. Standard library only.

SEC EDGAR asks automated clients to stay under 10 requests a second and blocks
the ones that do not. This keeps both tools well under that, in every process:

  pacing       one request slot every PAUSE seconds (0.25, so 4 a second), reserved in a
               small lock-protected file, so two skills running at once share one clock
  breaker      an HTTP 403 or 429 stops every SEC call at once and starts a cooldown
               (15 minutes, or longer if the SEC says Retry-After); calls refuse until it ends
  budget       an optional cap on live requests for one run
  cache        gzip copies of JSON answers kept for 24 hours, so a repeat run sends nothing

State lives in trader/state/ (sec-last.txt, sec-blocked.json, sec-cache/). Delete
sec-blocked.json to end a cooldown early; do that only if you know the block is over.
"""

import gzip
import hashlib
import json
import os
import time
import zlib

try:
    import fcntl
except ImportError:  # Windows: pacing still works inside one process, not across processes
    fcntl = None

PAUSE = 0.25
COOLDOWN = 900
CACHE_TTL = 24 * 3600
STATE_DIR = os.path.join("trader", "state")
BLOCK_CODES = (403, 429)


class SecStop(Exception):
    """SEC calls are stopped (cooldown or budget). Nothing was sent."""


def parse_retry_after(value):
    """Seconds from a Retry-After header, or None. Dates are ignored: the cooldown covers them."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 < n <= 24 * 3600 else None


class Guard(object):
    def __init__(self, state_dir=STATE_DIR, pause=PAUSE, cooldown=COOLDOWN, ttl=CACHE_TTL,
                 budget=None, clock=time.time, sleep=time.sleep):
        self.state_dir, self.pause, self.cooldown, self.ttl = state_dir, pause, cooldown, ttl
        self.budget, self.clock, self.sleep, self.spent = budget, clock, sleep, 0

    def _path(self, *parts):
        return os.path.join(self.state_dir, *parts)

    # -- breaker
    def check(self):
        try:
            with open(self._path("sec-blocked.json"), encoding="utf-8") as fh:
                info = json.load(fh)
            until = float(info["until"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        if until > self.clock():
            raise SecStop("SEC answered HTTP %s earlier; cooling down until %s, no request sent"
                          % (info.get("code"), time.strftime("%H:%M", time.localtime(until))))

    def blocked(self, code, retry_after=None):
        secs = max(self.cooldown, retry_after or 0)
        now = self.clock()
        os.makedirs(self.state_dir, exist_ok=True)
        with open(self._path("sec-blocked.json"), "w", encoding="utf-8") as fh:
            json.dump({"until": now + secs, "code": code, "at": now}, fh)
            fh.write("\n")
        raise SecStop("SEC answered HTTP %s: stopped, no more SEC requests for %d minutes"
                      % (code, (secs + 59) // 60))

    def after_error(self, exc):
        """Call with a urllib HTTPError. Raises SecStop for 403 and 429, returns for anything else."""
        if exc.code in BLOCK_CODES:
            hdrs = getattr(exc, "headers", None)
            self.blocked(exc.code, parse_retry_after(hdrs.get("Retry-After") if hdrs else None))

    # -- budget
    def spend(self):
        if self.budget is not None and self.spent >= self.budget:
            raise SecStop("SEC request budget of %d used up for this run; run again to continue "
                          "from the cache" % self.budget)
        self.spent += 1

    # -- pacing
    def wait(self):
        os.makedirs(self.state_dir, exist_ok=True)
        with open(self._path("sec-last.txt"), "a+", encoding="utf-8") as fh:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_EX)
            fh.seek(0)
            try:
                last = float(fh.read().strip() or 0)
            except ValueError:
                last = 0.0
            now = self.clock()
            if last > now + 60:  # a clock that went backwards must not park us for an hour
                last = now
            slot = max(now, last + self.pause)
            fh.seek(0)
            fh.truncate()
            fh.write(repr(slot))
        if slot > now:
            self.sleep(slot - now)

    def before(self):
        """Every live request goes through this first: breaker, budget, pacing."""
        self.check()
        self.spend()
        self.wait()

    # -- cache
    def _cache_file(self, url):
        return self._path("sec-cache", hashlib.sha256(url.encode("utf-8")).hexdigest()[:32] + ".gz")

    def cache_get(self, url):
        path = self._cache_file(url)
        try:
            if self.clock() - os.path.getmtime(path) > self.ttl:
                return None
            with open(path, "rb") as fh:
                return gzip.decompress(fh.read())
        except (OSError, EOFError, ValueError, zlib.error):
            return None

    def cache_put(self, url, data):
        path = self._cache_file(url)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "wb") as fh:
            fh.write(gzip.compress(data))
        os.replace(tmp, path)
