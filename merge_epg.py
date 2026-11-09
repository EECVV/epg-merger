#!/usr/bin/env python3
"""Merge XMLTV feeds and build a static GitHub Pages subscription."""
from __future__ import annotations

import argparse
import copy
import gzip
import io
import json
import re
import shutil
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from defusedxml.ElementTree import fromstring

MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_XML = 256 * 1024 * 1024


class SourceError(ValueError):
    """A controlled diagnostic that never includes URLs or untrusted XML text."""


def explain_error(error: Exception) -> str:
    if isinstance(error, SourceError):
        return str(error)
    if isinstance(error, HTTPError):
        return f"HTTP {error.code}: server refused or could not serve the source"
    if isinstance(error, ET.ParseError):
        line, column = error.position
        return f"XML is malformed at line {line}, column {column}"
    if isinstance(error, gzip.BadGzipFile) and str(error).startswith("CRC check failed"):
        return "gzip CRC integrity check failed; source data is corrupt"
    if isinstance(error, (gzip.BadGzipFile, EOFError)):
        return "gzip file is invalid or incomplete"
    if isinstance(error, ValueError):
        return "invalid request or source value; check URL format (no quotes, spaces or trailing comments)"
    return "request, decompression or XML parsing failed; exception type is shown above"


def read_limited(stream, limit: int) -> bytes:
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise SourceError(f"source exceeds size limit ({limit} bytes)")
    return data


def decode_xml(data: bytes) -> bytes:
    # Detect gzip by file signature, not filename or HTTP Content-Type.
    if data.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            return read_limited(stream, MAX_XML)
    if len(data) > MAX_XML:
        raise SourceError("XML exceeds decompressed size limit")
    return data


def download(url: str) -> bytes:
    if urlsplit(url).scheme not in {"http", "https"}:
        raise SourceError("only HTTP/HTTPS sources are supported")
    for attempt in range(3):
        try:
            request = Request(url, headers={
                "User-Agent": "epg-merger/1.0",
                "Accept-Encoding": "identity",
            })
            with urlopen(request, timeout=45) as response:
                return read_limited(response, MAX_DOWNLOAD)
        except SourceError:
            raise
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    raise RuntimeError("unreachable")


@lru_cache(maxsize=200000)
def parse_time(value: str) -> datetime:
    # Day precision or better; named ambiguous time zones are rejected.
    match = re.fullmatch(r"(\d{8}|\d{10}|\d{12}|\d{14})(?:\s*([+-]\d{4}|UTC|GMT|Z))?", value.strip())
    if not match:
        raise SourceError("missing/unsupported XMLTV timestamp; expected YYYYMMDDhhmmss +0800 (day precision or better)")
    digits, offset = match.groups()
    digits = digits.ljust(14, "0")
    zone = timezone.utc
    if offset and offset[0] in "+-":
        hours, minutes = int(offset[1:3]), int(offset[3:5])
        if hours > 23 or minutes > 59:
            raise SourceError("invalid timezone offset; expected hours 00-23 and minutes 00-59")
        delta = timedelta(hours=hours, minutes=minutes)
        zone = timezone(delta if offset[0] == "+" else -delta)
    try:
        return datetime.strptime(digits, "%Y%m%d%H%M%S").replace(tzinfo=zone).astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise SourceError("invalid calendar date or clock time") from error


def load_aliases(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict) or set(config) - {"global", "sources"}:
        raise ValueError("aliases.json must contain only global and sources")
    global_map = config.get("global", {})
    source_maps = config.get("sources", {})
    if not isinstance(source_maps, dict):
        raise ValueError("sources aliases must be an object")
    for key in source_maps:
        if not re.fullmatch(r"[1-9]\d*", key):
            raise ValueError("source alias keys must be source numbers: 1, 2, ...")
    for mapping in [global_map, *source_maps.values()]:
        if not isinstance(mapping, dict) or any(
            not isinstance(k, str) or not k.strip() or
            not isinstance(v, str) or not v.strip()
            for k, v in mapping.items()
        ):
            raise ValueError("channel aliases must map nonempty strings to nonempty strings")
    return {"global": global_map, "sources": source_maps}


