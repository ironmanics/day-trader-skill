import gzip
import io
import unittest
import urllib.error

from _load import load

ptr = load("trader-pelosi", "ptr")
news = load("trader-news", "news")
facts = load("trader-analyst", "facts")
alerts = load("trader-alerts", "alerts")
entry = load("trader-entry", "entry")
scan = load("trader-scanner", "scan")

BOMB_XML = (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;&a;</x>')


class Pelosi(unittest.TestCase):
    def test_doc_id_must_be_digits(self):
        for bad in ("../../etc/passwd", "2003/../x", "1;rm", "", "12345678901234"):
            with self.assertRaises(ptr.PtrError, msg=bad):
                ptr._fetch_and_read(bad, 2026, "unused")

    def test_doctype_refused(self):
        with self.assertRaises(ptr.PtrError):
            ptr.parse_index(BOMB_XML, 2026)


class News(unittest.TestCase):
    def test_doctype_refused(self):
        with self.assertRaises(news.NewsError):
            news.parse_rss(BOMB_XML, "TSLA")
        with self.assertRaises(news.NewsError):
            news.parse_sec_atom(BOMB_XML, "TSLA")


class Caps(unittest.TestCase):
    def test_read_capped(self):
        for mod in (ptr, news, facts, alerts, entry, scan):
            old = mod.MAX_BYTES
            mod.MAX_BYTES = 10
            try:
                self.assertEqual(mod._read_capped(io.BytesIO(b"x" * 10)), b"x" * 10)
                with self.assertRaises(urllib.error.URLError):
                    mod._read_capped(io.BytesIO(b"x" * 11))
            finally:
                mod.MAX_BYTES = old

    def test_gunzip_capped(self):
        blob = gzip.compress(b"a" * 1000)
        for mod in (news, facts):
            old = mod.MAX_BYTES
            mod.MAX_BYTES = 100
            try:
                with self.assertRaises(urllib.error.URLError):
                    mod._gunzip_capped(blob)
            finally:
                mod.MAX_BYTES = old
            self.assertEqual(mod._gunzip_capped(blob), b"a" * 1000)


class Tickers(unittest.TestCase):
    def test_symbols_are_quoted_for_urls(self):
        self.assertEqual(entry.yahoo_symbol("brk.b"), "BRK-B")
        self.assertEqual(scan.to_yahoo("BRK.B"), "BRK-B")
        self.assertEqual(news.yahoo_symbol("AAPL&x=1/../y"), "AAPL%26X%3D1%2F--%2FY")
        self.assertIn("/chart/A%26B%3D1?", alerts.yahoo_url("A&B=1"))
        self.assertIn("?s=a%26b%3d1.us&", alerts.stooq_url("a&b=1").lower())


if __name__ == "__main__":
    unittest.main()
