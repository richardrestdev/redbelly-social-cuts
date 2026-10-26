#!/usr/bin/env python3
"""Check the clip pack against the brief and build verification.md.

Usage:
    python3 tools/verify.py            print the report
    python3 tools/verify.py --write    print it and save it to verification.md

Needs Python 3.8+ with ffmpeg and ffprobe on the PATH.
Exits with status 1 if any check fails.
"""
import csv
import json
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIPS = ROOT / "clips"
POSTERS = ROOT / "posters"
CLIP_LIST = ROOT / "clip_list.csv"
REPORT = ROOT / "verification.md"

MIN_SECONDS, MAX_SECONDS = 30.0, 60.0
MIN_SHORT_SIDE, MIN_FPS = 1080, 24.0
TARGET_LUFS, LUFS_TOLERANCE, MAX_TRUE_PEAK = -14.0, 1.0, -1.0
MAX_CAPTION = 280

# Each term must appear exactly as written; anything the pattern matches with
# other spelling or casing (Red Belly, RedBelly, Tokenizer, rbnt) is an error.
TERMS = {
    "Redbelly": re.compile(r"\bred[\s-]?belly\b", re.I),
    "Tokeniser": re.compile(r"\btokeni[sz]er\b", re.I),
    "RBNT": re.compile(r"\brbnt\b", re.I),
}

# Container tags that would carry a personal credit inside the files.
CREDIT_TAGS = {"artist", "album_artist", "author", "composer", "copyright",
               "comment", "description", "publisher", "encoded_by"}

TIMESTAMP = re.compile(r"(\d+):(\d\d):(\d\d),(\d{3})")


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True)


def probe(path):
    out = run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
               "-of", "json", str(path)]).stdout
    return json.loads(out)


def loudness(path):
    """Integrated loudness (LUFS) and true peak (dBTP) from the EBU R128 meter."""
    log = run(["ffmpeg", "-nostats", "-hide_banner", "-i", str(path), "-vn",
               "-af", "ebur128=peak=true", "-f", "null", "-"]).stderr
    summary = log.rsplit("Summary:", 1)[-1]
    lufs = float(re.search(r"I:\s+(-?[\d.]+) LUFS", summary).group(1))
    peak = float(re.search(r"True peak:\s+Peak:\s+(-?[\d.]+|-inf) dBFS", summary).group(1))
    return lufs, peak


def seconds(stamp):
    h, m, s, ms = map(int, TIMESTAMP.fullmatch(stamp).groups())
    return h * 3600 + m * 60 + s + ms / 1000


def read_srt(path):
    """Return [(start, end, text)] and a list of format problems."""
    cues, problems = [], []
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip())
    for n, block in enumerate(blocks, 1):
        lines = block.split("\n")
        if len(lines) < 3 or lines[0].strip() != str(n) or " --> " not in lines[1]:
            problems.append(f"cue {n} is not a valid SRT block")
            continue
        start, end = (seconds(part.strip()) for part in lines[1].split(" --> "))
        cues.append((start, end, " ".join(lines[2:])))
    return cues, problems


