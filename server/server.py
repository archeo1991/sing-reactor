from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from difflib import SequenceMatcher
from statistics import median
import html
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from http_safety import UnsafeUrlError, parse_single_range, resolve_bilibili_redirects, validate_bilibili_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LYRICS_CONFIG_PATH = Path(__file__).resolve().parent / "lyrics_config.json"
VENDOR_PYTHON = PROJECT_ROOT / ".tools" / "python"
if VENDOR_PYTHON.exists():
    sys.path.insert(0, str(VENDOR_PYTHON))

PORT = int(os.environ.get("SING_REACTOR_API_PORT", "18768"))
HOST = os.environ.get("SING_REACTOR_API_HOST", "127.0.0.1").strip() or "127.0.0.1"
VIDEO_CACHE_DIR = PROJECT_ROOT / ".cache" / "videos"
VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_CACHE_DIR = PROJECT_ROOT / ".cache" / "audio"
AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
SPEECH_CACHE_DIR = PROJECT_ROOT / ".cache" / "speech"
SPEECH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
HF_CACHE_DIR = PROJECT_ROOT / ".cache" / "huggingface"
HF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("HF_HOME", str(HF_CACHE_DIR))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(HF_CACHE_DIR / "hub"))
LOCAL_MODELS_DIR = PROJECT_ROOT / ".models"
LOCAL_ORIGINS = {"http://127.0.0.1:4190", "http://localhost:4190"}
ALLOWED_ORIGINS = LOCAL_ORIGINS | {
    origin.strip().rstrip("/")
    for origin in os.environ.get("SING_REACTOR_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
}
MAX_JSON_BODY_BYTES = 1024 * 1024
MAX_LYRICS_BYTES = 512 * 1024
CACHE_TTL_SECONDS = 7 * 24 * 3600
CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024
CACHE_CLEAN_INTERVAL = 300
SPEECH_CACHE_SCHEMA_VERSION = 3
SAMPLED_SPEECH_CACHE_SCHEMA_VERSION = 3


def load_lyrics_config():
    try:
        with LYRICS_CONFIG_PATH.open("r", encoding="utf-8") as stream:
            config = json.load(stream)
        if not isinstance(config, dict) or int(config.get("schema_version") or 0) != 1:
            raise ValueError("invalid lyrics config schema")
        return config
    except (OSError, ValueError, json.JSONDecodeError):
        return {
            "schema_version": 1, "pipeline_version": 2,
            "provider_quality": {"bilibili_subtitle": 0.72, "netease": 0.85, "lrclib": 0.9},
            "candidate_selection": {"metadata_weight": 0.14, "provider_weight": 0.06, "audio_weight": 0.8},
            "fallback": {
                "allow_original_synced": True, "allow_whisper_text": True,
                "allow_weak_timeline_rebuild": True,
                "weak_timeline_min_similarity": 0.86, "weak_timeline_max_offset": 45,
            },
        }


LYRICS_CONFIG = load_lyrics_config()
LYRICS_PIPELINE_VERSION = int(LYRICS_CONFIG.get("pipeline_version") or 2)
_CACHE_LOCKS = {}
_CACHE_LOCKS_GUARD = threading.Lock()
_CACHE_CLEAN_LOCK = threading.Lock()
_LAST_CACHE_CLEAN = 0
_WHISPER_MODELS = {}
_WHISPER_MODEL_LOCK = threading.Lock()
_WHISPER_INFERENCE = threading.BoundedSemaphore(max(1, int(os.environ.get("SING_REACTOR_WHISPER_CONCURRENCY", "1"))))

KNOWN_TITLES = {
    "BV1es4y177tg": "后来的我们",
    "BV1sc411V7ZE": "后来的我们",
    "BV1sc411V7s7": "富士山下",
    "BV1n24y1V7a6": "富士山下",
}

KNOWN_ARTISTS = {
    "BV1es4y177tg": "五月天",
    "BV1sc411V7ZE": "五月天",
    "BV1sc411V7s7": "陈奕迅",
    "BV1n24y1V7a6": "陈奕迅",
}

KNOWN_CIDS = {
    "BV1es4y177tg": 314381983,
}

KNOWN_VIDEO_OFFSETS = {
    "BV1sc411V7s7": 13,
}


KNOWN_LYRICS = {
    "后来的我们": [
        {"time": "00:15", "seconds": 15.78, "text": "然后呢 他们说你的心 似乎痊愈了"},
        {"time": "00:25", "seconds": 25.4, "text": "也开始有个人 为你守护着"},
        {"time": "00:31", "seconds": 31.61, "text": "我该心安或是 心痛呢"},
        {"time": "00:37", "seconds": 37.97, "text": "然后呢"},
        {"time": "00:40", "seconds": 40.63, "text": "其实我的日子 也还可以呢"},
        {"time": "00:46", "seconds": 46.66, "text": "除了回忆肆虐 的某些时刻"},
        {"time": "00:53", "seconds": 53.33, "text": "庆幸还有眼泪 冲淡苦涩"},
        {"time": "01:01", "seconds": 61.15, "text": "而那些昨日 依然缤纷着"},
        {"time": "01:07", "seconds": 67.26, "text": "它们都有我 细心收藏着"},
        {"time": "01:13", "seconds": 73.59, "text": "也许你还记得 也许你都忘了"},
        {"time": "01:19", "seconds": 79.28, "text": "也不是那么 重要了"},
        {"time": "01:25", "seconds": 85.26, "text": "只期待 后来的你 能快乐"},
        {"time": "01:31", "seconds": 91.97, "text": "那就是 后来的我 最想的"},
        {"time": "01:38", "seconds": 98.26, "text": "后来的我们 依然走着"},
        {"time": "01:42", "seconds": 102.43, "text": "只是不再并肩了"},
        {"time": "01:45", "seconds": 105.41, "text": "朝各自的人生 追寻了"},
        {"time": "01:50", "seconds": 110.06, "text": "无论是 后来故事 怎么了"},
        {"time": "01:56", "seconds": 116.94, "text": "也要让 后来人生 精彩着"},
        {"time": "02:02", "seconds": 122.99, "text": "后来的我们 我期待着"},
        {"time": "02:06", "seconds": 126.96, "text": "泪水中能看到 你真的 自由了"},
    ]
}


def normalize_bilibili_url(value, resolve_redirects=False):
    safe_url = validate_bilibili_url(value)
    if resolve_redirects:
        safe_url, _ = resolve_bilibili_redirects(safe_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        })
    return safe_url


def extract_page_number(url):
    try:
        value = parse_qs(urlparse(url).query).get("p", ["1"])[0]
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def select_bilibili_page(view, url):
    pages = list((view or {}).get("pages") or [])
    requested = extract_page_number(url)
    page = next((item for item in pages if int(item.get("page") or 0) == requested), None)
    if page is None and pages and requested <= len(pages):
        page = pages[requested - 1]
    if page is None:
        page = {
            "page": 1, "cid": (view or {}).get("cid"),
            "part": (view or {}).get("title") or "",
            "duration": (view or {}).get("duration") or 0,
        }
    return page, requested, len(pages)


def infer_metadata_language_hint(text):
    text = str(text or "")
    if re.search(r"[\u3040-\u30ff\u31f0-\u31ff]", text):
        return "ja"
    if re.search(r"[\uac00-\ud7a3]", text):
        return "ko"
    if len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text)) >= 2:
        return "zh"
    if len(re.findall(r"[A-Za-z]+", text)) >= 2:
        return "en"
    return "auto"


def build_song_metadata(title, artist=None, view=None, page=None):
    view = view or {}
    page = page or {}
    page_title = clean_title(page.get("part") or title)
    total_title = clean_title(view.get("title") or title)
    raw = " ".join(str(value or "") for value in (page_title, total_title, view.get("desc"), view.get("dynamic"), view.get("tname")))
    terms = []
    patterns = (("live", r"\blive\b|现场|現場|演唱会|演唱會|音乐节|音樂節"), ("cover", r"\bcover\b|翻唱|试唱|試唱"), ("instrumental", r"\binstrumental\b|伴奏|无人声|無人聲|off\s*vocal|karaoke"), ("remix", r"\bremix\b|重混|混音版"), ("remaster", r"\bremaster(?:ed)?\b|重制|修复版|修復版"), ("edit", r"\bedit\b|剪辑版|剪輯版|片段"))
    for name, pattern in patterns:
        if re.search(pattern, raw, re.I):
            terms.append(name)
    return {
        "song_title": normalize_song_query(page_title) or normalize_song_query(total_title),
        "display_title": page_title, "total_title": total_title,
        "artist": artist or infer_artist_from_title(page_title) or infer_artist_from_title(total_title) or "",
        "duration": float(page.get("duration") or view.get("duration") or 0),
        "page": int(page.get("page") or 1), "cid": page.get("cid"),
        "language": infer_metadata_language_hint(raw), "version_terms": terms,
        "version": "_".join(terms), "is_live": "live" in terms,
        "is_cover": "cover" in terms, "is_instrumental": "instrumental" in terms,
    }


def is_bilibili_url(value):
    try:
        validate_bilibili_url(value, resolve_host=False)
        return True
    except UnsafeUrlError:
        return False


def extract_bvid(url):
    match = re.search(r"BV[0-9A-Za-z]+", url or "")
    return match.group(0) if match else ""


def infer_title_from_url(url):
    bvid = extract_bvid(url)
    if bvid in KNOWN_TITLES:
        return KNOWN_TITLES[bvid]
    return f"B站歌曲 {bvid}" if bvid else "待识别歌曲"


def clean_title(title):
    title = re.sub(r"\s+", " ", title or "").strip()
    title = re.sub(r"_哔哩哔哩_bilibili$", "", title)
    title = re.sub(r" - 哔哩哔哩.*$", "", title)
    return title.strip() or "待识别歌曲"


def infer_artist_from_title(title):
    text = str(title or "")
    known_names = ["五月天", "陈奕迅", "陳奕迅", "任素汐", "刘若英", "周杰伦", "林俊杰"]
    for name in known_names:
        if name in text:
            return "陈奕迅" if name == "陳奕迅" else name
    match = re.search(r"([^\s《》【】\[\]（）()]{2,16})\s*[《<]", text)
    if match:
        candidate = match.group(1)
        candidate = re.sub(r"^(官方|高清|修复|Hi-Res|4K|MV)+", "", candidate, flags=re.I).strip()
        if candidate and not re.search(r"修复|收录|专辑|歌曲|视频", candidate):
            return candidate
    return None


def request_json(url, referer="https://www.bilibili.com/"):
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Referer": referer,
        },
    )
    with urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8", errors="ignore"))


BILIBILI_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}


def _is_allowed_bilibili_media_url(value):
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port not in (None, 80 if parsed.scheme == "http" else 443):
        return False
    return host == "bilivideo.com" or host.endswith(".bilivideo.com") or host == "bilivideo.cn" or host.endswith(".bilivideo.cn")


def _safe_bilibili_media_urls(item):
    values = [item.get("baseUrl") or item.get("base_url")]
    values.extend(item.get("backupUrl") or item.get("backup_url") or [])
    return [value for value in values if value and _is_allowed_bilibili_media_url(value)]


def fetch_bilibili_media_streams(url):
    bvid = extract_bvid(url)
    if not bvid:
        return None
    view = fetch_video_view(bvid)
    page, _, _ = select_bilibili_page(view, url)
    if not view or not page.get("cid"):
        return None
    query = urlencode({"bvid": bvid, "cid": page["cid"], "fnval": 16, "qn": 80, "fourk": 1})
    payload = request_json(f"https://api.bilibili.com/x/player/playurl?{query}")
    if payload.get("code") != 0:
        return None
    data = payload.get("data") or {}
    dash = data.get("dash") or {}
    videos = [(item, _safe_bilibili_media_urls(item)) for item in dash.get("video") or []]
    audios = [(item, _safe_bilibili_media_urls(item)) for item in dash.get("audio") or []]
    videos = [(item, urls) for item, urls in videos if urls]
    audios = [(item, urls) for item, urls in audios if urls]
    if not videos or not audios:
        return None
    video, video_urls = max(videos, key=lambda pair: ("avc1" in str(pair[0].get("codecs") or ""), int(pair[0].get("bandwidth") or 0)))
    audio, audio_urls = max(audios, key=lambda pair: int(pair[0].get("bandwidth") or 0))
    return {
        "video_url": video_urls[0],
        "audio_url": audio_urls[0],
        "headers": BILIBILI_REQUEST_HEADERS,
    }


def post_json(url, data, referer):
    body = urlencode(data).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Referer": referer,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8", errors="ignore"))


def fetch_bilibili_title(url):
    _, body = resolve_bilibili_redirects(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        },
        max_bytes=1024 * 512,
    )
    html = body.decode("utf-8", errors="ignore")
    og_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html, re.I)
    if og_match:
        return clean_title(og_match.group(1))
    title_match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    if title_match:
        return clean_title(title_match.group(1))
    return None