def read_sources(path: Path) -> list[str]:
    urls = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    if not urls:
        raise ValueError("sources.txt is empty; add your actual EPG URLs first")
    if any(urlsplit(url).scheme not in {"http", "https"} for url in urls):
        raise ValueError("each source must be one complete HTTP/HTTPS URL")
    return urls


def parse_source(data: bytes, aliases: dict[str, str], *, strict=True, diagnostics=None):
    stats = diagnostics if diagnostics is not None else {}
    stats.update(cleaned_timestamps=0, discarded_stop_times=0, skipped_programmes=0, skipped_channels=0, ambiguous_channel_ids=0, warnings=[])

    def warn(message):
        if len(stats["warnings"]) < 5:
            stats["warnings"].append(message)

    def timestamp(element, field, number):
        value = element.get(field, "")
        try:
            return parse_time(value)
        except SourceError as error:
            # Repair only this observed scraper marker; never strip arbitrary text.
            if not strict and "正在播出" in value:
                cleaned = re.sub(r"\s+", "", value.replace("正在播出", ""))
                try:
                    result = parse_time(cleaned)
                except SourceError:
                    pass
                else:
                    element.set(field, result.strftime("%Y%m%d%H%M%S +0000"))
                    stats["cleaned_timestamps"] += 1
                    warn(f"programme #{number}, {field}: removed known scraper marker")
                    return result
            raise SourceError(f"programme #{number}, {field}: {error}") from error

    root = fromstring(decode_xml(data))
    if root.tag != "tv":
        raise SourceError("root is not <tv>; URL may return a web page instead of XMLTV, or use an unsupported XML namespace")
    channels = []
    programmes = []
    channel_number = programme_number = 0
    original_names = {}
    ambiguous_ids = set()
    for element in root:
        if element.tag == "channel":
            channel_number += 1
            original = element.get("id", "").strip()
            if not original:
                message = f"channel #{channel_number}: missing or empty id"
                if strict:
                    raise SourceError(message)
                stats["skipped_channels"] += 1
                warn(message)
                continue
            element.set("id", aliases.get(original, original))
            if not any((name.text or "").strip() for name in element.findall("display-name")):
                name = ET.Element("display-name")
                name.text = element.get("id")
                element.insert(0, name)
            names = {(name.text or "").strip().casefold() for name in element.findall("display-name") if (name.text or "").strip()}
            if original in original_names and names.isdisjoint(original_names[original]):
                ambiguous_ids.add(original)
            original_names.setdefault(original, set()).update(names)
            channels.append(element)
        elif element.tag == "programme":
            programme_number += 1
            original = element.get("channel", "").strip()
            try:
                if not original:
                    raise SourceError(f"programme #{programme_number}: missing or empty channel")
                if not any((title.text or "").strip() for title in element.findall("title")):
                    raise SourceError(f"programme #{programme_number}: missing or empty title")
                start = timestamp(element, "start", programme_number)
                if element.get("stop"):
                    try:
                        stop = timestamp(element, "stop", programme_number)
                    except SourceError:
                        if strict:
                            raise
                        del element.attrib["stop"]
                        stats["discarded_stop_times"] += 1
                        warn(f"programme #{programme_number}: omitted invalid optional stop time")
                    else:
                        if stop <= start:
                            if strict or stop == start:
                                raise SourceError(f"programme #{programme_number}: stop is not later than start (zero or negative duration)")
                            # XMLTV stop is optional. Preserve the known start rather than invent an end date.
                            del element.attrib["stop"]
                            stats["discarded_stop_times"] += 1
                            warn(f"programme #{programme_number}: omitted stop earlier than start")
            except SourceError as error:
                if strict:
                    raise
                stats["skipped_programmes"] += 1
                warn(str(error))
                continue
            element.set("channel", aliases.get(original, original))
            for previous in element.findall("previously-shown"):
                channel = previous.get("channel")
                if channel:
                    previous.set("channel", aliases.get(channel, channel))
            programmes.append((start, element))
    if not programmes:
        first_issue = f"; first issue: {stats['warnings'][0]}" if stats["warnings"] else ""
        raise SourceError(f"source contains no usable <programme> records; skipped {stats['skipped_programmes']} invalid programmes{first_issue}")
    stats["ambiguous_channel_ids"] = len(ambiguous_ids)
    return channels, programmes