def check_clip(clip_id, row):
    mp4 = CLIPS / f"{clip_id}.mp4"
    srt = CLIPS / f"{clip_id}.srt"
    poster = POSTERS / f"{clip_id}.jpg"
    failures = []
    if not mp4.exists():
        return None, [f"{mp4.relative_to(ROOT)} is missing"]

    info = probe(mp4)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    width, height = video["width"], video["height"]
    fps = float(Fraction(video["r_frame_rate"]))
    duration = float(info["format"]["duration"])

    if width * 16 != height * 9:
        failures.append(f"frame is {width}x{height}, not 9:16")
    if min(width, height) < MIN_SHORT_SIDE:
        failures.append(f"frame is {width}x{height}, below 1080p")
    if fps < MIN_FPS:
        failures.append(f"{fps:.2f} fps is below {MIN_FPS:.0f}")
    if not MIN_SECONDS <= duration <= MAX_SECONDS:
        failures.append(f"{duration:.1f} s is outside {MIN_SECONDS:.0f} to {MAX_SECONDS:.0f} s")
    if "mp4" not in info["format"]["format_name"]:
        failures.append("container is not MP4")
    if video["codec_name"] != "h264" or audio is None or audio["codec_name"] != "aac":
        failures.append("streams are not H.264 video with AAC audio")

    tags = dict(info["format"].get("tags", {}))
    for stream in info["streams"]:
        tags.update(stream.get("tags", {}))
    credits = sorted(k for k in tags if k.lower() in CREDIT_TAGS)
    if credits:
        failures.append(f"metadata carries credit tags: {', '.join(credits)}")

    lufs, peak = loudness(mp4)
    if abs(lufs - TARGET_LUFS) > LUFS_TOLERANCE:
        failures.append(f"loudness {lufs:.1f} LUFS is outside {TARGET_LUFS:.0f} ±{LUFS_TOLERANCE:.0f}")
    if peak > MAX_TRUE_PEAK:
        failures.append(f"true peak {peak:.1f} dBTP is above {MAX_TRUE_PEAK:.1f}")

    cues = []
    if not srt.exists():
        failures.append(f"{srt.relative_to(ROOT)} is missing")
    else:
        cues, problems = read_srt(srt)
        failures += problems
        previous_end = 0.0
        for n, (start, end, text) in enumerate(cues, 1):
            if end <= start:
                failures.append(f"subtitle cue {n} ends before it starts")
            if start < previous_end - 0.001:
                failures.append(f"subtitle cue {n} overlaps the cue before it")
            if end > duration + 0.05:
                failures.append(f"subtitle cue {n} ends after the video")
            for term, pattern in TERMS.items():
                for found in pattern.findall(text):
                    if found != term:
                        failures.append(f'subtitle cue {n} spells "{found}" instead of "{term}"')
            previous_end = end
        if not cues:
            failures.append("subtitle file has no cues")

    if not poster.exists():
        failures.append(f"{poster.relative_to(ROOT)} is missing")
    elif b"Exif\x00\x00" in poster.read_bytes()[:65536]:
        failures.append(f"{poster.relative_to(ROOT)} carries EXIF data")

    if row is None:
        failures.append("no row in clip_list.csv")
    else:
        for field in ("episode", "episode_url", "timestamp", "speaker", "role", "caption"):
            if not row.get(field, "").strip():
                failures.append(f"clip_list.csv has no {field}")
        if not row.get("episode", "").startswith("Redbelly Insights - Episode"):
            failures.append("source is not a Redbelly Insights episode")
        if len(row.get("caption", "")) > MAX_CAPTION:
            failures.append(f"caption is longer than {MAX_CAPTION} characters")

    measured = {"frame": f"{width}x{height}", "fps": fps, "duration": duration,
                "lufs": lufs, "peak": peak, "cues": len(cues)}
    return measured, failures


def build_report():
    with CLIP_LIST.open(encoding="utf-8") as f:
        rows = {row["id"]: row for row in csv.DictReader(f)}
    clip_ids = sorted(p.stem for p in CLIPS.glob("*.mp4"))

    lines = [
        "# Verification report",
        "",
        "Generated by `tools/verify.py` from the rendered files with ffprobe and the ffmpeg EBU R128 meter.",
        "Run `python3 tools/verify.py --write` to measure the files again and refresh this page.",
        "",
        "| Clip | Resolution | FPS | Duration | Loudness | Subtitles | Specs |",
        "|---|---|---|---|---|---|---|",
    ]
    all_failures = []
    if len(clip_ids) != 6:
        all_failures.append(f"pack has {len(clip_ids)} clips, the brief asks for 6")
    for clip_id in clip_ids:
        m, failures = check_clip(clip_id, rows.get(clip_id))
        all_failures += [f"[{clip_id}] {f}" for f in failures]
        if m is None:
            lines.append(f"| {clip_id} | | | | | | FAIL |")
            continue
        lines.append(
            f"| {clip_id} | {m['frame']} | {m['fps']:.2f} | {m['duration']:.1f} s | "
            f"{m['lufs']:.1f} LUFS / {m['peak']:.1f} dBTP | {m['cues']} cues | "
            f"{'FAIL' if failures else 'PASS'} |")

    lines += [
        "",
        f"Loudness target: {TARGET_LUFS:.1f} LUFS integrated (±{LUFS_TOLERANCE:.0f} LU), "
        f"true peak {MAX_TRUE_PEAK:.1f} dBTP or lower on the exported AAC.",
        "",
        "## What is checked for every clip",
        "",
        "- 9:16 frame at 1080p or higher, 24 fps or higher, H.264 video with AAC audio in an MP4 file",
        "- Length between 30 and 60 seconds",
        "- Integrated loudness within ±1 LU of -14 LUFS and true peak at or below -1 dBTP",
        "- A matching .srt file whose cues are numbered, in order, do not overlap and end inside the video",
        "- Redbelly, Tokeniser and RBNT spelled exactly so in every subtitle cue",
        "- No credit tags (artist, author, comment, copyright and similar) in the file metadata",
        "- A poster frame with no EXIF data",
        "- A clip_list.csv row naming the Redbelly Insights source episode, timestamp, speaker, role "
        f"and a post caption of at most {MAX_CAPTION} characters",
        "",
        "## Failures",
        "",
    ]
    lines += [f"- {f}" for f in all_failures] or ["- None"]
    lines += [
        "",
        "## Items for a person to check",
        "",
        "- Subtitles were reconciled from two speech models (Parakeet TDT 0.6B v2, Whisper medium.en) "
        "and edited by hand; a final listen by a person is recommended before posting.",
        "",
    ]
    return "\n".join(lines), all_failures


def main():
    report, failures = build_report()
    print(report)
    if "--write" in sys.argv[1:]:
        REPORT.write_text(report, encoding="utf-8")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