def fetch_title_with_media_tool(url):
    command = get_yt_dlp_command()
    if not command:
        return None
    try:
        result = subprocess.run(
            command + ["--skip-download", "--print", "%(title)s", url],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=45,
            env=subprocess_env(),
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        cleaned = clean_title(line)
        if cleaned:
            return cleaned
    return None


def fetch_video_view(bvid):
    if not bvid:
        return None
    payload = request_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    if payload.get("code") != 0:
        return None
    return payload.get("data") or None


def seconds_to_time(value):
    value = max(0, float(value or 0))
    minute = int(value // 60)
    second = int(value % 60)
    return f"{minute:02d}:{second:02d}"


def lyric_line(seconds, text):
    return {"time": seconds_to_time(seconds), "seconds": max(0, float(seconds or 0)), "text": str(text or "").strip()}


def parse_lrc(lrc_text):
    lines = []
    for raw in (lrc_text or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        matches = list(re.finditer(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]", raw))
        text = re.sub(r"\[[^\]]+\]", "", raw).strip()
        if not text or not matches:
            continue
        for match in matches:
            minute = int(match.group(1))
            second = int(match.group(2))
            fraction = int((match.group(3) or "0").ljust(3, "0")[:3]) / 1000
            lines.append(lyric_line(minute * 60 + second + fraction, text))
    return sorted(lines, key=lambda item: item["seconds"])


def parse_vtt(vtt_text):
    lines = []
    current_seconds = None
    text_parts = []

    def flush():
        nonlocal current_seconds, text_parts
        if current_seconds is None or not text_parts:
            current_seconds = None
            text_parts = []
            return
        text = " ".join(text_parts)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            lines.append(lyric_line(current_seconds, text))
        current_seconds = None
        text_parts = []

    for raw in (vtt_text or "").splitlines():
        line = raw.strip()
        if not line or line.upper().startswith("WEBVTT") or line.startswith("NOTE"):
            flush()
            continue
        match = re.search(r"(\d{1,2}:\d{2}:\d{2}\.\d{1,3}|\d{1,2}:\d{2}\.\d{1,3})\s+-->", line)
        if match:
            flush()
            current_seconds = time_to_seconds(match.group(1))
            continue
        if current_seconds is not None:
            text_parts.append(line)
    flush()
    return sorted(lines, key=lambda item: item["seconds"])


def time_to_seconds(value):
    parts = str(value or "0:00").replace(",", ".").split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return 0
    return 0


def normalize_song_query(title):
    quoted = re.findall(r"《([^》]{1,80})》", title or "")
    if quoted:
        return quoted[-1].strip()
    title = re.sub(r"【.*?】|\[.*?\]|（.*?）|\(.*?\)", " ", title or "")
    title = re.sub(r"(官方|完整版|翻唱|cover|MV|Live|现场|歌词|字幕|伴奏|纯享)", " ", title, flags=re.I)
    title = title.replace("《", " ").replace("》", " ")
    title = re.sub(r"\s+", " ", title).strip()
    return title


def normalize_alignment_text(text):
    text = html.unescape(str(text or ""))
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"\[[^\]]+\]", " ", text)
    text = re.sub(r"^[男女合]\s*[:：]", "", text)
    text = re.sub(r"[（(][^）)]{0,16}[）)]", " ", text)
    text = re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff]+", "", text)
    return text.strip()


def text_similarity(left, right):
    left = normalize_alignment_text(left)
    right = normalize_alignment_text(right)
    if not left or not right:
        return 0
    if left == right:
        return 1
    shorter, longer = sorted([left, right], key=len)
    contain_score = 0
    if shorter in longer:
        ratio = len(shorter) / max(1, len(longer))
        contain_score = 0.55 + 0.35 * ratio
        if len(shorter) <= 2 and ratio < 0.75:
            contain_score = min(contain_score, 0.58)
    return max(contain_score, SequenceMatcher(None, left, right).ratio())


def title_similarity(expected, actual):
    return text_similarity(normalize_song_query(expected), normalize_song_query(actual))


def lyrics_duration(lines):
    if not lines:
        return 0
    return max(float(line.get("seconds") or 0) for line in lines)


def is_credit_lyric_line(text):
    text = str(text or "").strip()
    role = (
        r"演唱|原唱|作词|作詞|作曲|编曲|編曲|改编|改編|改编编曲|改編編曲|"
        r"制作人|制作|製作人|製作|監製|监制|音乐总监|音樂總監|音响总监|音響總監|"
        r"舞台总监|舞台總監|音乐设计|音樂設計|音乐统筹|音樂統籌|乐队总监|樂隊總監|乐队统筹|樂隊統籌|"
        r"录音|錄音|混音|母带|母帶|和声|和聲|和声编写|和聲編寫|声乐指导|聲樂指導|"
        r"吉他|贝斯|貝斯|鼓|鼓手|打击乐|打擊樂|弦乐|弦樂|键盘|鍵盤|钢琴|鋼琴|和音|和声|和聲|PGM|Program|"
        r"OP|SP|出品|发行|發行|版权|版權"
    )
    return bool(re.match(rf"^(?:{role})\s*[:：]", text, re.I))


def clean_lyric_lines(lines):
    cleaned = []
    for line in lines or []:
        text = str(line.get("text") or "").strip()
        if not text or is_credit_lyric_line(text):
            continue
        cleaned.append(lyric_line(line.get("seconds"), text))
    return cleaned


def duration_score(expected, actual):
    expected = float(expected or 0)
    actual = float(actual or 0)
    if expected <= 0 or actual <= 0:
        return 0.5
    diff = abs(expected - actual)
    if diff <= 8:
        return 1
    if diff <= 30:
        return 0.65
    return 0.25


def duration_mismatch(expected, actual):
    expected = float(expected or 0)
    actual = float(actual or 0)
    if expected <= 0 or actual <= 0:
        return False
    return abs(expected - actual) > max(45, expected * 0.35)


def version_penalty(title):
    text = str(title or "").lower()
    return 0.18 if re.search(r"live|现场|演唱会|remix|伴奏|翻唱|cover|片段|剪辑", text, re.I) else 0


def build_lyric_queries(title, artist=None):
    base = normalize_song_query(title)
    queries = []
    if base and artist:
        queries.append(f"{base} {artist}")
    if base:
        queries.append(base)
    if title and title != base:
        queries.append(title)

    seen = set()
    unique = []
    for query in queries:
        query = re.sub(r"\s+", " ", str(query or "")).strip()
        if query and query not in seen:
            seen.add(query)
            unique.append(query)
    return unique


def fetch_lyrics_from_lrclib(title, artist=None, expected_duration=0):
    best_candidate = None
    expected_artist = re.sub(r"\s+", "", artist or "").lower()
    for search_query in build_lyric_queries(title, artist):
        results = request_json(f"https://lrclib.net/api/search?q={quote(search_query)}", referer="https://lrclib.net/")
        if not isinstance(results, list) or not results:
            continue
        for item in results:
            parsed = parse_lrc(item.get("syncedLyrics") or "")
            plain = item.get("plainLyrics") or ""
            plain_lines = [lyric_line(index * 6, line) for index, line in enumerate(plain.splitlines()) if line.strip()]
            lines = clean_lyric_lines(parsed or plain_lines)
            if not lines:
                continue
            item_duration = float(item.get("duration") or 0)
            actual_duration = item_duration or (lyrics_duration(lines) if parsed else 0)
            if duration_mismatch(expected_duration, actual_duration):
                continue
            track_score = title_similarity(title, item.get("trackName") or "")
            item_artist = re.sub(r"\s+", "", item.get("artistName") or "").lower()
            artist_score = 1 if not expected_artist else text_similarity(expected_artist, item_artist)
            has_synced = 1 if parsed else 0
            duration_match_score = duration_score(expected_duration, actual_duration)
            score = track_score * 0.5 + artist_score * 0.2 + has_synced * 0.18 + duration_match_score * 0.12 - version_penalty(item.get("trackName"))
            if track_score < 0.78 or (expected_artist and artist_score < 0.72):
                continue
            candidate = (score, lines, {
                "provider": "lrclib",
                "trackName": item.get("trackName"),
                "artistName": item.get("artistName"),
                "query": search_query,
                "score": round(score, 3),
                "duration": actual_duration,
                "durationScore": duration_match_score,
                "mode": "synced_lrc" if parsed else "text_only",
            })
            if not best_candidate or candidate[0] > best_candidate[0]:
                best_candidate = candidate
    if best_candidate:
        return best_candidate[1], best_candidate[2]
    return [], None


def names_match(expected, actual):
    expected = normalize_song_query(expected)
    actual = normalize_song_query(actual)
    if not expected or not actual:
        return False
    return text_similarity(expected, actual) >= 0.78


def artists_match(expected, artists):
    if not expected:
        return True
    expected = re.sub(r"\s+", "", expected or "").lower()
    for artist in artists or []:
        name = re.sub(r"\s+", "", artist.get("name") or str(artist or "")).lower()
        if expected and (expected in name or name in expected):
            return True
    return False


def fetch_lyrics_from_netease(title, artist=None, expected_duration=0):
    for search_query in build_lyric_queries(title, artist):
        payload = post_json(
            "https://music.163.com/api/search/get/web",
            {"s": search_query, "type": 1, "offset": 0, "total": "true", "limit": 10},
            "https://music.163.com/",
        )
        songs = (((payload.get("result") or {}).get("songs")) or [])
        candidates = []
        for song in songs:
            song_duration = float(song.get("duration") or 0) / 1000
            if duration_mismatch(expected_duration, song_duration):
                continue
            track_score = title_similarity(title, song.get("name"))
            artist_score = 1 if artists_match(artist, song.get("artists") or []) else 0
            if track_score < 0.78 or (artist and artist_score < 1):
                continue
            duration_match_score = duration_score(expected_duration, song_duration)
            score = track_score * 0.66 + artist_score * 0.22 + duration_match_score * 0.12 - version_penalty(song.get("name"))
            candidates.append((score, song, song_duration, duration_match_score))
        if not candidates:
            continue

        best_score, song, song_duration, duration_match_score = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
        lyric_payload = request_json(
            f"https://music.163.com/api/song/lyric?id={song.get('id')}&lv=1&kv=1&tv=-1",
            referer="https://music.163.com/",
        )
        raw_lrc = ((lyric_payload.get("lrc") or {}).get("lyric")) or ""
        parsed = parse_lrc(raw_lrc)
        parsed = clean_lyric_lines(parsed)
        meta = {
            "provider": "netease",
            "trackName": song.get("name"),
            "artistName": "/".join(a.get("name") for a in (song.get("artists") or []) if a.get("name")),
            "query": search_query,
            "score": round(best_score, 3),
            "duration": song_duration,
            "durationScore": duration_match_score,
        }
        if parsed:
            return parsed, meta
    return [], None


def fetch_subtitle_lines(bvid, cid):
    if not bvid or not cid:
        return []
    info = request_json(f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}")
    subtitles = (((info.get("data") or {}).get("subtitle") or {}).get("subtitles") or [])
    if not subtitles:
        return []

    def subtitle_priority(item):
        language = str(item.get("lan") or "").lower()
        label = str(item.get("lan_doc") or "").lower()
        is_chinese = language.startswith("zh") or bool(re.search(r"中文|简体|繁体|汉语|漢語", label))
        is_auto = bool(re.search(r"ai|自动|自動|机器|機器", f"{language} {label}", re.I))
        return (1 if is_chinese else 0, 1 if not is_auto else 0)

    subtitle_url = max(subtitles, key=subtitle_priority).get("subtitle_url") or ""
    if subtitle_url.startswith("//"):
        subtitle_url = "https:" + subtitle_url
    elif subtitle_url.startswith("/"):
        subtitle_url = urljoin("https://www.bilibili.com", subtitle_url)
    if not subtitle_url:
        return []

    subtitle = request_json(subtitle_url)
    body = subtitle.get("body") or []
    return [
        lyric_line(item.get("from"), item.get("content"))
        for item in body
        if str(item.get("content") or "").strip()
    ]


def split_alignment_units(text):
    normalized = normalize_alignment_text(text)
    return [char for char in normalized if char.strip()]


def anchor_time_units(anchors):
    units = []
    for anchor in anchors or []:
        anchor_start = float(anchor.get("start", anchor.get("seconds", 0)) or 0)
        anchor_end = float(anchor.get("end", 0) or 0)
        if anchor_end <= anchor_start:
            anchor_end = anchor_start + 0.25
        words = anchor.get("words") or []
        quality_fields = {
            key: anchor.get(key) for key in ("no_speech_prob", "avg_logprob", "compression_ratio")
            if anchor.get(key) is not None
        }
        if words:
            for word in words:
                chars = split_alignment_units(word.get("word"))
                if not chars:
                    continue
                word_start = float(word.get("start", anchor_start) or anchor_start)
                word_end = float(word.get("end", word_start) or word_start)
                if word_end <= word_start:
                    word_end = word_start + max(0.12, (anchor_end - anchor_start) / max(1, len(chars)))
                step = (word_end - word_start) / max(1, len(chars))
                for index, char in enumerate(chars):
                    units.append({
                        "char": char, "start": word_start + step * index, "end": word_start + step * (index + 1),
                        "anchor_start": anchor_start, "anchor_end": anchor_end, "timing_source": "word", **quality_fields,
                    })
            continue
        chars = split_alignment_units(anchor.get("text"))
        if not chars:
            continue
        step = (anchor_end - anchor_start) / max(1, len(chars))
        for index, char in enumerate(chars):
            units.append({
                "char": char, "start": anchor_start + step * index, "end": anchor_start + step * (index + 1),
                "anchor_start": anchor_start, "anchor_end": anchor_end, "timing_source": "segment", **quality_fields,
            })
    return sorted(units, key=lambda item: item["start"])


def best_unit_span(line_text, units, min_index=0, expected_seconds=None):
    chars = split_alignment_units(line_text)
    if not chars or not units:
        return None
    target = "".join(chars)
    min_index = max(0, int(min_index or 0))
    line_len = len(chars)
    min_len = max(1, int(line_len * 0.45))
    max_len = max(min_len, int(line_len * 2.15) + 4)
    start_limit = min(len(units), min_index + max(90, line_len * 15))
    if expected_seconds is not None:
        nearby = [i for i, unit in enumerate(units) if i >= min_index and abs(unit["start"] - expected_seconds) <= 18]
        if nearby:
            min_index = max(min_index, min(nearby) - max(4, line_len))
            start_limit = min(len(units), max(nearby) + max(8, line_len * 2))
    best = None
    unit_text = "".join(unit["char"] for unit in units)
    for start in range(min_index, start_limit):
        max_end = min(len(units), start + max_len)
        for end in range(start + min_len, max_end + 1):
            sample = unit_text[start:end]
            similarity = SequenceMatcher(None, target, sample).ratio()
            if expected_seconds is not None:
                similarity -= min(0.12, abs(units[start]["start"] - expected_seconds) / 220)
            candidate = (similarity, start, end)
            if not best or candidate[0] > best[0]:
                best = candidate
    if not best:
        return None
    threshold = 0.5 if line_len <= 4 else 0.36
    if best[0] < threshold:
        return None
    _, start, end = best
    anchor_start = units[start].get("anchor_start", units[start]["start"])
    anchor_end = units[end - 1].get("anchor_end", units[end - 1]["end"])
    if expected_seconds is not None and abs(anchor_start - expected_seconds) > 3.0:
        anchor_start = units[start]["start"]
    return {"start_index": start, "end_index": end, "seconds": anchor_start, "end": anchor_end, "similarity": best[0]}


def estimate_local_lrc_offset(lyrics, anchors, max_lines=24):
    offsets = []
    used_anchor_indexes = set()
    for lyric in (lyrics or [])[:max_lines]:
        best = None
        for anchor_index, anchor in enumerate(anchors or []):
            if anchor_index in used_anchor_indexes:
                continue
            similarity = text_similarity(lyric.get("text"), anchor.get("text"))
            if similarity < 0.5:
                continue
            offset = float(anchor.get("seconds") or 0) - float(lyric.get("seconds") or 0)
            if abs(offset) > 12:
                continue
            candidate = (similarity, anchor_index, offset)
            if not best or candidate[0] > best[0]:
                best = candidate
        if best:
            used_anchor_indexes.add(best[1])
            offsets.append(best[2])
    if len(offsets) < 4:
        return None, {"mode": "unverified", "matched_count": len(offsets), "reason": "not_enough_local_offsets"}
    center = median(offsets)
    cluster = [value for value in offsets if abs(value - center) <= 1.25]
    if len(cluster) < 4:
        return None, {"mode": "unverified", "matched_count": len(offsets), "reason": "local_offsets_not_consistent"}
    offset = median(cluster)
    return offset, {"mode": "local_lrc_offset", "matched_count": len(cluster), "offset": round(offset, 3)}


def alignment_line_is_usable(text):
    normalized = normalize_alignment_text(text)
    if not normalized or is_credit_lyric_line(text):
        return False
    return not bool(re.fullmatch(r"(纯音乐|純音樂|间奏|間奏|前奏|尾奏|instrumental|music|伴奏)", normalized, re.I))


def unit_alignment_quality(unit):
    quality = 1.0
    if unit.get("no_speech_prob") is not None:
        quality *= max(0.0, 1.0 - float(unit["no_speech_prob"]))
    if unit.get("avg_logprob") is not None:
        quality *= max(0.0, min(1.0, (float(unit["avg_logprob"]) + 1.25) / 1.1))
    if unit.get("compression_ratio") is not None and float(unit["compression_ratio"]) > 2.0:
        quality *= 0.7
    return quality


def lyric_span_candidates(line_index, line_text, units, expected_seconds=None, top_k=28, max_starts=720):
    target = normalize_alignment_text(line_text)
    if not target:
        return []
    target_len = len(target)
    unit_text = "".join(unit["char"] for unit in units)
    min_len = max(1, int(target_len * 0.5))
    max_len = min(112, max(min_len, int(target_len * 1.9) + 8))
    lengths = set()
    for ratio in (0.55, 0.7, 0.85, 1.0, 1.15, 1.35, 1.6, 1.9):
        center = max(min_len, min(max_len, round(target_len * ratio)))
        lengths.update(range(max(min_len, center - 2), min(max_len, center + 2) + 1))

    positions = {}
    for index, char in enumerate(unit_text):
        positions.setdefault(char, []).append(index)
    starts = set()
    offsets = sorted(set((0, target_len // 4, target_len // 2, (target_len * 3) // 4, target_len - 1)))
    for offset in offsets:
        found = positions.get(target[offset], [])
        step = max(1, math.ceil(len(found) / max(1, max_starts // max(1, len(offsets)))))
        for position in found[::step]:
            start = position - offset
            if 0 <= start < len(units):
                starts.add(start)
    if expected_seconds is not None:
        nearest = sorted(range(len(units)), key=lambda index: abs(float(units[index]["start"]) - expected_seconds))[:24]
        for center in nearest:
            starts.update(range(max(0, center - target_len), min(len(units), center + target_len + 1), max(1, target_len // 8)))
    if len(starts) > max_starts:
        ordered = sorted(starts)
        stride = len(ordered) / max_starts
        starts = {ordered[min(len(ordered) - 1, int(index * stride))] for index in range(max_starts)}

    candidates = []
    for start in sorted(starts):
        for span_len in lengths:
            end = start + span_len
            if end > len(units):
                continue
            span_seconds = float(units[end - 1]["end"]) - float(units[start]["start"])
            if span_seconds > max(16.0, min(30.0, target_len * 1.65)):
                continue
            sample = unit_text[start:end]
            matcher = SequenceMatcher(None, target, sample, autojunk=False)
            similarity = matcher.ratio()
            matched_chars = sum(block.size for block in matcher.get_matching_blocks())
            coverage = matched_chars / max(1, target_len)
            if target in sample or sample in target:
                similarity = max(similarity, 0.55 + 0.4 * min(len(target), len(sample)) / max(len(target), len(sample)))
            threshold = 0.56 if target_len <= 4 else 0.44
            if similarity < threshold or coverage < 0.42:
                continue
            quality = sum(unit_alignment_quality(unit) for unit in units[start:end]) / span_len
            length_fit = 1.0 - min(1.0, abs(span_len - target_len) / max(1, target_len))
            prior_penalty = 0.0
            if expected_seconds is not None:
                prior_penalty = min(0.1, abs(float(units[start]["start"]) - expected_seconds) / 2400)
            score = similarity * 1.55 + coverage * 0.6 + quality * 0.2 + length_fit * 0.15 - prior_penalty
            candidates.append({
                "line_index": line_index, "start_index": start, "end_index": end,
                "seconds": float(units[start]["start"]), "end": float(units[end - 1]["end"]),
                "similarity": similarity, "coverage": min(1.0, coverage), "quality": quality, "score": score,
                "timing_source": "word" if all(unit.get("timing_source") == "word" for unit in units[start:end]) else "segment",
            })
    candidates.sort(key=lambda item: (-item["score"], item["start_index"], item["end_index"]))
    selected = []
    for candidate in candidates:
        if any(abs(candidate["start_index"] - kept["start_index"]) <= 2 and abs(candidate["end_index"] - kept["end_index"]) <= 2 for kept in selected):
            continue
        selected.append(candidate)
        if len(selected) >= top_k:
            break
    return sorted(selected, key=lambda item: (item["start_index"], item["end_index"]))


def select_monotonic_alignment(candidate_rows, active_indexes, unit_count, beam_width=96):
    states = [(0.0, -1, [])]
    for active_position, line_index in enumerate(active_indexes):
        next_states = []
        for score, last_end, path in states:
            next_states.append((score - 0.58, last_end, path))
            for candidate in candidate_rows[active_position]:
                if candidate["start_index"] < last_end:
                    continue
                asr_gap = candidate["start_index"] - last_end if last_end >= 0 else candidate["start_index"]
                gap_penalty = min(0.34, asr_gap * 0.0012)
                next_states.append((score + candidate["score"] - gap_penalty, candidate["end_index"], path + [candidate]))
        deduped = {}
        for state in next_states:
            key = state[1]
            if key not in deduped or state[0] > deduped[key][0]:
                deduped[key] = state
        states = sorted(
            deduped.values(),
            key=lambda state: state[0] - min(0.2, max(0, unit_count - state[1]) * 0.0001),
            reverse=True,
        )[:beam_width]
    return max(states, key=lambda state: state[0])[2] if states else []


def alignment_metrics(lyrics, active_indexes, units, matches):
    matched_by_line = {item["line_index"]: item for item in matches}
    total_chars = sum(len(normalize_alignment_text(lyrics[index].get("text"))) for index in active_indexes)
    matched_chars = sum(
        len(normalize_alignment_text(lyrics[index].get("text"))) * matched_by_line[index]["coverage"]
        for index in active_indexes if index in matched_by_line
    )
    similarities = [item["similarity"] for item in matches]
    qualities = [item["quality"] for item in matches]
    unmatched_run = max_run = 0
    for index in active_indexes:
        if index in matched_by_line:
            unmatched_run = 0
        else:
            unmatched_run += 1
            max_run = max(max_run, unmatched_run)
    unit_span = float(units[-1]["end"]) - float(units[0]["start"]) if units else 0
    time_span = matches[-1]["end"] - matches[0]["seconds"] if len(matches) >= 2 else 0
    regions = set()
    if unit_span > 0:
        for item in matches:
            regions.add(min(2, int(3 * (item["seconds"] - units[0]["start"]) / unit_span)))
    return {
        "matched_count": len(matches),
        "line_coverage": len(matches) / max(1, len(active_indexes)),
        "character_coverage": matched_chars / max(1, total_chars),
        "avg_similarity": sum(similarities) / max(1, len(similarities)),
        "time_coverage": time_span / max(0.001, unit_span),
        "max_unmatched_run": max_run,
        "asr_quality": sum(qualities) / max(1, len(qualities)),
        "time_regions": len(regions),
        "strong_anchor": max(similarities, default=0),
    }


def alignment_rejection_reason(metrics, line_count):
    matched = metrics["matched_count"]
    if matched == 0:
        return "no_matching_lines"
    if line_count == 1:
        return None if metrics["avg_similarity"] >= 0.72 and metrics["asr_quality"] >= 0.45 else "weak_single_line_match"
    if line_count <= 3:
        if matched < 2 or metrics["line_coverage"] < 0.66:
            return "not_enough_matched_lines"
        if metrics["character_coverage"] < 0.6:
            return "insufficient_character_coverage"
    elif line_count <= 6:
        if matched < 3 or metrics["line_coverage"] < 0.5:
            return "not_enough_matched_lines"
        if metrics["character_coverage"] < 0.52:
            return "insufficient_character_coverage"
    else:
        if matched < 5 or metrics["line_coverage"] < 0.55:
            return "insufficient_line_coverage"
        if metrics["character_coverage"] < 0.55:
            return "insufficient_character_coverage"
    if metrics["avg_similarity"] < 0.68:
        return "low_text_similarity"
    if metrics["asr_quality"] < 0.42:
        return "low_asr_quality"
    if metrics["max_unmatched_run"] > min(6, max(2, math.ceil(line_count * 0.2))):
        return "too_many_consecutive_unmatched_lines"
    if line_count >= 10 and (metrics["time_regions"] < 3 or metrics["time_coverage"] < 0.5):
        return "matches_not_time_distributed"
    return None


def interpolate_alignment_timeline(lyrics, active_indexes, matches):
    matched_by_line = {item["line_index"]: item for item in matches}
    active_positions = {index: position for position, index in enumerate(active_indexes)}
    matched_positions = sorted(active_positions[index] for index in matched_by_line)
    if not matched_positions:
        return None, 0, "no_matching_lines"
    if matched_positions[0] != 0 or matched_positions[-1] != len(active_indexes) - 1:
        return None, 0, "unbounded_edge_interpolation"
    timeline = [None] * len(lyrics)
    for index, match in matched_by_line.items():
        line = lyric_line(match["seconds"], lyrics[index].get("text"))
        line["end"] = match["end"]
        line["confidence"] = round(match["similarity"] * match["quality"], 3)
        timeline[index] = line
    interpolated = 0
    for left_pos, right_pos in zip(matched_positions, matched_positions[1:]):
        if right_pos == left_pos + 1:
            continue
        left_index = active_indexes[left_pos]
        right_index = active_indexes[right_pos]
        missing = active_indexes[left_pos + 1:right_pos]
        weights = [max(1, len(normalize_alignment_text(lyrics[index].get("text")))) for index in missing]
        right_weight = max(1, len(normalize_alignment_text(lyrics[right_index].get("text"))))
        denominator = sum(weights) + right_weight
        elapsed = 0
        gap = timeline[right_index]["seconds"] - timeline[left_index]["seconds"]
        if gap <= 0.35 * len(missing):
            return None, interpolated, "insufficient_interpolation_space"
        for index, weight in zip(missing, weights):
            elapsed += weight
            timeline[index] = lyric_line(timeline[left_index]["seconds"] + gap * elapsed / denominator, lyrics[index].get("text"))
            timeline[index]["confidence"] = round(min(timeline[left_index]["confidence"], timeline[right_index]["confidence"]) * 0.65, 3)
            interpolated += 1
    for index, line in enumerate(timeline):
        if line is not None:
            continue
        previous = next((timeline[i] for i in range(index - 1, -1, -1) if timeline[i] is not None), None)
        following = next((timeline[i] for i in range(index + 1, len(timeline)) if timeline[i] is not None), None)
        if not previous or not following:
            return None, interpolated, "unbounded_non_lyric_line"
        timeline[index] = lyric_line((previous["seconds"] + following["seconds"]) / 2, lyrics[index].get("text"))
        timeline[index]["confidence"] = round(min(previous.get("confidence", 0.5), following.get("confidence", 0.5)) * 0.5, 3)
    previous_seconds = -0.001
    for line in timeline:
        if line["seconds"] <= previous_seconds:
            if previous_seconds - line["seconds"] > 0.05:
                return None, interpolated, "non_monotonic_alignment"
            line["seconds"] = previous_seconds + 0.01
            line["time"] = seconds_to_time(line["seconds"])
        previous_seconds = line["seconds"]
    return timeline, interpolated, None


def align_lyrics_to_recognized_timeline(lyrics, anchors):
    units = anchor_time_units(anchors)
    active_indexes = [index for index, line in enumerate(lyrics or []) if alignment_line_is_usable(line.get("text"))]
    has_words = any(unit.get("timing_source") == "word" for unit in units)
    mode = "asr_word_sequence_timeline" if has_words else "asr_segment_sequence_timeline"
    if not lyrics or not units or not active_indexes:
        return lyrics, {"mode": "unverified", "aligned_to_video": False, "matched": 0, "matched_count": 0, "reason": "missing_alignment_input"}
    candidate_rows = [
        lyric_span_candidates(index, lyrics[index].get("text"), units, lyrics[index].get("seconds"))
        for index in active_indexes
    ]
    matches = select_monotonic_alignment(candidate_rows, active_indexes, len(units))
    metrics = alignment_metrics(lyrics, active_indexes, units, matches)
    reason = alignment_rejection_reason(metrics, len(active_indexes))
    timeline = None
    interpolated = 0
    if not reason:
        timeline, interpolated, reason = interpolate_alignment_timeline(lyrics, active_indexes, matches)
    confidence = (
        metrics["line_coverage"] * 0.24 + metrics["character_coverage"] * 0.24 +
        metrics["avg_similarity"] * 0.3 + metrics["time_coverage"] * 0.12 + metrics["asr_quality"] * 0.1
    )
    summaries = [{
        "line": item["line_index"], "start": round(item["seconds"], 3), "end": round(item["end"], 3),
        "similarity": round(item["similarity"], 3), "coverage": round(item["coverage"], 3),
        "confidence": round(item["similarity"] * item["quality"], 3), "timing": item.get("timing_source"),
    } for item in matches]
    meta = {
        "mode": mode if not reason else "unverified",
        "aligned_to_video": not bool(reason),
        "matched": metrics["matched_count"],
        "matched_count": metrics["matched_count"],
        "interpolated": interpolated,
        "interpolated_count": interpolated,
        "coverage": round(metrics["line_coverage"], 3),
        "line_coverage": round(metrics["line_coverage"], 3),
        "charCoverage": round(metrics["character_coverage"], 3),
        "character_coverage": round(metrics["character_coverage"], 3),
        "avgSimilarity": round(metrics["avg_similarity"], 3),
        "avg_similarity": round(metrics["avg_similarity"], 3),
        "timeSpanCoverage": round(metrics["time_coverage"], 3),
        "time_coverage": round(metrics["time_coverage"], 3),
        "maxUnmatchedRun": metrics["max_unmatched_run"],
        "max_unmatched_run": metrics["max_unmatched_run"],
        "timeRegions": metrics["time_regions"],
        "asrQuality": round(metrics["asr_quality"], 3),
        "asr_quality": round(metrics["asr_quality"], 3),
        "confidence": round(confidence, 3),
        "confidence_basis": "online_text_full_asr_sequence",
        "lineMatches": summaries,
    }
    if reason:
        meta["reason"] = reason
        return lyrics, meta
    return timeline, meta


def build_precise_timeline_from_units(lyrics, anchors, expected_offset=None):
    return align_lyrics_to_recognized_timeline(lyrics, anchors)


def align_lrc_with_anchor_lines(lyrics, anchors):
    return align_lyrics_to_recognized_timeline(lyrics, anchors)


def best_phrase_span(lyric_text, anchor_text):
    lyric = normalize_alignment_text(lyric_text)
    anchor = normalize_alignment_text(anchor_text)
    if not lyric or not anchor:
        return None
    if anchor in lyric:
        start = lyric.index(anchor)
        return {"start_ratio": start / len(lyric), "end_ratio": (start + len(anchor)) / len(lyric), "similarity": 1.0}

    best = None
    minimum_length = max(1, round(len(anchor) * 0.65))
    maximum_length = min(len(lyric), max(minimum_length, round(len(anchor) * 1.35)))
    for span_length in range(minimum_length, maximum_length + 1):
        for start in range(0, len(lyric) - span_length + 1):
            similarity = SequenceMatcher(None, lyric[start:start + span_length], anchor).ratio()
            candidate = (similarity, -abs(span_length - len(anchor)), -start, start, start + span_length)
            if not best or candidate > best:
                best = candidate
    if not best:
        return None
    return {
        "start_ratio": best[3] / len(lyric),
        "end_ratio": best[4] / len(lyric),
        "similarity": best[0],
    }


def usable_lyric_line_duration(lyrics, lyric_index):
    lyric = lyrics[lyric_index]
    if is_credit_lyric_line(lyric.get("text")):
        return 0.0
    line_start = float(lyric.get("seconds") or 0)
    next_line = next((line for line in lyrics[lyric_index + 1:] if not is_credit_lyric_line(line.get("text"))), None)
    if next_line is None:
        return 0.0
    gap = float(next_line.get("seconds") or 0) - line_start
    if gap <= 0:
        return 0.0
    text_length = len(normalize_alignment_text(lyric.get("text")))
    singing_duration_cap = min(12.0, max(2.0, text_length * 0.65))
    return min(gap, singing_duration_cap)


def match_sampled_anchors_to_lyrics(lyrics, sampled_anchors, candidate_duration, media_duration):
    if not lyrics or not sampled_anchors:
        return []
    candidate_duration = float(candidate_duration or lyrics_duration(lyrics) or 0)
    media_duration = float(media_duration or 0)
    duration_ratio = candidate_duration / media_duration if candidate_duration > 0 and media_duration > 0 else 1.0
    matches = []
    minimum_lyric_index = 0
    for anchor in sorted(sampled_anchors, key=lambda item: float(item.get("seconds", item.get("start", 0)) or 0)):
        audio_seconds = float(anchor.get("seconds", anchor.get("start", 0)) or 0)
        approximate_lyric_seconds = audio_seconds * duration_ratio
        search_radius = max(18.0, candidate_duration * 0.1)
        best = None
        for lyric_index in range(minimum_lyric_index, len(lyrics)):
            lyric = lyrics[lyric_index]
            if is_credit_lyric_line(lyric.get("text")):
                continue
            lyric_seconds = float(lyric.get("seconds") or 0)
            if lyric_seconds < approximate_lyric_seconds - search_radius:
                continue
            if lyric_seconds > approximate_lyric_seconds + search_radius:
                break
            normalized_anchor_length = len(normalize_alignment_text(anchor.get("text")))
            phrase_span = best_phrase_span(lyric.get("text"), anchor.get("text"))
            if not phrase_span:
                continue
            phrase_confidence = float(phrase_span["similarity"]) * (0.72 + 0.28 * min(1.0, normalized_anchor_length / 8))
            similarity = max(text_similarity(lyric.get("text"), anchor.get("text")), phrase_confidence)
            threshold = 0.82 if normalized_anchor_length <= 4 else (0.76 if normalized_anchor_length <= 7 else 0.72)
            if similarity < threshold:
                continue
            phrase_start_ratio = min(1.0, max(0.0, float(phrase_span["start_ratio"])))
            phrase_end_ratio = min(1.0, max(phrase_start_ratio, float(phrase_span["end_ratio"])))
            expected_lyric_seconds = lyric_seconds + phrase_start_ratio * usable_lyric_line_duration(lyrics, lyric_index)
            previous = matches[-1] if matches else None
            if previous and expected_lyric_seconds <= float(previous["expected_lyric_seconds"]):
                continue
            if previous and lyric_index == previous["lyric_index"]:
                overlap = max(0.0, min(phrase_end_ratio, previous["phrase_end_ratio"]) - max(phrase_start_ratio, previous["phrase_start_ratio"]))
                shorter_span = min(phrase_end_ratio - phrase_start_ratio, previous["phrase_end_ratio"] - previous["phrase_start_ratio"])
                if shorter_span > 0 and overlap / shorter_span >= 0.6:
                    continue
            distance_penalty = min(0.08, abs(expected_lyric_seconds - approximate_lyric_seconds) / max(1.0, search_radius) * 0.08)
            candidate = (similarity - distance_penalty, similarity, lyric_index, lyric_seconds, expected_lyric_seconds, phrase_start_ratio, phrase_end_ratio)
            if not best or candidate[0] > best[0]:
                best = candidate
        if best:
            _, similarity, lyric_index, lyric_seconds, expected_lyric_seconds, phrase_start_ratio, phrase_end_ratio = best
            matches.append({
                "lyric_seconds": lyric_seconds,
                "line_seconds": lyric_seconds,
                "expected_lyric_seconds": expected_lyric_seconds,
                "phrase_start_ratio": phrase_start_ratio,
                "phrase_end_ratio": phrase_end_ratio,
                "audio_seconds": audio_seconds,
                "similarity": similarity,
                "window_index": int(anchor.get("window_index", 0)),
                "lyric_index": lyric_index,
                "text": lyrics[lyric_index].get("text"),
                "anchor_text": anchor.get("text"),
            })
            minimum_lyric_index = lyric_index
    return matches


def timeline_point_seconds(item):
    return float(item.get("expected_lyric_seconds", item["lyric_seconds"]))


def timeline_fit_metrics(matches, scale, offset):
    residuals = [float(item["audio_seconds"]) - (scale * timeline_point_seconds(item) + offset) for item in matches]
    if not residuals:
        return {"rmse": math.inf, "mae": math.inf, "max_residual": math.inf}
    return {
        "rmse": math.sqrt(sum(value * value for value in residuals) / len(residuals)),
        "mae": sum(abs(value) for value in residuals) / len(residuals),
        "max_residual": max(abs(value) for value in residuals),
    }


def robust_timeline_models(matches):
    points = sorted(matches or [], key=lambda item: (timeline_point_seconds(item), float(item["audio_seconds"])))
    if not points:
        return {"mode": "unverified", "reason": "no_matches", "matched_count": 0}

    fixed_offset = median(float(item["audio_seconds"]) - timeline_point_seconds(item) for item in points)
    fixed_inliers = [item for item in points if abs(float(item["audio_seconds"]) - timeline_point_seconds(item) - fixed_offset) <= 2.5]
    if fixed_inliers:
        fixed_offset = median(float(item["audio_seconds"]) - timeline_point_seconds(item) for item in fixed_inliers)
    fixed_metrics = timeline_fit_metrics(fixed_inliers, 1.0, fixed_offset)

    slopes = []
    for left_index, left in enumerate(points):
        for right in points[left_index + 1:]:
            delta = timeline_point_seconds(right) - timeline_point_seconds(left)
            if abs(delta) >= 1.0:
                slopes.append((float(right["audio_seconds"]) - float(left["audio_seconds"])) / delta)
    linear_scale = median(slopes) if slopes else 1.0
    linear_offset = median(float(item["audio_seconds"]) - linear_scale * timeline_point_seconds(item) for item in points)
    linear_inliers = [item for item in points if abs(float(item["audio_seconds"]) - (linear_scale * timeline_point_seconds(item) + linear_offset)) <= 2.0]
    if len(linear_inliers) >= 2:
        inlier_slopes = []
        for left_index, left in enumerate(linear_inliers):
            for right in linear_inliers[left_index + 1:]:
                delta = timeline_point_seconds(right) - timeline_point_seconds(left)
                if abs(delta) >= 1.0:
                    inlier_slopes.append((float(right["audio_seconds"]) - float(left["audio_seconds"])) / delta)
        if inlier_slopes:
            linear_scale = median(inlier_slopes)
        linear_offset = median(float(item["audio_seconds"]) - linear_scale * timeline_point_seconds(item) for item in linear_inliers)
    linear_metrics = timeline_fit_metrics(linear_inliers, linear_scale, linear_offset)

    def summary(items):
        return {
            "matched_count": len(items),
            "window_count": len({int(item["window_index"]) for item in items}),
            "avg_similarity": sum(float(item["similarity"]) for item in items) / max(1, len(items)),
            "max_similarity": max((float(item["similarity"]) for item in items), default=0),
            "span": (max(timeline_point_seconds(item) for item in items) - min(timeline_point_seconds(item) for item in items)) if items else 0,
        }

    fixed_summary = summary(fixed_inliers)
    linear_summary = summary(linear_inliers)
    fixed_text_ok = (
        fixed_summary["matched_count"] >= 4
        and fixed_summary["window_count"] >= 2
        and fixed_summary["avg_similarity"] >= 0.78
        and fixed_metrics["rmse"] <= 1.5
        and fixed_metrics["mae"] <= 1.0
        and fixed_summary["span"] >= 45
        and abs(fixed_offset) <= 45
    )
    linear_text_ok = (
        linear_summary["matched_count"] >= 6
        and linear_summary["window_count"] >= 3
        and linear_summary["avg_similarity"] >= 0.80
        and linear_metrics["rmse"] <= 1.25
        and linear_metrics["mae"] <= 0.85
        and linear_summary["span"] >= 90
        and 0.94 <= linear_scale <= 1.06
        and fixed_metrics["rmse"] > 0
        and linear_metrics["rmse"] <= fixed_metrics["rmse"] * 0.70
        and abs(linear_scale - 1) >= 0.0025
    )
    linear_temporal_ok = (
        linear_summary["matched_count"] >= 6
        and linear_summary["window_count"] == 3
        and linear_summary["avg_similarity"] >= 0.74
        and linear_summary["max_similarity"] >= 0.90
        and linear_summary["span"] >= 90
        and 0.94 <= linear_scale <= 1.06
        and abs(linear_scale - 1) >= 0.0025
        and abs(linear_offset) <= 45
        and linear_metrics["rmse"] <= 0.65
        and linear_metrics["mae"] <= 0.45
        and linear_metrics["max_residual"] <= 1.25
        and fixed_metrics["rmse"] > 0
        and linear_metrics["rmse"] <= fixed_metrics["rmse"] * 0.70
    )
    fixed_temporal_ok = (
        not linear_temporal_ok
        and fixed_summary["matched_count"] >= 5
        and fixed_summary["window_count"] == 3
        and fixed_summary["avg_similarity"] >= 0.74
        and fixed_summary["max_similarity"] >= 0.90
        and fixed_summary["span"] >= 90
        and fixed_metrics["rmse"] <= 0.75
        and fixed_metrics["mae"] <= 0.50
        and fixed_metrics["max_residual"] <= 1.25
        and abs(fixed_offset) <= 45
    )
    linear_ok = linear_text_ok or linear_temporal_ok
    fixed_ok = fixed_text_ok or fixed_temporal_ok
    common = {"matches": points, "fixed_model": {"scale": 1.0, "offset": round(fixed_offset, 6), **fixed_summary, **fixed_metrics}}
    if linear_ok:
        return {"mode": "sampled_linear_timeline", "scale": linear_scale, "offset": linear_offset, "confidence_basis": "text_and_time" if linear_text_ok else "temporal_consensus", **linear_summary, **linear_metrics, **common}
    if fixed_ok:
        return {"mode": "sampled_fixed_offset", "scale": 1.0, "offset": fixed_offset, "confidence_basis": "text_and_time" if fixed_text_ok else "temporal_consensus", **fixed_summary, **fixed_metrics, **common}
    return {"mode": "unverified", "reason": "low_confidence", **fixed_summary, **fixed_metrics, **common}


def apply_timeline_transform(lyrics, scale, offset):
    transformed = []
    previous_seconds = -0.001
    for source in lyrics or []:
        line = dict(source)
        seconds = max(0.0, float(source.get("seconds") or 0) * float(scale) + float(offset))
        if seconds <= previous_seconds:
            seconds = previous_seconds + 0.001
        line["seconds"] = seconds
        line["time"] = seconds_to_time(seconds)
        if "end" in line:
            end = max(0.0, float(source.get("end") or 0) * float(scale) + float(offset))
            line["end"] = max(seconds + 0.001, end)
        transformed.append(line)
        previous_seconds = seconds
    return transformed


def lyrics_look_untimed(lines):
    if not lines:
        return False
    expected = [index * 6 for index in range(min(len(lines), 8))]
    actual = [round(float(line.get("seconds") or 0), 1) for line in lines[:8]]
    return actual == expected


def rebuild_plain_lyrics_timeline(lyrics, anchors):
    return align_lyrics_to_recognized_timeline(lyrics, anchors)


def tool_available(name):
    return shutil.which(name) is not None


def get_yt_dlp_command():
    if VENDOR_PYTHON.exists():
        return [sys.executable, "-m", "yt_dlp"]
    vendored_exe = VENDOR_PYTHON / "bin" / "yt-dlp.exe"
    if vendored_exe.exists():
        return [str(vendored_exe)]
    executable = shutil.which("yt-dlp")
    if executable:
        return [executable]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        return None


def subprocess_env():
    env = os.environ.copy()
    if VENDOR_PYTHON.exists():
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(VENDOR_PYTHON) + (os.pathsep + existing if existing else "")
    return env


def get_ffmpeg_path():
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def get_node_command():
    bundled = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"
    if bundled.exists():
        return str(bundled)
    return shutil.which("node")


VIDEO_STREAM_CACHE = {}


def get_video_stream_url(url):
    cached = VIDEO_STREAM_CACHE.get(url)
    if cached and cached.get("expires_at", 0) > time.time():
        return cached.get("url")
    yt_dlp_command = get_yt_dlp_command()
    if not yt_dlp_command:
        return None
    command = yt_dlp_command + ["-f", "best[ext=mp4]/best", "--get-url", url]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=45,
        env=subprocess_env(),
    )
    if completed.returncode != 0:
        return None
    stream_url = next((line.strip() for line in (completed.stdout or "").splitlines() if line.strip().startswith("http")), "")
    if not stream_url:
        return None
    VIDEO_STREAM_CACHE[url] = {"url": stream_url, "expires_at": time.time() + 1800}
    return stream_url


def run_ocr_on_images(image_paths):
    node = get_node_command()
    script = Path(__file__).resolve().parent / "ocr_frame.js"
    node_modules = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "node_modules"
    if not node or not script.exists() or not image_paths:
        return {}
    env = os.environ.copy()
    if node_modules.exists():
        pnpm_modules = node_modules / ".pnpm" / "node_modules"
        extra_paths = [str(node_modules)]
        if pnpm_modules.exists():
            extra_paths.append(str(pnpm_modules))
        pnpm_root = node_modules / ".pnpm"
        if pnpm_root.exists():
            for package_dir in pnpm_root.glob("*node_modules"):
                extra_paths.append(str(package_dir))
        env["NODE_PATH"] = os.pathsep.join(extra_paths)
    completed = subprocess.run(
        [node, str(script), *[str(path) for path in image_paths]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=max(45, len(image_paths) * 20),
        env=env,
    )
    output = (completed.stdout or completed.stderr or "").strip()
    try:
        payload = json.loads(output.splitlines()[-1])
        return {str(item.get("path")): item.get("text") or "" for item in payload.get("results") or []}
    except Exception:
        return {}


def clean_ocr_text(text):
    text = re.sub(r"[\[\]{}<>|_=~`^]+", " ", text or "")
    text = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    ascii_count = len(re.findall(r"[A-Za-z]", text))
    if len(text) < 4:
        return ""
    if cjk_count < 2 and ascii_count < 6:
        return ""
    if cjk_count and cjk_count / max(1, len(text)) < 0.25:
        return ""
    return text[:80]


def ocr_text_quality(text):
    text = clean_ocr_text(text)
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    return cjk_count * 2 + len(text)


def dedupe_ocr_lines(lines):
    best_by_time = {}
    for line in lines:
        text = clean_ocr_text(line.get("text"))
        if not text:
            continue
        seconds = float(line.get("seconds") or 0)
        current = best_by_time.get(seconds)
        if not current or ocr_text_quality(text) > ocr_text_quality(current.get("text")):
            best_by_time[seconds] = {"seconds": seconds, "text": text}

    deduped = []
    last_text = ""
    for line in sorted(best_by_time.values(), key=lambda item: item["seconds"]):
        text = line["text"]
        if text == last_text:
            continue
        if last_text and (text in last_text or last_text in text):
            if ocr_text_quality(text) <= ocr_text_quality(last_text):
                continue
            deduped[-1] = lyric_line(line.get("seconds"), text)
        else:
            deduped.append(lyric_line(line.get("seconds"), text))
        last_text = text
    return deduped


def extract_video_ocr_lines(url, yt_dlp_command, ffmpeg_path, tmp_dir):
    video_path = Path(tmp_dir) / "sample.mp4"
    download_command = yt_dlp_command + [
        "-f",
        "worstvideo+bestaudio/worst/best",
        "--max-filesize",
        "80M",
        "--merge-output-format",
        "mp4",
        "-o",
        str(video_path),
        url,
    ]
    download = subprocess.run(
        download_command,
        cwd=tmp_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=180,
        env=subprocess_env(),
    )
    candidates = list(Path(tmp_dir).glob("sample*.mp4"))
    if candidates:
        video_path = candidates[0]
    if not video_path.exists():
        reason = (download.stderr or download.stdout or "视频片段下载失败").strip()
        raise RuntimeError(reason[-500:])

    frames_dir = Path(tmp_dir) / "frames"
    frames_dir.mkdir(exist_ok=True)

    raw_lines = []
    regions = {
        "middle": "fps=1/5,crop=iw:ih*0.48:0:ih*0.24,scale=1400:-1",
        "lower": "fps=1/5,crop=iw:ih*0.38:0:ih*0.52,scale=1400:-1",
    }
    for region, crop_filter in regions.items():
        frame_pattern = str(frames_dir / f"{region}_%03d.jpg")
        subprocess.run(
            [ffmpeg_path, "-y", "-i", str(video_path), "-vf", crop_filter, "-frames:v", "10", frame_pattern],
            cwd=tmp_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=90,
        )
    frames = sorted(frames_dir.glob("*.jpg"))
    ocr_results = run_ocr_on_images(frames)
    for frame in frames:
        frame_match = re.search(r"_(\d+)\.jpg$", frame.name, re.I)
        if not frame_match:
            continue
        cleaned = clean_ocr_text(ocr_results.get(str(frame), ""))
        if cleaned:
            raw_lines.append({"seconds": (int(frame_match.group(1)) - 1) * 5, "text": cleaned})
    return dedupe_ocr_lines(raw_lines)


def attempt_video_recognition(url):
    attempts = []
    yt_dlp_command = get_yt_dlp_command()
    ffmpeg_path = get_ffmpeg_path()

    if not yt_dlp_command:
        attempts.append({
            "stage": "video_subtitle",
            "status": "missing_tool",
            "message": "需要安装视频字幕工具，才能读取视频自带字幕或自动字幕。"
        })
    else:
        with tempfile.TemporaryDirectory(prefix="sing-reactor-") as tmp_dir:
            output = str(Path(tmp_dir) / "%(id)s.%(ext)s")
            command = yt_dlp_command + [
                "--skip-download",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs",
                "zh-Hans,zh-CN,zh-Hant,zh-TW,zh,en",
                "--sub-format",
                "vtt",
                "-o",
                output,
                url,
            ]
            try:
                subprocess.run(
                    command,
                    cwd=tmp_dir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    timeout=60,
                    env=subprocess_env(),
                )
                for file_path in Path(tmp_dir).glob("*.vtt"):
                    parsed = parse_vtt(file_path.read_text(encoding="utf-8", errors="ignore"))
                    if parsed:
                        return parsed, {"provider": "yt-dlp", "trackName": file_path.name}, attempts + [{
                            "stage": "video_subtitle",
                            "status": "ok",
                            "message": "已从视频字幕中识别到逐句文本。"
                        }]
                attempts.append({
                    "stage": "video_subtitle",
                    "status": "no_result",
                    "message": "没有读取到可用的视频字幕。"
                })
            except Exception as exc:
                attempts.append({
                    "stage": "video_subtitle",
                    "status": "failed",
                    "message": f"读取视频字幕失败：{exc.__class__.__name__}"
                })

    if not ffmpeg_path:
        attempts.append({
            "stage": "video_ocr",
            "status": "missing_tool",
            "message": "需要安装视频抽帧工具，才能 OCR 识别画面歌词。"
        })
    else:
        if not yt_dlp_command:
            attempts.append({
                "stage": "video_ocr",
                "status": "missing_tool",
                "message": "OCR 需要视频下载工具和视频抽帧工具同时可用。"
            })
        else:
            with tempfile.TemporaryDirectory(prefix="sing-reactor-ocr-") as tmp_dir:
                try:
                    ocr_lines = extract_video_ocr_lines(url, yt_dlp_command, ffmpeg_path, tmp_dir)
                    if ocr_lines:
                        return ocr_lines, {"provider": "video_ocr", "trackName": "画面歌词识别"}, attempts + [{
                            "stage": "video_ocr",
                            "status": "ok",
                            "message": "已从视频画面中识别到歌词文本。"
                        }]
                    attempts.append({
                        "stage": "video_ocr",
                        "status": "no_result",
                        "message": "已尝试画面 OCR，但中文识别质量不足，暂不作为歌词使用。"
                    })
                except Exception as exc:
                    attempts.append({
                        "stage": "video_ocr",
                        "status": "failed",
                        "message": "画面 OCR 失败。"
                    })

    return [], None, attempts


def get_known_lyrics(title):
    normalized = normalize_song_query(title)
    for name, lyrics in KNOWN_LYRICS.items():
        if name == normalized:
            return lyrics
    return []


def build_no_subtitle_lines():
    return [
        lyric_line(0, "暂时没有匹配到歌词。"),
        lyric_line(6, "已经尝试自动搜索和视频识别，可以换一个歌名更明确或带字幕的视频。"),
    ]


def align_candidate_lyrics(candidate_lyrics, anchor_lines, platform_anchors=None, speech_meta=None):
    if not candidate_lyrics or not anchor_lines:
        return candidate_lyrics, None
    aligned, correction = align_lyrics_to_recognized_timeline(candidate_lyrics, anchor_lines)
    if correction:
        correction["anchorProvider"] = "bilibili_subtitle" if platform_anchors else (speech_meta or {}).get("provider")
    return aligned, correction


def synced_fallback_quality(meta, provider_name=""):
    correction = (meta or {}).get("correction") or {}
    mode = str(correction.get("mode") or "")
    aligned = bool(correction.get("aligned_to_video"))
    sampled = mode.startswith("sampled_")
    confidence = float(correction.get("confidence") or correction.get("avg_similarity") or correction.get("avgSimilarity") or 0)
    matched = int(correction.get("matched_count") or correction.get("matched") or 0)
    windows = int(correction.get("window_count") or 0)
    coverage = float(correction.get("coverage") or correction.get("line_coverage") or correction.get("character_coverage") or 0)
    provider_text_quality = 1 if provider_name == "netease" else 0
    return (int(aligned), int(sampled), confidence, matched, windows, coverage, provider_text_quality)


def provider_quality(provider):
    return float((LYRICS_CONFIG.get("provider_quality") or {}).get(provider, 0.5))


def candidate_match_type(song_metadata, candidate_meta):
    detected_artist = str(song_metadata.get("artist") or "")
    source_artist = str((candidate_meta or {}).get("artistName") or "")
    same_artist = not detected_artist or not source_artist or text_similarity(detected_artist, source_artist) >= 0.72
    if song_metadata.get("is_live"):
        return "live"
    if song_metadata.get("is_cover") or not same_artist:
        return "cover"
    if song_metadata.get("version"):
        return "version"
    return "exact"


def candidate_audio_score(correction):
    correction = correction or {}
    if not correction.get("aligned_to_video"):
        return 0.0
    confidence = float(correction.get("confidence") or correction.get("avg_similarity") or correction.get("avgSimilarity") or 0)
    line_coverage = float(correction.get("line_coverage") or correction.get("coverage") or 0)
    character_coverage = float(correction.get("character_coverage") or correction.get("charCoverage") or line_coverage)
    time_coverage = float(correction.get("time_coverage") or correction.get("timeSpanCoverage") or 0)
    return min(1.0, confidence * 0.55 + line_coverage * 0.2 + character_coverage * 0.15 + time_coverage * 0.1)


def candidate_total_score(provider, meta, correction):
    config = LYRICS_CONFIG.get("candidate_selection") or {}
    audio = candidate_audio_score(correction)
    metadata = float((meta or {}).get("score") or (meta or {}).get("metadata_score") or 0)
    return (
        audio * float(config.get("audio_weight", 0.8))
        + metadata * float(config.get("metadata_weight", 0.14))
        + provider_quality(provider) * float(config.get("provider_weight", 0.06))
    )


def cover_candidate_has_strong_audio(song_metadata, meta, correction):
    if candidate_match_type(song_metadata, meta) != "cover":
        return True
    config = LYRICS_CONFIG.get("candidate_selection") or {}
    return (
        float((correction or {}).get("confidence") or 0) >= float(config.get("cover_min_confidence", 0.78))
        and float((correction or {}).get("line_coverage") or (correction or {}).get("coverage") or 0) >= float(config.get("cover_min_line_coverage", 0.55))
        and int((correction or {}).get("matched_count") or (correction or {}).get("matched") or 0) >= int(config.get("cover_min_matches", 5))
    )


def candidate_requires_verified_timeline(song_metadata, meta):
    version_text = " ".join(str(value or "") for value in (
        song_metadata.get("version"),
        (meta or {}).get("trackName"),
        (meta or {}).get("version"),
    ))
    return bool(
        song_metadata.get("is_live")
        or song_metadata.get("is_cover")
        or song_metadata.get("is_instrumental")
        or re.search(r"\blive\b|\bcover\b|\bremix\b|现场|現場|演唱会|演唱會|翻唱|伴奏|剪辑|剪輯|变速|變速", version_text, re.I)
    )


def standardized_lyrics_meta(provider, meta, correction, song_metadata, attempted_providers, rejected_candidates, mode=None):
    correction = dict(correction or {})
    internal_mode = str(correction.get("mode") or "")
    if mode is None:
        if internal_mode.startswith("sampled_") or internal_mode == "original_synced_fallback":
            mode = "lrc_timeline_synced"
        elif correction.get("aligned_to_video"):
            mode = "lyrics_timeline_rebuilt"
        else:
            mode = "text_only"
    match_type = candidate_match_type(song_metadata, meta)
    metadata = {
        "songTitle": song_metadata.get("song_title"),
        "song_title": song_metadata.get("song_title"),
        "detectedArtist": song_metadata.get("artist"),
        "detected_artist": song_metadata.get("artist"),
        "sourceArtist": (meta or {}).get("artistName") or "",
        "source_artist": (meta or {}).get("artistName") or "",
        "language": song_metadata.get("language"),
        "version": song_metadata.get("version"),
        "page": song_metadata.get("page"),
        "cid": song_metadata.get("cid"),
    }
    timeline = {
        "model": internal_mode or None,
        "offset": correction.get("offset"), "scale": correction.get("scale", 1.0),
        "rmse": correction.get("rmse"),
        "anchorCount": correction.get("matched_count") or correction.get("matched") or 0,
        "anchor_count": correction.get("matched_count") or correction.get("matched") or 0,
    }
    attempted = list(dict.fromkeys(attempted_providers))
    return {
        **(meta or {}),
        "provider": provider,
        "mode": mode,
        "confidence": round(candidate_audio_score(correction), 3),
        "matchType": match_type,
        "match_type": match_type,
        "metadata": metadata,
        "timeline": timeline,
        "attemptedProviders": attempted,
        "attempted_providers": attempted,
        "rejectedCandidates": rejected_candidates,
        "pipelineVersion": LYRICS_PIPELINE_VERSION,
        "correction": correction,
    }


def whisper_fallback_lines(anchors):
    lines = []
    for anchor in anchors or []:
        text = str(anchor.get("text") or "").strip()
        if text:
            line = lyric_line(anchor.get("start", anchor.get("seconds", 0)), text)
            if anchor.get("end") is not None:
                line["end"] = float(anchor["end"])
            lines.append(line)
    return lines


def parse_manual_lyrics(lyric_text):
    lyrics = parse_lrc(lyric_text or "")
    if lyrics:
        return clean_lyric_lines(lyrics)
    return clean_lyric_lines([
        lyric_line(index * 6, line.strip())
        for index, line in enumerate((lyric_text or "").splitlines())
        if line.strip()
    ])


def align_manual_lyrics(url, lyric_text):
    try:
        url = normalize_bilibili_url(url, resolve_redirects=True)
    except (UnsafeUrlError, OSError):
        return {"ok": False, "error": "unsupported_url"}, 400
    lyrics = parse_manual_lyrics(lyric_text)
    if not lyrics:
        return {"ok": False, "error": "missing_lyrics"}, 400
    anchors, speech_meta, attempts = transcribe_audio_anchors(url, infer_lyrics_language_hint(lyrics))
    if not anchors:
        return {"ok": True, "lyrics": lyrics, "lyricsMeta": {"mode": "manual_unaligned", "reason": "missing_anchors"}, "recognitionAttempts": attempts}, 200
    aligned, correction = align_candidate_lyrics(lyrics, anchors, None, speech_meta)
    return {
        "ok": True,
        "lyrics": aligned,
        "lyricsMeta": correction,
        "recognitionAttempts": attempts,
        "anchorProvider": (speech_meta or {}).get("provider"),
    }, 200


def identify(url):
    if not url:
        return {"ok": False, "error": "missing_url"}, 400
    try:
        url = normalize_bilibili_url(url, resolve_redirects=True)
    except (UnsafeUrlError, OSError):
        return {"ok": False, "error": "unsupported_url"}, 400

    bvid = extract_bvid(url)
    title = infer_title_from_url(url)
    cid = KNOWN_CIDS.get(bvid)
    artist = KNOWN_ARTISTS.get(bvid)
    page_number = extract_page_number(url)
    song_metadata = {"song_title": normalize_song_query(title), "artist": artist or "", "language": "auto", "version": "", "version_terms": []}
    video_offset_seconds = KNOWN_VIDEO_OFFSETS.get(bvid, 0)
    bilibili_duration = 0
    title_source = "known" if bvid in KNOWN_TITLES else "url"
    lyrics = []
    lyrics_source = "none"
    lyrics_meta = None
    platform_anchors = []
    speech_anchors = []
    speech_meta = None
    speech_attempted = False
    speech_language_hint = None
    synced_fallback = None
    warning = None
    recognition_attempts = []

    try:
        view = fetch_video_view(bvid)
        if view:
            page, page_number, _ = select_bilibili_page(view, url)
            title = KNOWN_TITLES.get(bvid) or clean_title(page.get("part") or view.get("title"))
            artist = KNOWN_ARTISTS.get(bvid) or artist or infer_artist_from_title(page.get("part")) or infer_artist_from_title(view.get("title"))
            cid = page.get("cid") or cid
            bilibili_duration = float(page.get("duration") or view.get("duration") or 0)
            title_source = "bilibili_api"
            platform_anchors = fetch_subtitle_lines(bvid, cid)
            song_metadata = build_song_metadata(title, artist, view, page)
    except Exception as exc:
        warning = f"bilibili_api_failed: {exc.__class__.__name__}"

    if title_source == "url":
        try:
            fetched = fetch_bilibili_title(url)
            if fetched:
                title = fetched
                artist = artist or infer_artist_from_title(fetched)
                title_source = "bilibili_page"
        except Exception as exc:
            warning = warning or f"title_fetch_failed: {exc.__class__.__name__}"
    if title_source == "url":
        fetched = fetch_title_with_media_tool(url)
        if fetched:
            title = KNOWN_TITLES.get(bvid) or fetched
            artist = artist or infer_artist_from_title(fetched)
            title_source = "media_tool"

    candidates = []
    attempted_providers = []
    rejected_candidates = []
    provider_artist = None if song_metadata.get("is_cover") else (song_metadata.get("artist") or artist)
    if platform_anchors:
        candidates.append(("bilibili_subtitle", platform_anchors, {
            "provider": "bilibili_subtitle", "trackName": song_metadata.get("song_title"),
            "artistName": song_metadata.get("artist"), "duration": bilibili_duration,
            "score": 1.0, "mode": "synced_lrc",
        }))
    for provider_name, provider in (("netease", fetch_lyrics_from_netease), ("lrclib", fetch_lyrics_from_lrclib)):
        attempted_providers.append(provider_name)
        try:
            candidate_lyrics, candidate_meta = provider(
                song_metadata.get("song_title") or title, provider_artist, bilibili_duration
            )
            if candidate_lyrics:
                candidate_meta = dict(candidate_meta or {})
                candidate_meta.setdefault("provider", provider_name)
                candidate_meta.setdefault("mode", "synced_lrc" if provider_name == "netease" else candidate_meta.get("mode"))
                candidates.append((provider_name, candidate_lyrics, candidate_meta))
        except Exception as exc:
            warning = warning or f"{provider_name}_lyrics_failed: {exc.__class__.__name__}"
    known_lyrics = get_known_lyrics(song_metadata.get("song_title") or title)
    if known_lyrics:
        candidates.append(("known_lyrics", known_lyrics, {
            "provider": "known_lyrics", "trackName": song_metadata.get("song_title"),
            "artistName": song_metadata.get("artist"), "score": 0.8, "mode": "text_only",
        }))
    attempted_providers = (["bilibili_subtitle"] if platform_anchors else []) + attempted_providers + (["known_lyrics"] if known_lyrics else [])

    language_hint = song_metadata.get("language")
    if language_hint == "auto":
        language_hint = None
    if candidates or bool((LYRICS_CONFIG.get("fallback") or {}).get("allow_whisper_text", True)):
        speech_anchors, speech_meta, speech_attempts = transcribe_audio_anchors(url, language_hint)
        speech_attempted = True
        speech_language_hint = language_hint
        recognition_attempts.extend(speech_attempts)

    evaluated = []
    fallbacks = []
    for provider_name, candidate_lyrics, candidate_meta in candidates:
        correction = None
        output_lyrics = candidate_lyrics
        if speech_anchors:
            output_lyrics, correction = align_candidate_lyrics(candidate_lyrics, speech_anchors, None, speech_meta)
        cover_allowed = cover_candidate_has_strong_audio(song_metadata, candidate_meta, correction)
        if correction and correction.get("aligned_to_video") and cover_allowed:
            score = candidate_total_score(provider_name, candidate_meta, correction)
            evaluated.append((score, provider_name, output_lyrics, candidate_meta, correction))
            continue
        reason = "weak_cover_audio_evidence" if not cover_allowed else ((correction or {}).get("reason") or "missing_audio_evidence")
        rejected_candidates.append({
            "provider": provider_name, "trackName": candidate_meta.get("trackName"),
            "artistName": candidate_meta.get("artistName"), "reason": reason,
        })
        if not cover_allowed:
            continue
        text_only = candidate_meta.get("mode") == "text_only"
        if text_only:
            continue
        lyric_times = [float(line.get("seconds") or 0) for line in candidate_lyrics]
        lyric_span = max(lyric_times) - min(lyric_times) if lyric_times else 0
        fallback_lyrics = candidate_lyrics
        fallback_correction = {"mode": "original_synced_fallback", "aligned_to_video": False, "reason": reason, "fallback": True}
        if bilibili_duration >= 60 and len(candidate_lyrics) >= 8 and lyric_span >= 45:
            aligned, sampled_correction, sampled_attempts = calibrate_synced_lyrics(
                url, candidate_lyrics, float(candidate_meta.get("duration") or 0), bilibili_duration, infer_lyrics_language_hint(candidate_lyrics)
            )
            recognition_attempts.extend(sampled_attempts)
            fallback_correction = dict(sampled_correction or fallback_correction)
            fallback_correction["fallback"] = True
            if fallback_correction.get("aligned_to_video"):
                fallback_lyrics = aligned
        if candidate_requires_verified_timeline(song_metadata, candidate_meta) and not fallback_correction.get("aligned_to_video"):
            rejected_candidates[-1]["reason"] = "version_timeline_unverified"
            continue
        fallback_meta = {**candidate_meta, "correction": fallback_correction}
        fallbacks.append((synced_fallback_quality(fallback_meta, provider_name), provider_name, fallback_lyrics, candidate_meta, fallback_correction))

    if evaluated:
        _, lyrics_source, lyrics, selected_meta, selected_correction = max(evaluated, key=lambda item: item[0])
        lyrics_meta = standardized_lyrics_meta(lyrics_source, selected_meta, selected_correction, song_metadata, attempted_providers, rejected_candidates)
    elif fallbacks and bool((LYRICS_CONFIG.get("fallback") or {}).get("allow_original_synced", True)):
        _, lyrics_source, lyrics, selected_meta, selected_correction = max(fallbacks, key=lambda item: item[0])
        lyrics_meta = standardized_lyrics_meta(lyrics_source, selected_meta, selected_correction, song_metadata, attempted_providers, rejected_candidates, "lrc_timeline_synced")

    if not lyrics:
        if not speech_attempted:
            speech_anchors, speech_meta, speech_attempts = transcribe_audio_anchors(url)
            speech_attempted = True
            recognition_attempts.extend(speech_attempts)
        if speech_anchors and bool((LYRICS_CONFIG.get("fallback") or {}).get("allow_whisper_text", True)):
            lyrics = whisper_fallback_lines(speech_anchors)
            if lyrics:
                lyrics_source = "whisper"
                whisper_correction = {
                    "mode": "recognized_segment_timeline",
                    "aligned_to_video": True,
                    "matched_count": len(lyrics),
                }
                lyrics_meta = standardized_lyrics_meta(
                    "whisper",
                    {"provider": "whisper", "mode": "text_only"},
                    whisper_correction,
                    song_metadata,
                    attempted_providers + ["whisper"],
                    rejected_candidates,
                    "none",
                )

    if not lyrics:
        video_lyrics, video_meta, video_attempts = attempt_video_recognition(url)
        recognition_attempts.extend(video_attempts)
        if video_lyrics:
            lyrics = video_lyrics
            lyrics_source = "video_recognition"
            video_correction = (video_meta or {}).get("correction") or video_meta or {}
            lyrics_meta = standardized_lyrics_meta(
                "video_recognition", video_meta, video_correction, song_metadata,
                attempted_providers + ["video_recognition"], rejected_candidates,
            )

    if not lyrics:
        if platform_anchors:
            lyrics = platform_anchors
            lyrics_source = "bilibili_subtitle"

    if not lyrics:
        lyrics = build_no_subtitle_lines()
        lyrics_source = "no_public_subtitle"

    video_stream_url = get_safe_video_stream_url(url, lyrics, bilibili_duration)
    video_warning = ""
    cached_path = cached_video_path(url)
    if not video_stream_url and cached_path.exists():
        cached_duration = video_duration_seconds(cached_path)
        lyric_end = lyrics_duration(lyrics)
        if cached_duration and lyric_end and cached_duration + 8 < lyric_end:
            video_warning = f"当前 B站视频缓存只有 {cached_duration:.1f} 秒，但歌词到 {lyric_end:.1f} 秒；请换完整歌曲视频链接。"
    elif not video_stream_url:
        video_warning = "本地视频工具不可用或视频下载失败。"

    correction = (lyrics_meta or {}).get("correction") or {}
    correction_mode = correction.get("mode")
    if lyrics_source in ("bilibili_subtitle", "video_recognition") or correction.get("aligned_to_video") or correction_mode in (
        "sampled_fixed_offset",
        "sampled_linear_timeline",
        "lyrics_timeline_rebuilt",
        "lrc_phrase_timeline",
        "dynamic_lrc_phrase_timeline",
        "local_lrc_phrase_timeline",
        "word_anchor_timeline",
        "recognized_word_timeline",
        "recognized_segment_timeline",
        "asr_word_sequence_timeline",
        "asr_segment_sequence_timeline",
    ):
        video_offset_seconds = 0

    return {
        "ok": True,
        "title": title,
        "source": url,
        "bvid": bvid,
        "cid": cid,
        "videoStreamUrl": video_stream_url,
        "videoWarning": video_warning,
        "videoOffsetSeconds": video_offset_seconds,
        "titleSource": title_source,
        "lyricsSource": lyrics_source,
        "lyricsMeta": lyrics_meta,
        "warning": warning,
        "recognitionAttempts": recognition_attempts,
        "lyrics": lyrics,
    }, 200


def cached_video_path(url):
    key = media_cache_key(url)
    return VIDEO_CACHE_DIR / f"{key}.mp4"


def media_cache_key(url):
    bvid = extract_bvid(url)
    if bvid:
        return f"{bvid}-p{extract_page_number(url)}"
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]


def cached_audio_path(url):
    return AUDIO_CACHE_DIR / f"{media_cache_key(url)}.wav"


def cached_speech_path(url):
    return SPEECH_CACHE_DIR / f"{media_cache_key(url)}.json"


def cached_sampled_speech_path(url):
    return SPEECH_CACHE_DIR / f"{media_cache_key(url)}.sampled.json"


def cache_lock(url):
    key = media_cache_key(url)
    with _CACHE_LOCKS_GUARD:
        return _CACHE_LOCKS.setdefault(key, threading.RLock())


def unique_temp_path(target, label="tmp"):
    target = Path(target)
    return target.with_name(f".{target.stem}.{label}.{uuid.uuid4().hex}{target.suffix}")


def cleanup_caches(force=False):
    global _LAST_CACHE_CLEAN
    now = time.time()
    if not force and now - _LAST_CACHE_CLEAN < CACHE_CLEAN_INTERVAL:
        return
    if not _CACHE_CLEAN_LOCK.acquire(blocking=False):
        return
    try:
        _LAST_CACHE_CLEAN = now
        files = []
        for directory in (VIDEO_CACHE_DIR, AUDIO_CACHE_DIR, SPEECH_CACHE_DIR):
            for path in directory.iterdir():
                if not path.is_file():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if (path.name.startswith(".") and now - stat.st_mtime > 3600) or now - stat.st_mtime > CACHE_TTL_SECONDS:
                    path.unlink(missing_ok=True)
                    continue
                files.append((stat.st_mtime, stat.st_size, path))
        total = sum(item[1] for item in files)
        for _, size, path in sorted(files):
            if total <= CACHE_MAX_BYTES:
                break
            try:
                path.unlink(missing_ok=True)
                total -= size
            except OSError:
                pass
    finally:
        _CACHE_CLEAN_LOCK.release()


def video_has_audio(path):
    ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path or not Path(path).exists():
        return False
    completed = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=20,
    )
    output = (completed.stderr or "") + (completed.stdout or "")
    return "Audio:" in output


def video_has_video(path):
    ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path or not Path(path).exists():
        return False
    completed = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=20,
    )
    output = (completed.stderr or "") + (completed.stdout or "")
    return "Video:" in output


def video_duration_seconds(path):
    ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path or not Path(path).exists():
        return 0
    completed = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=20,
    )
    output = (completed.stderr or "") + (completed.stdout or "")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not match:
        return 0
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def video_is_browser_compatible(path):
    ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path or not Path(path).exists():
        return False
    completed = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=20,
    )
    output = ((completed.stderr or "") + (completed.stdout or "")).lower()
    return ("video: h264" in output or "(avc1" in output) and "audio:" in output


def transcode_to_browser_mp4(path):
    ffmpeg_path = get_ffmpeg_path()
    path = Path(path)
    if not ffmpeg_path or not path.exists():
        return False
    temp_path = path.with_suffix(".browser.mp4")
    completed = subprocess.run(
        [
            ffmpeg_path,
            "-y",
            "-i",
            str(path),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(temp_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=300,
    )
    if completed.returncode == 0 and temp_path.exists() and video_has_audio(temp_path) and video_is_browser_compatible(temp_path):
        temp_path.replace(path)
        return True
    try:
        temp_path.unlink(missing_ok=True)
    except Exception:
        pass
    return False


def merge_sidecar_audio_if_present(target):
    ffmpeg_path = get_ffmpeg_path()
    target = Path(target)
    if not ffmpeg_path or not target.exists() or video_has_audio(target):
        return False
    audio_candidates = sorted(target.parent.glob(f"{target.stem}*.m4a"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not audio_candidates:
        return False
    audio_path = audio_candidates[0]
    temp_path = target.with_suffix(".merged.mp4")
    completed = subprocess.run(
        [
            ffmpeg_path,
            "-y",
            "-i",
            str(target),
            "-i",
            str(audio_path),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(temp_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=300,
    )
    if completed.returncode == 0 and temp_path.exists() and video_has_video(temp_path) and video_has_audio(temp_path) and video_is_browser_compatible(temp_path):
        temp_path.replace(target)
        return True
    try:
        temp_path.unlink(missing_ok=True)
    except Exception:
        pass
    return False


def extract_audio_wav(url):
    target = cached_audio_path(url)
    with cache_lock(url):
        cleanup_caches()
        if target.exists() and target.stat().st_size > 1024 * 32:
            os.utime(target, None)
            return target
        ffmpeg_path = get_ffmpeg_path()
        if not ffmpeg_path:
            raise RuntimeError("missing_ffmpeg")
        video_path = ensure_cached_video(url)
        temp_path = unique_temp_path(target, "audio")
        completed = subprocess.run(
            [
                ffmpeg_path,
                "-y",
                "-i",
                str(video_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-acodec",
                "pcm_s16le",
                str(temp_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=180,
        )
        if completed.returncode != 0 or not temp_path.exists():
            temp_path.unlink(missing_ok=True)
            raise RuntimeError("audio_extract_failed")
        temp_path.replace(target)
        return target


def speech_segment_is_usable(segment):
    text = str(segment.get("text") or "").strip()
    normalized = normalize_alignment_text(text)
    if len(normalized) < 2:
        return False
    no_speech_prob = segment.get("no_speech_prob")
    avg_logprob = segment.get("avg_logprob")
    compression_ratio = segment.get("compression_ratio")
    if no_speech_prob is not None and float(no_speech_prob) > 0.6:
        return False
    if avg_logprob is not None and float(avg_logprob) < -1.0:
        return False
    if compression_ratio is not None and float(compression_ratio) > 2.4:
        return False
    if len(normalized) >= 8 and len(set(normalized)) <= 2:
        return False
    return True


def filter_speech_segments(segments):
    return [segment for segment in (segments or []) if speech_segment_is_usable(segment)]


def normalize_whisper_segments(segments):
    normalized_segments = []
    for segment in segments or []:
        words = []
        for word in getattr(segment, "words", None) or []:
            words.append({
                "word": str(getattr(word, "word", "") or "").strip(),
                "start": float(getattr(word, "start", 0) or 0),
                "end": float(getattr(word, "end", 0) or 0),
            })
        start = float(getattr(segment, "start", 0) or 0)
        normalized_segment = {
            "start": start,
            "end": float(getattr(segment, "end", 0) or 0),
            "seconds": start,
            "time": seconds_to_time(start),
            "text": str(getattr(segment, "text", "") or "").strip(),
            "words": words,
            "no_speech_prob": float(getattr(segment, "no_speech_prob", 0) or 0),
            "avg_logprob": float(getattr(segment, "avg_logprob", 0) or 0),
            "compression_ratio": float(getattr(segment, "compression_ratio", 0) or 0),
        }
        if speech_segment_is_usable(normalized_segment):
            normalized_segments.append(normalized_segment)
    return normalized_segments


def build_anchor_sample_windows(media_duration):
    duration = float(media_duration or 0)
    if duration < 60:
        return []
    window_length = 24.0 if duration >= 120 else 16.0
    windows = []
    for center_ratio in (0.2, 0.5, 0.8):
        center = duration * center_ratio
        start = max(0.0, center - window_length / 2)
        end = min(duration, center + window_length / 2)
        if windows and start <= windows[-1]["end"]:
            windows[-1]["end"] = max(windows[-1]["end"], end)
        else:
            windows.append({"start": start, "end": end})
    for index, window in enumerate(windows):
        window["index"] = index
    return windows


def segment_window_index(segment, windows):
    start = float(segment.get("start", segment.get("seconds", 0)) or 0)
    end = float(segment.get("end", start) or start)
    midpoint = (start + max(start, end)) / 2
    for window in windows or []:
        if float(window["start"]) <= midpoint <= float(window["end"]):
            return int(window["index"])
    return None


def sampled_segments_in_windows(segments, windows):
    sampled = []
    for segment in filter_speech_segments(segments):
        window_index = segment_window_index(segment, windows)
        if window_index is None:
            continue
        item = dict(segment)
        item["window_index"] = window_index
        sampled.append(item)
    return sampled


def normalized_model_identity(model):
    value = str(model or "").strip()
    if not value:
        return ""
    path = Path(value).expanduser()
    if path.exists() or path.is_absolute() or any(separator in value for separator in ("/", "\\")):
        try:
            return os.path.normcase(str(path.resolve()))
        except OSError:
            return os.path.normcase(os.path.abspath(value))
    return value.lower()


def normalized_language_hint(language_hint):
    return str(language_hint or "auto").strip().lower() or "auto"


def lyrics_script_counts(lyrics):
    text = "".join(str(item.get("text") or "") for item in (lyrics or []))
    return len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text)), len(re.findall(r"[A-Za-z\u3400-\u4dbf\u4e00-\u9fff]", text))


def infer_lyrics_language_hint(lyrics):
    han_count, letter_count = lyrics_script_counts(lyrics)
    return "zh" if han_count >= 4 and han_count / max(1, letter_count) >= 0.5 else None


def cached_segments_are_valid(segments, require_words=False, require_quality=False, allow_empty=False):
    if not isinstance(segments, list) or (not allow_empty and not segments):
        return False
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            return False
        try:
            start = float(segment.get("start", segment.get("seconds", 0)))
            end = float(segment.get("end", start))
        except (TypeError, ValueError):
            return False
        if start < 0 or end < start:
            return False
        if require_quality and any(segment.get(key) is None for key in ("no_speech_prob", "avg_logprob", "compression_ratio")):
            return False
        words = segment.get("words")
        if require_words and (not isinstance(words, list) or (normalize_alignment_text(segment.get("text")) and not words)):
            return False
        if words is not None:
            if not isinstance(words, list):
                return False
            for word in words:
                if not isinstance(word, dict) or not isinstance(word.get("word"), str):
                    return False
                try:
                    word_start = float(word.get("start"))
                    word_end = float(word.get("end"))
                except (TypeError, ValueError):
                    return False
                if word_start < 0 or word_end < word_start:
                    return False
    return True


def load_cached_sampled_speech(url, windows, language_hint=None):
    path = cached_sampled_speech_path(url)
    requested_language = normalized_language_hint(language_hint)
    expected = [[round(float(item["start"]), 3), round(float(item["end"]), 3)] for item in windows]
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        version_ok = (
            payload.get("version") == SAMPLED_SPEECH_CACHE_SCHEMA_VERSION
            and payload.get("pipelineVersion") == LYRICS_PIPELINE_VERSION
        )
        windows_ok = payload.get("windows") == expected
        segments_ok = cached_segments_are_valid(payload.get("segments"), require_quality=True)
        meta = payload.get("meta") or {}
        cached_model = normalized_model_identity(meta.get("model"))
        current_model = normalized_model_identity(resolve_whisper_model())
        model_ok = bool(cached_model) and cached_model == current_model
        cached_language = normalized_language_hint(meta.get("languageHint") or meta.get("requestedLanguage"))
        language_ok = cached_language == requested_language
        if version_ok and windows_ok and segments_ok and model_ok and language_ok:
            return payload
    except Exception:
        return None
    return None


def save_cached_sampled_speech(url, windows, segments, meta):
    if not cached_segments_are_valid(segments, require_quality=True):
        return False
    path = cached_sampled_speech_path(url)
    temp_path = unique_temp_path(path, "sampled-speech")
    payload = {
        "version": SAMPLED_SPEECH_CACHE_SCHEMA_VERSION,
        "pipelineVersion": LYRICS_PIPELINE_VERSION,
        "windows": [[round(float(item["start"]), 3), round(float(item["end"]), 3)] for item in windows],
        "segments": segments,
        "meta": meta,
    }
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)
    return True


def load_cached_speech(url, language_hint=None):
    path = cached_speech_path(url)
    requested_language = normalized_language_hint(language_hint)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        segments = payload.get("segments")
        meta = payload.get("meta") or {}
        current_model = normalized_model_identity(resolve_whisper_model())
        cached_model = normalized_model_identity(meta.get("model"))
        version_ok = (
            payload.get("version") == SPEECH_CACHE_SCHEMA_VERSION
            and payload.get("pipelineVersion") == LYRICS_PIPELINE_VERSION
        )
        segments_ok = cached_segments_are_valid(segments, require_words=True, require_quality=True)
        model_ok = bool(cached_model) and cached_model == current_model
        cached_language = normalized_language_hint(meta.get("languageHint") or meta.get("requestedLanguage"))
        language_ok = cached_language == requested_language
        if version_ok and segments_ok and model_ok and language_ok:
            return payload
    except Exception:
        return None
    return None


def save_cached_speech(url, payload):
    if not cached_segments_are_valid(payload.get("segments"), require_words=True, require_quality=True):
        return False
    path = cached_speech_path(url)
    temp_path = unique_temp_path(path, "speech")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)
    return True


def get_whisper_model(model_name):
    with _WHISPER_MODEL_LOCK:
        model = _WHISPER_MODELS.get(str(model_name))
        if model is None:
            from faster_whisper import WhisperModel
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
            _WHISPER_MODELS[str(model_name)] = model
        return model


def resolve_whisper_model():
    configured = os.environ.get("SING_REACTOR_WHISPER_MODEL")
    if configured:
        configured_path = Path(configured)
        if configured_path.exists():
            return str(configured_path)
        local_configured = LOCAL_MODELS_DIR / configured
        if local_configured.exists():
            return str(local_configured)
        return configured
    for name in ("faster-whisper-small", "faster-whisper-tiny"):
        model_dir = LOCAL_MODELS_DIR / name
        if (model_dir / "model.bin").exists() and (model_dir / "config.json").exists():
            return str(model_dir)
    return "tiny"


def _transcribe_audio_anchors(url, language_hint=None):
    requested_language = normalized_language_hint(language_hint)
    cached = load_cached_speech(url, language_hint)
    if cached:
        segments = filter_speech_segments(cached.get("segments") or [])
        return segments, cached.get("meta") or {}, [{
            "stage": "speech_anchors",
            "status": "cached",
            "message": f"已使用本地 Whisper 缓存：{len(segments)} 个可用音频时间锚点。"
        }]
    attempts = []
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        attempts.append({
            "stage": "speech_anchors",
            "status": "missing_tool",
            "message": "缺少 faster-whisper，暂时不能用音频锚点校准歌词。"
        })
        return [], None, attempts
    try:
        audio_path = extract_audio_wav(url)
        model_name = resolve_whisper_model()
        model = get_whisper_model(model_name)
        with _WHISPER_INFERENCE:
            segments, info = model.transcribe(
                str(audio_path),
                language=None if requested_language == "auto" else requested_language,
                word_timestamps=True,
                beam_size=5,
                best_of=5,
                condition_on_previous_text=True,
            )
            normalized_segments = normalize_whisper_segments(list(segments))
        meta = {
            "provider": "faster_whisper",
            "model": str(model_name),
            "language": getattr(info, "language", None),
            "languageHint": requested_language,
            "requestedLanguage": requested_language,
            "duration": getattr(info, "duration", None),
        }
        if normalized_segments:
            save_cached_speech(url, {
                "version": SPEECH_CACHE_SCHEMA_VERSION,
                "pipelineVersion": LYRICS_PIPELINE_VERSION,
                "segments": normalized_segments,
                "meta": meta,
            })
        return normalized_segments, meta, [{
            "stage": "speech_anchors",
            "status": "ok" if normalized_segments else "no_result",
            "message": f"已生成 {len(normalized_segments)} 个音频时间锚点。" if normalized_segments else "音频识别未生成可用时间锚点。"
        }]
    except Exception:
        attempts.append({
            "stage": "speech_anchors",
            "status": "failed",
            "message": "音频锚点识别失败，已继续尝试其他歌词来源。"
        })
        return [], None, attempts


def transcribe_audio_anchors(url, language_hint=None):
    with cache_lock(url):
        return _transcribe_audio_anchors(url, language_hint)


def _transcribe_sampled_audio_anchors(url, media_duration, language_hint=None):
    requested_language = normalized_language_hint(language_hint)
    windows = build_anchor_sample_windows(media_duration)
    if not windows:
        return [], None, [{"stage": "sampled_speech_anchors", "status": "no_result", "message": "媒体过短，未运行采样音频校准。"}]
    full_cached = load_cached_speech(url, language_hint)
    if full_cached:
        reused = sampled_segments_in_windows(full_cached.get("segments") or [], windows)
        if reused:
            return reused, {**(full_cached.get("meta") or {}), "cache": "full"}, [{
                "stage": "sampled_speech_anchors", "status": "cached_full", "message": f"已从完整 Whisper 缓存复用 {len(reused)} 个采样锚点。"
            }]
    sampled_cached = load_cached_sampled_speech(url, windows, language_hint)
    if sampled_cached:
        segments = filter_speech_segments(sampled_cached.get("segments") or [])
        return segments, sampled_cached.get("meta") or {}, [{
            "stage": "sampled_speech_anchors", "status": "cached", "message": f"已使用本地采样 Whisper 缓存：{len(segments)} 个锚点。"
        }]
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return [], None, [{"stage": "sampled_speech_anchors", "status": "missing_tool", "message": "缺少 faster-whisper，保留原同步歌词。"}]
    try:
        audio_path = extract_audio_wav(url)
        model_name = resolve_whisper_model()
        model = get_whisper_model(model_name)
        clip_timestamps = []
        for window in windows:
            clip_timestamps.extend([float(window["start"]), float(window["end"])])
        with _WHISPER_INFERENCE:
            segments, info = model.transcribe(
                str(audio_path),
                language=None if requested_language == "auto" else requested_language,
                word_timestamps=True,
                beam_size=5,
                best_of=5,
                condition_on_previous_text=False,
                clip_timestamps=clip_timestamps,
            )
            normalized = normalize_whisper_segments(list(segments))
        normalized = sampled_segments_in_windows(normalized, windows)
        meta = {
            "provider": "faster_whisper_sampled",
            "model": str(model_name),
            "language": getattr(info, "language", None),
            "languageHint": requested_language,
            "requestedLanguage": requested_language,
            "duration": getattr(info, "duration", None),
        }
        if normalized:
            save_cached_sampled_speech(url, windows, normalized, meta)
        return normalized, meta, [{"stage": "sampled_speech_anchors", "status": "ok" if normalized else "no_result", "message": f"已生成 {len(normalized)} 个采样音频锚点。" if normalized else "采样识别未生成可用时间锚点。"}]
    except Exception as exc:
        return [], None, [{
            "stage": "sampled_speech_anchors", "status": "failed", "message": f"采样音频锚点识别失败：{exc.__class__.__name__} {str(exc)[:120]}".strip()
        }]


def transcribe_sampled_audio_anchors(url, media_duration, language_hint=None):
    with cache_lock(url):
        cleanup_caches()
        return _transcribe_sampled_audio_anchors(url, media_duration, language_hint)


def calibrate_synced_lyrics(url, lyrics, candidate_duration, media_duration, language_hint=None):
    anchors, speech_meta, attempts = transcribe_sampled_audio_anchors(url, media_duration, language_hint)
    if not anchors:
        return lyrics, {"mode": "unverified", "aligned_to_video": False, "reason": "missing_sampled_anchors", "attempts": attempts}, attempts
    matches = match_sampled_anchors_to_lyrics(lyrics, anchors, candidate_duration, media_duration)
    fit = robust_timeline_models(matches)
    fit["anchorProvider"] = (speech_meta or {}).get("provider")
    fit["attempts"] = attempts
    if fit.get("mode") not in ("sampled_fixed_offset", "sampled_linear_timeline"):
        fallback_config = LYRICS_CONFIG.get("fallback") or {}
        matches = list(fit.get("matches") or [])
        fixed_model = fit.get("fixed_model") or {}
        strong_matches = [
            item for item in matches
            if float(item.get("similarity") or 0) >= float(fallback_config.get("weak_timeline_min_similarity", 0.86))
        ]
        weak_offset = fixed_model.get("offset")
        if (
            bool(fallback_config.get("allow_weak_timeline_rebuild", True))
            and strong_matches
            and weak_offset is not None
            and abs(float(weak_offset)) <= float(fallback_config.get("weak_timeline_max_offset", 45))
        ):
            fit = {
                **fit,
                "mode": "sampled_fixed_offset",
                "scale": 1.0,
                "offset": float(weak_offset),
                "matched_count": len(strong_matches),
                "window_count": len({int(item.get("window_index", 0)) for item in strong_matches}),
                "avg_similarity": sum(float(item.get("similarity") or 0) for item in strong_matches) / len(strong_matches),
                "confidence_basis": "weak_audio_anchor",
                "weak_evidence": True,
            }
            fit["aligned_to_video"] = True
            transformed = apply_timeline_transform(lyrics, 1.0, float(weak_offset))
            return transformed, fit, attempts
        fit["aligned_to_video"] = False
        return lyrics, fit, attempts
    fit["aligned_to_video"] = True
    transformed = apply_timeline_transform(lyrics, fit["scale"], fit["offset"])
    return transformed, fit, attempts


def _ensure_cached_video(url):
    target = cached_video_path(url)
    if target.exists() and target.stat().st_size > 1024 * 64 and not video_has_audio(target):
        merge_sidecar_audio_if_present(target)
    if target.exists() and target.stat().st_size > 1024 * 64 and video_has_audio(target) and video_is_browser_compatible(target):
        return target
    if target.exists() and target.stat().st_size > 1024 * 64 and video_has_audio(target):
        if transcode_to_browser_mp4(target):
            return target
    yt_dlp_command = get_yt_dlp_command()
    ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path:
        raise RuntimeError("missing_video_tools")
    download_base = unique_temp_path(target, "download")
    if yt_dlp_command:
        output = str(download_base.with_suffix(".%(ext)s"))
        command = yt_dlp_command + [
            "-f",
            "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/bestvideo[vcodec^=avc1]+bestaudio/best[ext=mp4]/best",
            "--merge-output-format",
            "mp4",
            "--ffmpeg-location",
            str(ffmpeg_path),
            "-o",
            output,
            url,
        ]
        subprocess.run(
            command,
            cwd=VIDEO_CACHE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=300,
            env=subprocess_env(),
        )
        candidates = [target] + sorted(VIDEO_CACHE_DIR.glob(f"{download_base.stem}*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True)
        for candidate in candidates:
            if candidate.exists() and candidate.stat().st_size > 1024 * 64 and video_has_audio(candidate) and video_is_browser_compatible(candidate):
                if candidate != target:
                    candidate.replace(target)
                return target
        for candidate in candidates:
            if candidate.exists() and candidate.stat().st_size > 1024 * 64 and video_has_audio(candidate):
                if candidate != target:
                    candidate.replace(target)
                if transcode_to_browser_mp4(target):
                    return target

    # Bilibili may return HTTP 412 to yt-dlp's webpage extractor from cloud IPs.
    # The official playurl API avoids the webpage gate while keeping media hosts validated.
    streams = fetch_bilibili_media_streams(url)
    if streams:
        output_path = download_base.with_suffix(".api.mp4")
        headers = "".join(f"{key}: {value}\r\n" for key, value in streams["headers"].items())
        completed = subprocess.run(
            [
                ffmpeg_path, "-y", "-headers", headers, "-i", streams["video_url"],
                "-headers", headers, "-i", streams["audio_url"],
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                "-shortest", "-movflags", "+faststart", str(output_path),
            ],
            cwd=VIDEO_CACHE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=300,
            env=subprocess_env(),
        )
        if completed.returncode == 0 and output_path.exists() and output_path.stat().st_size > 1024 * 64:
            output_path.replace(target)
            if video_has_audio(target) and video_is_browser_compatible(target):
                return target
    raise RuntimeError("video_download_failed")


def ensure_cached_video(url):
    url = normalize_bilibili_url(url, resolve_redirects=True)
    with cache_lock(url):
        cleanup_caches()
        path = _ensure_cached_video(url)
        os.utime(path, None)
        return path


def get_safe_video_stream_url(url, lyrics, source_duration=0):
    if not is_bilibili_url(url):
        return None
    if cached_video_path(url).exists() or (get_yt_dlp_command() and get_ffmpeg_path()):
        return f"/api/video?url={quote(url, safe='')}"
    return None


def video_covers_lyrics(path, lyrics):
    duration = video_duration_seconds(path)
    lyric_end = lyrics_duration(lyrics)
    if duration <= 0 or lyric_end <= 0:
        return True
    return duration + 8 >= lyric_end


class Handler(BaseHTTPRequestHandler):
    def _cors_origin(self):
        origin = self.headers.get("Origin")
        return origin if origin in ALLOWED_ORIGINS else None

    def _send_cors_headers(self):
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_video_file(self, path):
        file_size = path.stat().st_size
        try:
            byte_range = parse_single_range(self.headers.get("Range"), file_size)
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{file_size}")
            self.send_header("Content-Length", "0")
            self._send_cors_headers()
            self.end_headers()
            return
        start, end = byte_range or (0, file_size - 1)
        status = 206 if byte_range else 200
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self._send_cors_headers()
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()
        with path.open("rb") as file:
            file.seek(start)
            remaining = length
            while remaining > 0:
                chunk = file.read(min(1024 * 512, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_OPTIONS(self):
        if self.headers.get("Origin") and not self._cors_origin():
            self._send_json({"ok": False, "error": "origin_not_allowed"}, 403)
            return
        self._send_json({"ok": True})

    def do_POST(self):
        if self.headers.get("Origin") and not self._cors_origin():
            self._send_json({"ok": False, "error": "origin_not_allowed"}, 403)
            return
        parsed = urlparse(self.path)
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._send_json({"ok": False, "error": "length_required"}, 411)
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._send_json({"ok": False, "error": "invalid_content_length"}, 400)
            return
        if length < 0:
            self._send_json({"ok": False, "error": "invalid_content_length"}, 400)
            return
        if length > MAX_JSON_BODY_BYTES:
            self._send_json({"ok": False, "error": "request_too_large"}, 413)
            return
        body = self.rfile.read(length)
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"ok": False, "error": "invalid_json"}, 400)
            return
        if not isinstance(data, dict):
            self._send_json({"ok": False, "error": "invalid_json"}, 400)
            return
        if parsed.path == "/api/align-lyrics":
            lyrics_text = data.get("lyrics") or ""
            if not isinstance(lyrics_text, str):
                self._send_json({"ok": False, "error": "invalid_lyrics"}, 400)
                return
            if len(lyrics_text.encode("utf-8")) > MAX_LYRICS_BYTES:
                self._send_json({"ok": False, "error": "lyrics_too_large"}, 413)
                return
            payload, status = align_manual_lyrics(data.get("url") or "", lyrics_text)
            self._send_json(payload, status)
            return
        self._send_json({"ok": False, "error": "not_found"}, 404)

    def do_GET(self):
        if self.headers.get("Origin") and not self._cors_origin():
            self._send_json({"ok": False, "error": "origin_not_allowed"}, 403)
            return
        parsed = urlparse(self.path)
        if parsed.path in ("/health", "/api/health"):
            self._send_json({"ok": True, "product": "Sing Reactor", "service": "api"})
            return
        if parsed.path == "/api/identify":
            url = parse_qs(parsed.query).get("url", [""])[0]
            payload, status = identify(url)
            self._send_json(payload, status)
            return
        if parsed.path == "/api/video":
            url = parse_qs(parsed.query).get("url", [""])[0]
            try:
                path = ensure_cached_video(url)
                self._send_video_file(path)
            except UnsafeUrlError:
                self._send_json({"ok": False, "error": "unsupported_url"}, 400)
            except Exception as exc:
                sys.stderr.write(f"[server] video unavailable: {exc.__class__.__name__}: {exc}\n")
                self._send_json({"ok": False, "error": "video_unavailable"}, 502)
            return
        self._send_json({"ok": False, "error": "not_found"}, 404)

    def log_message(self, fmt, *args):
        sys.stderr.write("[server] " + fmt % args + "\n")


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Sing Reactor API listening on http://{HOST}:{PORT}")
    server.serve_forever()
