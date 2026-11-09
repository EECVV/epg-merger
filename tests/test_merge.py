import contextlib
import gzip
import io
import json
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from defusedxml.common import DefusedXmlException
import merge_epg as app


def feed(channel="c1", title="新闻 &amp; 资讯", start="20261109080000 +0800", stop="20261109090000 +0800"):
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<!DOCTYPE tv SYSTEM "xmltv.dtd"><tv><channel id="{channel}">'
            f'<display-name>综合频道</display-name></channel>'
            f'<programme channel="{channel}" start="{start}" stop="{stop}">'
            f'<title>{title}</title><desc>节目介绍</desc></programme></tv>').encode()


class MergeTests(unittest.TestCase):
    def parse(self, data, aliases=None):
        return app.parse_source(data, aliases or {})

    def test_gzip_and_utf8_xml(self):
        plain = self.parse(feed())
        zipped = self.parse(gzip.compress(feed()))
        self.assertEqual(ET.tostring(plain[1][0][1]), ET.tostring(zipped[1][0][1]))
        self.assertEqual(zipped[1][0][1].findtext("title"), "新闻 & 资讯")

    def test_duplicate_feeds_and_source_priority(self):
        first = self.parse(feed())
        second = self.parse(feed(title="另一节目", start="20261109000000 +0000", stop="20261109010000 +0000"))
        root, report = app.merge([first, second, self.parse(feed())])
        self.assertEqual(report, {"channels": 1, "programmes": 1, "duplicates_removed": 2})
        self.assertEqual(root.findtext("programme/title"), "新闻 & 资讯")
        self.assertEqual(root.findtext("programme/desc"), "节目介绍")

    def test_later_source_adds_new_time_slots(self):
        root, report = app.merge([self.parse(feed()), self.parse(feed(start="20261109100000 +0800", stop="20261109110000 +0800"))])
        self.assertEqual(report["programmes"], 2)
        self.assertEqual([p.get("start") for p in root.findall("programme")], ["20261109080000 +0800", "20261109100000 +0800"])

    def test_aliases_update_channel_and_programme(self):
        root, report = app.merge([self.parse(feed("old"), {"old": "CCTV1"}), self.parse(feed("CCTV1"))])
        self.assertEqual(report["channels"], 1)
        self.assertEqual(report["programmes"], 1)
        self.assertEqual(root.find("channel").get("id"), "CCTV1")
        self.assertEqual(root.find("programme").get("channel"), "CCTV1")

    def test_same_display_name_different_ids_are_preserved(self):
        _, report = app.merge([self.parse(feed("one")), self.parse(feed("two"))])
        self.assertEqual(report["channels"], 2)
        self.assertEqual(report["programmes"], 2)

    def test_missing_channel_metadata_is_created(self):
        data = b'<tv><programme channel="missing" start="20261109000000 +0000"><title>Test</title></programme></tv>'
        root, _ = app.merge([self.parse(data)])
        self.assertEqual(root.findtext("channel/display-name"), "missing")

    def test_bad_xml_empty_feed_and_bad_times_are_rejected(self):
        for data in [b"<tv>", b"<html>oops</html>", b"<tv/>",
                     feed(start="bad"), feed(stop="20261109070000 +0800"),
                     feed(start="20261109080000 +2460"), feed(title="")]:
            with self.subTest(data=data), self.assertRaises(Exception):
                self.parse(data)

    def test_xml_entities_are_rejected(self):
        with self.assertRaises(DefusedXmlException):
            self.parse(b'<!DOCTYPE tv [<!ENTITY x "boom">]><tv>&x;</tv>')

    def test_timezone_and_partial_time(self):
        self.assertEqual(app.parse_time("202611090800 +0800"), app.parse_time("20261109000000 UTC"))
        self.assertEqual(app.parse_time("20261109"), app.parse_time("20261109000000 +0000"))
        with self.assertRaises(ValueError):
            app.parse_time("20261109000000 CST")

    def test_gzip_inflation_limit(self):
        with patch.object(app, "MAX_XML", 10), self.assertRaises(ValueError):
            app.decode_xml(gzip.compress(b"x" * 11))

    def test_corrupt_gzip_crc_is_rejected_even_in_compatible_mode(self):
        data = bytearray(gzip.compress(feed()))
        data[-8] ^= 1
        with self.assertRaises(gzip.BadGzipFile) as caught:
            app.parse_source(bytes(data), {}, strict=False)
        self.assertIn("CRC integrity check failed", app.explain_error(caught.exception))

    def test_record_errors_identify_number_and_field(self):
        first = ET.fromstring(feed()).find("programme")
        root = ET.fromstring(feed(start="bad"))
        root.insert(1, first)
        with self.assertRaisesRegex(app.SourceError, r"programme #2, start: missing/unsupported"):
            self.parse(ET.tostring(root))
        with self.assertRaisesRegex(app.SourceError, r"programme #1, stop: invalid calendar"):
            self.parse(feed(stop="20260231090000 +0800"))
        with self.assertRaisesRegex(app.SourceError, "zero or negative duration"):
            self.parse(feed(stop="20261109080000 +0800"))
        with self.assertRaisesRegex(app.SourceError, "missing or empty title"):
            self.parse(feed(title=""))

    def test_xml_errors_and_http_codes_have_safe_diagnostics(self):
        try:
            self.parse(b"<tv>\n<programme></tv>")
        except ET.ParseError as error:
            self.assertIn("line 2, column", app.explain_error(error))
        else:
            self.fail("invalid XML accepted")
        error = HTTPError("https://example.com/?token=PRIVATE", 403, "PRIVATE", {}, None)
        self.assertIn("HTTP 403", app.explain_error(error))
        self.assertNotIn("PRIVATE", app.explain_error(error))

    def test_untrusted_values_never_enter_diagnostics(self):
        secret = "PRIVATE-URL-OR-TOKEN"
        with self.assertRaises(app.SourceError) as caught:
            self.parse(feed(start=secret))
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, app.explain_error(ValueError(secret)))
        self.assertNotIn(secret, app.explain_error(OSError(secret)))

    def test_pipeline_logs_download_and_precise_validation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "sources.txt").write_text("https://example.invalid/epg?token=PRIVATE")
            (base / "aliases.json").write_text('{"global":{},"sources":{}}')
            output, errors = io.StringIO(), io.StringIO()
            with patch.object(app, "download", return_value=gzip.compress(feed(title=""))), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                result = app.main(["--sources", str(base / "sources.txt"), "--aliases", str(base / "aliases.json"), "--output", str(base / "out")])
            self.assertEqual(result, 1)
            self.assertIn("downloaded", output.getvalue())
            self.assertIn("gzip", output.getvalue())
            self.assertIn("during decode/parse/validate", errors.getvalue())
            self.assertIn("programme #1: missing or empty title", errors.getvalue())
            self.assertNotIn("PRIVATE", output.getvalue() + errors.getvalue())
            self.assertFalse((base / "out").exists())

    def test_display_names_precede_icon_after_merge(self):
        first = feed().replace(b"</display-name>", b'</display-name><icon src="https://example.com/icon.png"/>')
        second = feed().replace("综合频道".encode(), b"Alternate")
        root, _ = app.merge([self.parse(first), self.parse(second)])
        self.assertEqual([x.tag for x in root.find("channel")], ["display-name", "display-name", "icon"])

    def test_known_marker_is_cleaned_without_changing_instant(self):
        data = feed(start="2026110908    正在播出0000 +0800", stop="2026110909 正在播出0000 +0800")
        stats = {}
        _, programmes = app.parse_source(data, {}, strict=False, diagnostics=stats)
        self.assertEqual(stats["cleaned_timestamps"], 2)
        self.assertEqual(programmes[0][1].get("start"), "20261109000000 +0000")
        self.assertEqual(programmes[0][1].get("stop"), "20261109010000 +0000")
        with self.assertRaises(app.SourceError):
            self.parse(data)

    def test_bad_stop_is_omitted_and_zero_length_is_skipped(self):
        for stop in ["20261109000400 +0800", "garbage"]:
            stats = {}
            _, programmes = app.parse_source(feed(start="20261109235900 +0800", stop=stop), {}, strict=False, diagnostics=stats)
            self.assertEqual(stats["discarded_stop_times"], 1)
            self.assertNotIn("stop", programmes[0][1].attrib)
            self.assertEqual(programmes[0][1].get("start"), "20261109235900 +0800")
        root = ET.fromstring(feed())
        root.append(ET.fromstring(feed(stop="20261109080000 +0800")).find("programme"))
        stats = {}
        _, programmes = app.parse_source(ET.tostring(root), {}, strict=False, diagnostics=stats)
        self.assertEqual(len(programmes), 1)
        self.assertEqual(stats["skipped_programmes"], 1)

    def test_arbitrary_start_text_is_not_guessed(self):
        root = ET.fromstring(feed())
        root.append(ET.fromstring(feed(start="2026110908 other text 0000 +0800")).find("programme"))
        stats = {}
        _, programmes = app.parse_source(ET.tostring(root), {}, strict=False, diagnostics=stats)
        self.assertEqual(len(programmes), 1)
        self.assertEqual(stats["skipped_programmes"], 1)

    def test_namespace_prevents_numeric_id_collisions(self):
        a = self.parse(feed("2"))
        b = self.parse(feed("2").replace("综合频道".encode(), "山东卫视".encode()))
        self.assertEqual(app.id_conflicts([a, b]), 1)
        app.namespace_feed(*a, 1, set())
        app.namespace_feed(*b, 2, set())
        root, report = app.merge([a, b])
        self.assertEqual(report["channels"], 2)
        self.assertEqual(report["programmes"], 2)
        self.assertEqual({p.get("channel") for p in root.findall("programme")}, {"s1:2", "s2:2"})
        self.assertEqual(app.id_conflicts([a, b]), 0)

    def test_explicit_alias_can_merge_across_namespaces(self):
        a = self.parse(feed("one"), {"one": "CCTV1"})
        b = self.parse(feed("two"), {"two": "CCTV1"})
        app.namespace_feed(*a, 1, {"CCTV1"})
        app.namespace_feed(*b, 2, {"CCTV1"})
        _, report = app.merge([a, b])
        self.assertEqual(report["programmes"], 1)
        self.assertEqual(report["duplicates_removed"], 1)

    def test_internal_channel_collision_is_reported(self):
        root = ET.fromstring(feed())
        channel = ET.fromstring(feed()).find("channel")
        channel.find("display-name").text = "Unrelated channel"
        root.insert(1, channel)
        stats = {}
        app.parse_source(ET.tostring(root), {}, strict=False, diagnostics=stats)
        self.assertEqual(stats["ambiguous_channel_ids"], 1)

    def test_alias_configuration_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aliases.json"
            for payload in [{"global": {"c1": ""}}, {"sources": {"0": {}}}, {"sources": []}]:
                path.write_text(json.dumps(payload))
                with self.assertRaises(ValueError):
                    app.load_aliases(path)

    def test_http_pipeline_and_failure_preserves_output(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/broken":
                    self.send_response(503)
                    self.end_headers()
                    return
                data = gzip.compress(feed()) if self.path == "/compressed" else feed()
                if self.path == "/bad-gzip":
                    corrupt = bytearray(gzip.compress(feed()))
                    corrupt[-8] ^= 1
                    data = bytes(corrupt)
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                base = Path(directory)
                source_path, alias_path, output = base / "sources.txt", base / "aliases.json", base / "public"
                url = f"http://127.0.0.1:{server.server_port}"
                source_path.write_text(f"# comment\n{url}/plain\n{url}/compressed\n")
                alias_path.write_text('{"global":{},"sources":{}}')
                args = ["--sources", str(source_path), "--aliases", str(alias_path), "--output", str(output)]
                self.assertEqual(app.main(args), 0)
                original = {p.name: p.read_bytes() for p in output.iterdir()}
                self.assertEqual(gzip.decompress(original["epg.xml.gz"]), original["epg.xml"])
                self.assertEqual(json.loads(original["status.json"])["duplicates_removed"], 1)
                self.assertEqual(ET.fromstring(original["epg.xml"]).findtext("programme/title"), "新闻 & 资讯")
                source_path.write_text(f"{url}/plain\n{url}/broken\n")
                with patch.object(app.time, "sleep"):
                    self.assertEqual(app.main(args), 1)
                    self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, original)
                    self.assertEqual(app.main(args + ["--allow-partial"]), 0)
                report = json.loads((output / "status.json").read_text())
                self.assertTrue(report["partial"])
                self.assertFalse(report["sources"][1]["ok"])
                self.assertNotIn(url, (output / "status.json").read_text())
                prior = {p.name: p.read_bytes() for p in output.iterdir()}
                source_path.write_text(f"{url}/plain\n{url}/bad-gzip\n")
                self.assertEqual(app.main(args), 1)
                self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, prior)
                self.assertEqual(app.main(args + ["--allow-partial"]), 0)
                crc_report = json.loads((output / "status.json").read_text())
                self.assertTrue(crc_report["partial"])
                self.assertEqual(crc_report["sources"][1]["error_type"], "BadGzipFile")
                self.assertIn("部分来源失败", (output / "index.html").read_text(encoding="utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_all_sources_fail_without_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "sources.txt").write_text("https://example.invalid/epg.xml.gz\n")
            (base / "aliases.json").write_text('{"global":{},"sources":{}}')
            with patch.object(app, "download", side_effect=OSError), contextlib.redirect_stderr(io.StringIO()):
                result = app.main(["--sources", str(base / "sources.txt"), "--aliases", str(base / "aliases.json"), "--output", str(base / "out"), "--allow-partial"])
            self.assertEqual(result, 1)
            self.assertFalse((base / "out").exists())


if __name__ == "__main__":
    unittest.main()