def namespace_feed(channels, programmes, number, explicit_targets):
    def mapped(channel_id):
        return channel_id if channel_id in explicit_targets else f"s{number}:{channel_id}"

    for channel in channels:
        channel.set("id", mapped(channel.get("id")))
    for _, programme in programmes:
        programme.set("channel", mapped(programme.get("channel")))
        for previous in programme.findall("previously-shown"):
            if previous.get("channel"):
                previous.set("channel", mapped(previous.get("channel")))


def id_conflicts(feeds):
    seen = {}
    collisions = set()
    for number, (channels, _) in enumerate(feeds, 1):
        local = {}
        for channel in channels:
            names = {(name.text or "").strip().casefold() for name in channel.findall("display-name") if (name.text or "").strip()}
            local.setdefault(channel.get("id"), set()).update(names)
        for channel_id, names in local.items():
            for prior_number, prior_names in seen.get(channel_id, []):
                if names and prior_names and not names.intersection(prior_names):
                    collisions.add((prior_number, number, channel_id))
            seen.setdefault(channel_id, []).append((number, names))
    return len(collisions)


def merge(feeds):
    channels = {}
    programmes = {}
    duplicates = 0
    for source_channels, source_programmes in feeds:
        for channel in source_channels:
            channel_id = channel.get("id")
            if channel_id not in channels:
                channels[channel_id] = channel
            else:
                # Preserve aliases used by IPTV applications for name matching.
                target = channels[channel_id]
                names = {(x.text, x.get("lang")) for x in target.findall("display-name")}
                for name in channel.findall("display-name"):
                    key = (name.text, name.get("lang"))
                    if key not in names:
                        target.insert(len(target.findall("display-name")), copy.deepcopy(name))
                        names.add(key)
        for start, programme in source_programmes:
            channel_id = programme.get("channel")
            key = (channel_id, start)
            if key in programmes:
                duplicates += 1
            else:
                programmes[key] = programme
    for channel_id, _ in programmes:
        if channel_id not in channels:
            channel = ET.Element("channel", {"id": channel_id})
            ET.SubElement(channel, "display-name").text = channel_id
            channels[channel_id] = channel
    root = ET.Element("tv", {"generator-info-name": "epg-merger/1.0"})
    root.extend(channels[key] for key in sorted(channels))
    root.extend(programmes[key] for key in sorted(programmes))
    return root, {"channels": len(channels), "programmes": len(programmes), "duplicates_removed": duplicates}


def publish(root: ET.Element, report: dict, output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    # Prepare the complete result before replacing output files.
    with tempfile.TemporaryDirectory(prefix="epg-build-", dir=output.parent) as folder:
        stage = Path(folder)
        ET.ElementTree(root).write(stage / "epg.xml", encoding="utf-8", xml_declaration=True)
        with (stage / "epg.xml").open("rb") as source, (stage / "epg.xml.gz").open("wb") as dest:
            with gzip.GzipFile(filename="", mode="wb", fileobj=dest, mtime=0) as zipped:
                shutil.copyfileobj(source, zipped)
        (stage / "status.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        health = "部分来源失败：本次节目单仅包含成功的来源，请查看更新状态。" if report.get("partial") else "全部来源处理成功。"
        (stage / "index.html").write_text(
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>EPG 节目单</title>'
            '<h1>EPG 节目单</h1><p><a href="epg.xml.gz">epg.xml.gz</a> · '
            '<a href="epg.xml">epg.xml</a> · <a href="status.json">更新状态（UTC）</a></p>'
            f'<p>{health}</p>'
            f'<p>频道：{report["channels"]}；节目：{report["programmes"]}；'
            f'更新时间：{report["updated_at_utc"]}</p></html>', encoding="utf-8")
        (stage / ".nojekyll").touch()
        output.mkdir(parents=True, exist_ok=True)
        for file in stage.iterdir():
            file.replace(output / file.name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=Path("sources.txt"))
    parser.add_argument("--aliases", type=Path, default=Path("aliases.json"))
    parser.add_argument("--output", type=Path, default=Path("public"))
    parser.add_argument("--allow-partial", action="store_true", help="publish even if some sources fail")
    parser.add_argument("--strict", action="store_true", help="reject a whole source on any invalid programme")
    parser.add_argument("--namespace-channels", action="store_true", help="prefix unmapped channel IDs by source to prevent ID collisions")
    args = parser.parse_args(argv)
    urls = read_sources(args.sources)
    aliases = load_aliases(args.aliases)
    if any(int(key) > len(urls) for key in aliases["sources"]):
        raise ValueError("an alias source number exceeds the number of configured URLs")
    feeds, results = [], []
    failed = False
    for number, url in enumerate(urls, 1):
        mapping = {**aliases["global"], **aliases["sources"].get(str(number), {})}
        stage = "download"
        try:
            data = download(url)
            encoding = "gzip" if data.startswith(b"\x1f\x8b") else "plain"
            print(f"Source {number}: downloaded {len(data)} bytes ({encoding})", flush=True)
            stage = "decode/parse/validate"
            diagnostics = {}
            channels, programmes = parse_source(data, mapping, strict=args.strict, diagnostics=diagnostics)
            if args.namespace_channels:
                namespace_feed(channels, programmes, number, set(mapping.values()))
            feeds.append((channels, programmes))
            results.append({"source": number, "ok": True, "channels": len(channels), "programmes": len(programmes), **diagnostics})
            print(f"Source {number}: {len(channels)} channels, {len(programmes)} programmes", flush=True)
            if any(diagnostics[key] for key in ("cleaned_timestamps", "discarded_stop_times", "skipped_programmes", "skipped_channels")):
                print(f"Source {number}: cleaned {diagnostics['cleaned_timestamps']} timestamps; omitted {diagnostics['discarded_stop_times']} stop times; skipped {diagnostics['skipped_programmes']} programmes and {diagnostics['skipped_channels']} channels", flush=True)
                for warning in diagnostics["warnings"]:
                    print(f"Source {number}: warning: {warning}", file=sys.stderr, flush=True)
            if diagnostics["ambiguous_channel_ids"]:
                print(f"Source {number}: WARNING: {diagnostics['ambiguous_channel_ids']} IDs have disjoint channel names inside this source; source prefixes cannot disambiguate them. Check upstream channel definitions.", file=sys.stderr, flush=True)
        except Exception as error:
            # Show controlled diagnostics, never arbitrary exception strings or URLs.
            failed = True
            error_type = type(error).__name__
            detail = explain_error(error)
            results.append({"source": number, "ok": False, "error_type": error_type, "stage": stage, "error_detail": detail})
            print(f"Source {number}: failed ({error_type}) during {stage}: {detail}", file=sys.stderr, flush=True)
    if not feeds or (failed and not args.allow_partial):
        print("No publication: source failure; the existing Pages deployment remains unchanged.", file=sys.stderr)
        return 1
    conflicts = id_conflicts(feeds)
    if conflicts:
        print(f"WARNING: {conflicts} cross-source channel ID/name conflicts; use --namespace-channels or explicit channel aliases to prevent mixed schedules.", file=sys.stderr, flush=True)
    root, report = merge(feeds)
    report.update(updated_at_utc=datetime.now(timezone.utc).isoformat(), partial=failed, sources=results, channel_id_conflicts=conflicts, namespaced_channels=args.namespace_channels)
    publish(root, report, args.output)
    print(f"Published {report['channels']} channels and {report['programmes']} programmes; removed {report['duplicates_removed']} duplicates.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"Configuration/output error: {type(error).__name__}. Check sources.txt, aliases.json and output permissions.", file=sys.stderr)
        raise SystemExit(1)
