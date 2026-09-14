#!/usr/bin/env python3
"""
Niche Radar
-----------
Takip ettigin YouTube kanallarinin (video + shorts) yeni iceriklerini her gun bulur,
transkriptini ceker, Claude ile ozetler ve tek bir Markdown rapor uretir.

Sadece Python standart kutuphanesi + yt-dlp + claude CLI kullanir.
Python 3.9+ ile calisir (macOS'un kendi python3'u yeterlidir).

Komutlar:
  install                 ~/NicheRadar klasorunu, config ve prompt dosyalarini olusturur
  add-channel <@handle>   Kanal ekler (handle, kanal URL'i, video linki veya UC... id)
  check-channel <...>     Kanali cozumler ama eklemez (oneri dogrulama)
  remove-channel <...>    Kanali cikarir (ad, handle veya id)
  list                    Kanallari listeler
  doctor                  Bagimliliklari ve agi test eder
  run                     Yeni videolari bulur, ozetler, rapor yazar
      --dry-run           Sadece kesif yapar, hicbir sey indirmez/ozetlemez
      --no-llm            Transkript ceker ama Claude'u cagirmaz
      --limit N           Bu calismada en fazla N video isler
      --only "<kanal>"    Sadece adi eslesen kanal(lar)
  schedule install|remove|status   Gunluk zamanlayici (macOS launchd / Windows Task Scheduler)
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from pathlib import Path
from urllib import parse, request

HOME = Path(os.environ.get("NICHE_RADAR_HOME", str(Path.home() / "NicheRadar")))
CONFIG = HOME / "config.json"
STATE = HOME / "state.json"
REPORTS = HOME / "reports"
LOGS = HOME / "logs"
CACHE = HOME / "cache"
SCRIPT_DIR = Path(__file__).resolve().parent
IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
LABEL = "com.nicheradar.daily"

DEFAULT_CONFIG = {
    "channels": [],
    "tabs": ["videos", "shorts"],
    "discover_items": 10,
    "first_run_items": 10,
    "first_run_days": 7,
    "max_per_run": 20,
    "max_age_days": 14,
    "sub_langs": ["en", "tr"],
    "summary_lang": "Türkçe",
    "model": "sonnet",
    "digest": True,
    "max_transcript_chars": 60000,
    "sleep_seconds": 2,
    "report_dir": "",
    "schedule_time": "08:00",
    "notify_macos": True,
    "telegram": {"bot_token": "", "chat_id": ""},
    "ytdlp_extra_args": [],
    "whisper": {
        "enabled": False,
        "command": "whisper {audio} --model base --output_format txt --output_dir {outdir}"
    }
}


# ----------------------------------------------------------------- yardimcilar
def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    line = "[%s] %s" % (now(), msg)
    print(line, flush=True)
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / "radar.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def extra_path() -> str:
    cands = [
        Path.home() / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path.home() / ".cargo" / "bin",
        Path.home() / "AppData" / "Roaming" / "npm",
        Path.home() / "AppData" / "Roaming" / "Python" / "Scripts",
        Path.home() / "AppData" / "Local" / "Programs" / "Python",
    ]
    return os.pathsep.join(str(p) for p in cands if p.exists())


def env() -> dict:
    e = dict(os.environ)
    e["PATH"] = extra_path() + os.pathsep + e.get("PATH", "")
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def which(cmd: str) -> str | None:
    return shutil.which(cmd, path=env()["PATH"])


def run(cmd: list, timeout: int = 120, stdin: str | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env(), input=stdin, cwd=str(cwd) if cwd else None,
    )


def load_json(path: Path, default):
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        bak = path.with_name("%s.corrupt-%s" % (path.name, dt.datetime.now().strftime("%Y%m%d%H%M%S")))
        try:
            shutil.copy2(path, bak)
        except OSError:
            pass
        raise SystemExit("%s bozuk (satir %d: %s). Yedek: %s. Dosyayi duzelt ya da sil, sonra tekrar dene."
                         % (path.name, e.lineno, e.msg, bak.name))


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    user = load_json(CONFIG, {})
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def report_dir(cfg: dict) -> Path:
    d = cfg.get("report_dir") or ""
    return Path(os.path.expanduser(d)) if d else REPORTS


def human_duration(sec) -> str:
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return "?"
    m, s = divmod(sec, 60)
    return "%d:%02d dk" % (m, s) if m else "%d sn" % s


def human_views(v) -> str:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return "?"
    if v >= 1_000_000:
        return "%.1fM" % (v / 1_000_000)
    if v >= 1_000:
        return "%.1fK" % (v / 1_000)
    return str(v)


# ----------------------------------------------------------------- kanal cozumleme
def ytdlp_bin() -> str:
    b = which("yt-dlp")
    if not b:
        raise SystemExit("yt-dlp bulunamadi. Kurulum: uv tool install \"yt-dlp[default,curl-cffi]\"")
    return b


def resolve_channel(raw: str) -> dict:
    """@handle, kanal URL'i veya UC... id -> {"name", "id", "handle"}"""
    raw = raw.strip()
    mv = re.search(r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|live/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})", raw)
    if mv:  # video/shorts linki verildi: kanalini bul
        r = run([ytdlp_bin(), "--no-warnings", "--print", "%(channel_id)s",
                 "https://www.youtube.com/watch?v=%s" % mv.group(1)], timeout=90)
        cid = (r.stdout.strip().splitlines() or [""])[0]
        if not cid.startswith("UC"):
            raise SystemExit("Video linkinden kanal cozulemedi: %s\n%s" % (raw, r.stderr.strip()[-300:]))
        raw = cid
    m = re.search(r"(UC[A-Za-z0-9_-]{22})", raw)
    if m and (raw.startswith("UC") or "/channel/" in raw):
        url = "https://www.youtube.com/channel/%s/videos" % m.group(1)
    else:
        handle = raw
        mh = re.search(r"youtube\.com/@([^/?#]+)", raw)
        if mh:
            handle = mh.group(1)
        handle = handle.lstrip("@")
        url = "https://www.youtube.com/@%s/videos" % handle
    r = run([ytdlp_bin(), "--flat-playlist", "--playlist-items", "1", "--no-warnings",
             "--print", "%(playlist_channel_id)s\t%(playlist_channel)s\t%(playlist_uploader_id)s", url], timeout=90)
    line = (r.stdout.strip().splitlines() or [""])[0]
    parts = line.split("\t")
    if r.returncode != 0 or len(parts) < 2 or not parts[0].startswith("UC"):
        raise SystemExit("Kanal cozulemedi: %s\n%s" % (raw, r.stderr.strip()[-400:]))
    handle = parts[2] if len(parts) > 2 and parts[2] not in ("NA", "") else ""
    return {"name": parts[1] if parts[1] != "NA" else raw, "id": parts[0], "handle": handle}


# ----------------------------------------------------------------- kesif
def discover_tab(channel: dict, tab: str, n: int) -> list:
    url = "https://www.youtube.com/channel/%s/%s" % (channel["id"], tab)
    r = run([ytdlp_bin(), "--flat-playlist", "--playlist-items", "1-%d" % n, "--no-warnings",
             "--print", "%(id)s\t%(title)s", url], timeout=120)
    items = []
    for line in r.stdout.splitlines():
        if "\t" not in line:
            continue
        vid, title = line.split("\t", 1)
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            items.append({"id": vid, "title": title.strip(), "tab": tab})
    if r.returncode != 0 and not items:
        err = r.stderr.strip()
        # Shorts sekmesi olmayan kanal normaldir; diger hatalari logla
        if "does not have a shorts tab" in err.lower() or "this channel does not have" in err.lower():
            return []
        log("  ! %s/%s kesif hatasi: %s" % (channel["name"], tab, err[-200:]))
        if tab == "videos":
            items = discover_rss(channel)
    return items


def discover_rss(channel: dict) -> list:
    """Yedek kesif: resmi RSS feed'i (son 15 yukleme)."""
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=%s" % channel["id"]
    try:
        with request.urlopen(request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as resp:
            root = ET.fromstring(resp.read())
    except Exception as e:  # noqa: BLE001
        log("  ! RSS hatasi %s: %s" % (channel["name"], e))
        return []
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    out = []
    for e in root.findall("a:entry", ns):
        vid = e.find("yt:videoId", ns)
        title = e.find("a:title", ns)
        if vid is not None and vid.text:
            out.append({"id": vid.text, "title": (title.text or "") if title is not None else "", "tab": "rss"})
    return out


# ----------------------------------------------------------------- transkript
def parse_vtt(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines: list = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "-->" in line or re.fullmatch(r"\d+", line):
            continue
        if line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE")):
            continue
        line = html.unescape(re.sub(r"<[^>]+>", "", line)).strip()
        if not line or line == "[Music]" or line == "[Müzik]":
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return " ".join(lines)


def pick_vtt(folder: Path, langs: list) -> Path | None:
    files = sorted(folder.glob("*.vtt"))
    if not files:
        return None
    for lang in langs:
        for f in files:
            # id.en.vtt tercih, id.en-orig.vtt ikinci
            parts = f.name.split(".")
            if len(parts) >= 3 and parts[-2] == lang:
                return f
        for f in files:
            if (".%s" % lang) in f.name:
                return f
    return files[0]


def fetch_transcript(cfg: dict, vid: str) -> tuple:
    """-> (meta: dict, transcript: str, status: str)"""
    folder = CACHE / "subs" / vid
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    meta_file = folder / "meta.txt"
    langs = cfg["sub_langs"]
    sub_langs = ",".join("%s.*" % l for l in langs)
    url = "https://www.youtube.com/watch?v=%s" % vid
    cmd = [ytdlp_bin(), "--skip-download", "--write-auto-subs", "--write-subs",
           "--sub-langs", sub_langs, "--sub-format", "vtt", "--no-warnings", "-q",
           "--print-to-file", "%(upload_date)s\t%(duration)s\t%(view_count)s\t%(channel)s\t%(title)s", str(meta_file),
           "-o", str(folder / "%(id)s.%(ext)s")] + list(cfg.get("ytdlp_extra_args", [])) + [url]
    meta = {"upload_date": "", "duration": "", "view_count": "", "channel": "", "title": ""}
    last_err = ""
    for attempt in (1, 2):
        try:
            r = run(cmd, timeout=180)
        except subprocess.TimeoutExpired:
            last_err = "timeout"
            continue
        if meta_file.exists():
            line = meta_file.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
            if line:
                p = line[0].split("\t", 4)
                if len(p) == 5:
                    meta = dict(zip(["upload_date", "duration", "view_count", "channel", "title"], p))
            break
        last_err = r.stderr.strip()[-300:]
        time.sleep(3 * attempt)
    if not meta["title"]:
        return meta, "", "hata: " + (last_err or "meta alinamadi")
    vtt = pick_vtt(folder, langs)
    if vtt:
        text = parse_vtt(vtt)
        if len(text) > 200:
            return meta, text, "altyazi"
    # yedek: Whisper (opsiyonel, ffmpeg gerektirir)
    if cfg["whisper"].get("enabled"):
        text = whisper_transcript(cfg, vid, folder)
        if text:
            return meta, text, "whisper"
        return meta, "", "transkript yok (whisper basarisiz)"
    return meta, "", "transkript yok"


def whisper_transcript(cfg: dict, vid: str, folder: Path) -> str:
    if not which("ffmpeg"):
        log("  ! whisper icin ffmpeg gerekli, atlandi")
        return ""
    url = "https://www.youtube.com/watch?v=%s" % vid
    r = run([ytdlp_bin(), "-f", "bestaudio", "-x", "--audio-format", "m4a", "--no-warnings", "-q",
             "-o", str(folder / "audio.%(ext)s"), url], timeout=600)
    audio = folder / "audio.m4a"
    if r.returncode != 0 or not audio.exists():
        return ""
    try:
        argv = [a.format(audio=str(audio), outdir=str(folder)) for a in shlex.split(cfg["whisper"]["command"])]
    except (ValueError, KeyError, IndexError) as e:
        log("  ! whisper.command hatali: %s" % e)
        return ""
    if not which(argv[0]):
        log("  ! whisper komutu bulunamadi: %s" % argv[0])
        return ""
    try:
        subprocess.run(argv, env=env(), timeout=1800, capture_output=True)
    except subprocess.TimeoutExpired:
        return ""
    txts = list(folder.glob("*.txt"))
    txts = [t for t in txts if t.name != "meta.txt"]
    return txts[0].read_text(encoding="utf-8", errors="ignore").strip() if txts else ""


# ----------------------------------------------------------------- claude
def claude_bin() -> str:
    b = which("claude")
    if not b:
        raise SystemExit("claude CLI bulunamadi. Kurulum: https://claude.com/claude-code")
    return b


def read_prompt(name: str) -> str:
    for p in (HOME / name, SCRIPT_DIR / name):
        if p.exists():
            return p.read_text(encoding="utf-8")
    raise SystemExit("Prompt dosyasi yok: %s" % name)


def ask_claude(cfg: dict, prompt: str, timeout: int = 420) -> str:
    cmd = [claude_bin(), "-p", "--output-format", "text", "--model", cfg["model"]]
    try:
        r = run(cmd, timeout=timeout, stdin=prompt, cwd=HOME)
    except subprocess.TimeoutExpired:
        return "_(Claude yanit vermedi: zaman asimi)_"
    if r.returncode != 0:
        return "_(Claude hatasi: %s)_" % r.stderr.strip()[-300:]
    return r.stdout.strip()


def summarize(cfg: dict, item: dict, meta: dict, transcript: str) -> str:
    tpl = read_prompt("prompt.md")
    t = transcript[: cfg["max_transcript_chars"]]
    if len(transcript) > cfg["max_transcript_chars"]:
        t += "\n\n[... transkript kirpildi ...]"
    prompt = (tpl.replace("{summary_lang}", cfg["summary_lang"])
                 .replace("{channel}", meta.get("channel") or item["channel"])
                 .replace("{title}", meta.get("title") or item["title"])
                 .replace("{url}", "https://www.youtube.com/watch?v=%s" % item["id"])
                 .replace("{kind}", "Shorts" if item["tab"] == "shorts" else "Video")
                 .replace("{duration}", human_duration(meta.get("duration")))
                 .replace("{transcript}", t))
    return ask_claude(cfg, prompt)


def make_digest(cfg: dict, blocks: list) -> str:
    tpl = read_prompt("digest_prompt.md")
    joined = "\n\n---\n\n".join(blocks)
    return ask_claude(cfg, tpl.replace("{summary_lang}", cfg["summary_lang"]).replace("{summaries}", joined))


# ----------------------------------------------------------------- bildirim
def notify(cfg: dict, title: str, body: str) -> None:
    if IS_MAC and cfg.get("notify_macos"):
        safe = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
        run(["osascript", "-e", 'display notification "%s" with title "%s"' % (safe(body[:200]), safe(title))], timeout=15)
    tg = cfg.get("telegram") or {}
    if tg.get("bot_token") and tg.get("chat_id"):
        try:
            data = parse.urlencode({"chat_id": tg["chat_id"], "text": (title + "\n\n" + body)[:4000]}).encode()
            request.urlopen("https://api.telegram.org/bot%s/sendMessage" % tg["bot_token"], data=data, timeout=30)
        except Exception as e:  # noqa: BLE001
            log("  ! Telegram bildirimi basarisiz: %s" % e)


# ----------------------------------------------------------------- komutlar
def cmd_install(args) -> None:
    for d in (HOME, REPORTS, LOGS, CACHE):
        d.mkdir(parents=True, exist_ok=True)
    if not CONFIG.exists():
        save_json(CONFIG, DEFAULT_CONFIG)
        log("config olusturuldu: %s" % CONFIG)
    for name in ("prompt.md", "digest_prompt.md"):
        src = SCRIPT_DIR / name
        dst = HOME / name
        if src.exists() and not dst.exists():
            shutil.copy(src, dst)
    src_script = Path(__file__).resolve()
    dst_script = HOME / "radar.py"
    if src_script != dst_script:
        shutil.copy(src_script, dst_script)
    log("kurulum tamam: %s" % HOME)


def cmd_add(args) -> None:
    cfg_raw = load_json(CONFIG, DEFAULT_CONFIG)
    failed = []
    for raw in args.channel:
        try:
            ch = resolve_channel(raw)
        except SystemExit as e:
            log("! eklenemedi: %s -> %s" % (raw, str(e).splitlines()[0]))
            failed.append(raw)
            continue
        if any(c["id"] == ch["id"] for c in cfg_raw.get("channels", [])):
            log("zaten var: %s" % ch["name"])
            continue
        cfg_raw.setdefault("channels", []).append(ch)
        log("eklendi: %s (%s)" % (ch["name"], ch["id"]))
    save_json(CONFIG, cfg_raw)
    if failed:
        raise SystemExit("%d kanal eklenemedi: %s (digerleri kaydedildi)" % (len(failed), ", ".join(failed)))


def cmd_check(args) -> None:
    """Kanali cozumle ama config'e YAZMA (oneri adayi dogrulama)."""
    bad = 0
    for raw in args.channel:
        try:
            ch = resolve_channel(raw)
            print("OK   %s  %s  %s" % (ch["name"], ch.get("handle", ""), ch["id"]))
        except SystemExit as e:
            bad += 1
            print("YOK  %s  (%s)" % (raw, str(e).splitlines()[0]))
    if bad:
        raise SystemExit(1)


def cmd_remove(args) -> None:
    cfg_raw = load_json(CONFIG, DEFAULT_CONFIG)
    chans = cfg_raw.get("channels", [])
    for raw in args.channel:
        key = raw.strip().lstrip("@").lower()
        hit = [c for c in chans if key in (c["id"].lower(), c.get("handle", "").lstrip("@").lower(), c["name"].lower())
               or key in c["name"].lower()]
        if not hit:
            log("bulunamadi: %s" % raw)
            continue
        for c in hit:
            chans.remove(c)
            log("cikarildi: %s (%s)" % (c["name"], c["id"]))
    cfg_raw["channels"] = chans
    save_json(CONFIG, cfg_raw)


def cmd_list(args) -> None:
    cfg = load_config()
    if not cfg["channels"]:
        print("Kanal yok. Ekle: radar.py add-channel @handle")
    for c in cfg["channels"]:
        print("- %s  %s  %s" % (c["name"], c.get("handle", ""), c["id"]))


def cmd_doctor(args) -> None:
    ok = True
    print("Niche Radar doctor")
    print("  python   :", sys.version.split()[0], sys.executable)
    print("  os       :", platform.platform())
    print("  home     :", HOME, "(var)" if HOME.exists() else "(YOK -> install calistir)")
    for tool, hint in (("yt-dlp", 'uv tool install "yt-dlp[default,curl-cffi]"'),
                       ("claude", "https://claude.com/claude-code"),
                       ("ffmpeg", "opsiyonel, sadece whisper icin")):
        b = which(tool)
        if b:
            ver = ""
            if tool != "ffmpeg":
                try:
                    ver = run([b, "--version"], timeout=30).stdout.strip().splitlines()[0]
                except Exception:  # noqa: BLE001
                    ver = "?"
            print("  %-8s : OK  %s %s" % (tool, ver, b))
        else:
            print("  %-8s : YOK  (%s)" % (tool, hint))
            if tool != "ffmpeg":
                ok = False
    cfg = load_config()
    if not re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(cfg.get("schedule_time", "")).strip()):
        ok = False
        print("  saat     : GECERSIZ %r (beklenen HH:MM, ornek 08:00)" % cfg.get("schedule_time"))
    else:
        print("  saat     :", cfg["schedule_time"])
    print("  kanallar :", len(cfg["channels"]))
    state_d = load_json(STATE, {})
    if state_d.get("backlog"):
        print("  bekleyen :", len(state_d["backlog"]), "icerik (sonraki calismada islenir)")
    if cfg["channels"] and which("yt-dlp"):
        ch = cfg["channels"][0]
        t0 = time.time()
        items = discover_tab(ch, "videos", 3)
        print("  kesif    : %s -> %d video (%.1fs)" % (ch["name"], len(items), time.time() - t0))
        if not items:
            ok = False
            print("             ! kesif bos dondu. Ag/VPN/bot kontrolu olabilir; logs/radar.log'a bak")
    state = load_json(STATE, {})
    print("  son calisma:", state.get("last_run", "hic"))
    reps = sorted(report_dir(cfg).glob("*.md")) if report_dir(cfg).exists() else []
    print("  son rapor:", reps[-1].name if reps else "yok")
    print("  zamanlayici:", schedule_status_text())
    print("SONUC:", "hazir" if ok else "eksik var")


def _slim(i: dict) -> dict:
    return {k: i[k] for k in ("id", "title", "tab", "channel", "age_days") if k in i}


def cmd_run(args) -> None:
    cfg = load_config()
    if not cfg["channels"]:
        raise SystemExit("Kanal yok. Once: radar.py add-channel @handle")
    state = load_json(STATE, {"seen": {}, "initialized": False, "backlog": []})
    seen: dict = state.setdefault("seen", {})
    backlog: list = [i for i in (state.get("backlog") or []) if i.get("id") not in seen]
    first_run = not state.get("initialized")
    only = (args.only or "").lower()
    if first_run and only:
        raise SystemExit("Ilk calismada --only kullanilamaz (diger kanallarin baslangic noktasi konmaz). Once tam bir 'run' yap.")
    first_days = int(cfg.get("first_run_days") or 0)
    first_items = int(cfg.get("first_run_items") or 0)
    max_age = int(cfg["max_age_days"])
    log("=== run basladi (first_run=%s dry_run=%s no_llm=%s bekleyen=%d)" % (first_run, args.dry_run, args.no_llm, len(backlog)))

    lists: list = []  # kanal/sekme basina listeler; sonra round-robin birlestirilir
    if backlog:
        groups: dict = {}
        for i in backlog:
            if only and only not in i.get("channel", "").lower():
                continue
            groups.setdefault((i.get("channel"), i.get("tab")), []).append(i)
        lists.extend(groups.values())
        log("bekleyen: %d icerik onceki calismalardan devraliniyor" % sum(len(v) for v in groups.values()))
    backlog_ids = {i["id"] for i in backlog}

    total_listed = 0
    for ch in cfg["channels"]:
        if only and only not in ch["name"].lower() and only not in ch.get("handle", "").lower():
            continue
        for tab in cfg["tabs"]:
            n_disc = int(cfg["discover_items"])
            if first_run:
                n_disc = max(n_disc, first_items)
            items = discover_tab(ch, tab, n_disc)
            time.sleep(cfg["sleep_seconds"])
            total_listed += len(items)
            new = [i for i in items if i["id"] not in seen and i["id"] not in backlog_ids]
            if first_run:
                n_keep = first_items if first_days > 0 else 0
                keep = new[:n_keep]
                for i in new[n_keep:]:
                    seen[i["id"]] = {"t": now(), "ch": ch["name"], "status": "baseline"}
                new = keep
            for i in new:
                i["channel"] = ch["name"]
                i["age_days"] = first_days if first_run else max_age
            log("  %s/%s: %d listelendi, %d yeni" % (ch["name"], tab, len(items), len(new)))
            lists.append(new)

    if first_run and total_listed == 0:
        raise SystemExit("Kesif bos dondu (ag, VPN/Private Relay veya bot kontrolu). Baslangic noktasi KONMADI; "
                         "'doctor' calistir, sorunu giderip tekrar dene.")

    # round-robin: her kanal/sekme sirayla pay alsin, tek kanal tavani yemesin
    queue, used = [], set()
    for k in range(max((len(l) for l in lists), default=0)):
        for l in lists:
            if k < len(l) and l[k]["id"] not in used:
                used.add(l[k]["id"])
                queue.append(l[k])

    if first_run and first_days <= 0 and not queue and not args.dry_run:
        state["initialized"] = True
        state["last_run"] = now()
        state["backlog"] = []
        save_json(STATE, state)
        n_base = sum(1 for v in seen.values() if v.get("status") == "baseline")
        log("ilk calisma: gecmis istenmedi; mevcut %d icerik 'goruldu' sayildi, bundan sonraki yuklemeler islenecek" % n_base)
        return

    cap = min(args.limit or cfg["max_per_run"], cfg["max_per_run"])
    log("kuyruk: %d icerik, bu calismada en fazla %d islenecek (pencere disindakiler sayilmaz)" % (len(queue), cap))

    if args.dry_run:
        for i in queue:
            print("  [%s] %s  %s  https://youtu.be/%s  (pencere %d gun)" % (i["tab"], i["channel"], i["title"], i["id"], i.get("age_days", max_age)))
        log("dry-run bitti, state degismedi")
        return

    results, deferred, processed = [], [], 0
    today = dt.date.today()
    for idx, i in enumerate(queue):
        if processed >= cap:
            deferred = queue[idx:]
            break
        log("-> %s | %s" % (i["channel"], i["title"][:70]))
        meta, transcript, status = fetch_transcript(cfg, i["id"])
        time.sleep(cfg["sleep_seconds"])
        try:
            up = dt.datetime.strptime(meta.get("upload_date", ""), "%Y%m%d").date()
        except ValueError:
            up = None
        cutoff = today - dt.timedelta(days=int(i.get("age_days", max_age)))
        if up and up < cutoff:
            status = "eski (%s), atlandi" % up.isoformat()
            transcript = ""
        else:
            processed += 1
        summary = ""
        if transcript and not args.no_llm:
            summary = summarize(cfg, i, meta, transcript)
        elif transcript:
            summary = "_(no-llm modu: ozet uretilmedi; transkript %d karakter)_" % len(transcript)
        seen[i["id"]] = {"t": now(), "ch": i["channel"], "status": status}
        results.append({"item": i, "meta": meta, "status": status, "summary": summary, "chars": len(transcript)})
        state["backlog"] = [_slim(x) for x in queue[idx + 1:]]  # yarida kesilirse kalanlar kaybolmasin
        save_json(STATE, state)
        log("   durum: %s" % status)

    state["backlog"] = [_slim(x) for x in deferred]
    state["initialized"] = True
    state["last_run"] = now()
    save_json(STATE, state)
    if deferred:
        log("%d icerik sonraki calismaya ertelendi (bekleyen listede tutuluyor)" % len(deferred))

    if not results:
        log("yeni video yok, rapor yazilmadi")
        return

    path = write_report(cfg, results, args.no_llm)
    log("rapor: %s" % path)
    n_ok = sum(1 for r in results if r["summary"] and not r["summary"].startswith("_("))
    n_in = sum(1 for r in results if not r["status"].startswith("eski"))
    notify(cfg, "Niche Radar", "%d yeni icerik, %d ozet hazir. %s" % (n_in, n_ok, path.name))
    log("=== run bitti")


def write_report(cfg: dict, results: list, no_llm: bool) -> Path:
    rdir = report_dir(cfg)
    rdir.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    path = rdir / ("%s.md" % today)
    blocks, sections = [], []
    for r in results:
        if r["status"].startswith("eski"):
            continue  # tarih penceresi disinda: sadece durum tablosunda gorunur
        i, m = r["item"], r["meta"]
        title = m.get("title") or i["title"]
        url = "https://www.youtube.com/watch?v=%s" % i["id"]
        kind = "Shorts" if i["tab"] == "shorts" else "Video"
        date = m.get("upload_date", "")
        date = "%s-%s-%s" % (date[:4], date[4:6], date[6:8]) if len(date) == 8 else "?"
        head = "### [%s](%s)\n%s · **%s** · %s · %s görüntülenme · %s" % (
            title, url, kind, i["channel"], human_duration(m.get("duration")), human_views(m.get("view_count")), date)
        body = r["summary"] if r["summary"] else "_Durum: %s_" % r["status"]
        sections.append(head + "\n\n" + body)
        if r["summary"] and not r["summary"].startswith("_("):
            blocks.append("## %s — %s (%s)\n%s" % (i["channel"], title, url, r["summary"]))

    digest = ""
    if cfg.get("digest") and not no_llm and len(blocks) >= 2:
        digest = make_digest(cfg, blocks)

    cell = lambda x: str(x).replace("|", "\\|")  # noqa: E731
    status_rows = "\n".join("| %s | %s | %s |" % (cell(r["item"]["channel"]), cell((r["meta"].get("title") or r["item"]["title"])[:60]), cell(r["status"]))
                            for r in results)
    n_sum = sum(1 for r in results if r["summary"] and not r["summary"].startswith("_("))
    n_old = sum(1 for r in results if r["status"].startswith("eski"))
    n_txt = sum(1 for r in results if r["summary"].startswith("_("))  # no-llm: transkript var, ozet yok
    n_err = len(results) - n_sum - n_old - n_txt
    head_line = "**%d yeni içerik**, %d özet" % (len(results) - n_old, n_sum)
    if n_txt:
        head_line += ", %d transkript (özetsiz)" % n_txt
    if n_old:
        head_line += ", %d tarih penceresi dışı" % n_old
    if n_err:
        head_line += ", %d atlandı/hatalı" % n_err
    out = ["# Niche Radar · %s" % today, "", head_line + ". Üretim: %s" % now(), ""]
    if digest:
        out += ["## Günün öne çıkanları", "", digest, ""]
    if sections:
        out += ["## Videolar", ""] + [s + "\n" for s in sections]
    else:
        out += ["## Videolar", "", "_Bu çalışmada tarih penceresi içinde yeni içerik yok._", ""]
    out += ["## Durum tablosu", "", "| Kanal | Video | Durum |", "|---|---|---|", status_rows, ""]
    text = "\n".join(out)
    if path.exists():
        text = path.read_text(encoding="utf-8") + "\n\n---\n\n" + text
    path.write_text(text, encoding="utf-8")
    return path


# ----------------------------------------------------------------- zamanlayici
def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / ("%s.plist" % LABEL)


def schedule_status_text() -> str:
    if IS_MAC:
        r = run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), LABEL)], timeout=30)
        if r.returncode == 0:
            m = re.search(r"state = ([^\n]+)", r.stdout)
            return "launchd yuklu (%s), %s" % (m.group(1) if m else "?", plist_path())
        return "kurulu degil"
    if IS_WIN:
        r = run(["schtasks", "/Query", "/TN", "NicheRadar"], timeout=30)
        return "Task Scheduler kurulu" if r.returncode == 0 else "kurulu degil"
    return "bu OS icin otomatik zamanlayici yok (cron kullan)"


def cmd_schedule(args) -> None:
    cfg = load_config()
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(cfg["schedule_time"]).strip())
    if not m:
        raise SystemExit("config.json schedule_time gecersiz: %r (beklenen HH:MM, 00-23:00-59)" % cfg["schedule_time"])
    hh, mm = m.group(1).zfill(2), m.group(2)
    script = HOME / "radar.py"
    if not script.exists():
        cmd_install(args)
    py = sys.executable
    if args.action == "status":
        print(schedule_status_text())
        return
    if IS_MAC:
        p = plist_path()
        if args.action == "remove":
            run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)], timeout=30)
            if p.exists():
                p.unlink()
            print("kaldirildi")
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        path_env = extra_path() + ":/usr/bin:/bin:/usr/sbin:/sbin"
        p.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key><array><string>%s</string><string>%s</string><string>run</string></array>
  <key>WorkingDirectory</key><string>%s</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>%d</integer><key>Minute</key><integer>%d</integer></dict>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>%s</string><key>HOME</key><string>%s</string></dict>
  <key>StandardOutPath</key><string>%s</string>
  <key>StandardErrorPath</key><string>%s</string>
</dict></plist>
""" % tuple([LABEL] + [xml_escape(str(x)) for x in (py, script, HOME)] + [int(hh), int(mm)]
            + [xml_escape(str(x)) for x in (path_env, Path.home(), LOGS / "launchd.out.log", LOGS / "launchd.err.log")]),
            encoding="utf-8")
        run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)], timeout=30)
        r = run(["launchctl", "bootstrap", "gui/%d" % os.getuid(), str(p)], timeout=30)
        if r.returncode != 0:
            raise SystemExit("launchctl bootstrap hatasi: %s" % r.stderr.strip())
        print("kuruldu: her gun %s:%s -> %s" % (hh, mm, p))
        print("Not: Mac uykudaysa uyaninca kacan calismayi yapar; kapaliysa yapmaz.")
        return
    if IS_WIN:
        if args.action == "remove":
            run(["schtasks", "/Delete", "/TN", "NicheRadar", "/F"], timeout=30)
            print("kaldirildi")
            return
        xml_path = HOME / "NicheRadar.task.xml"
        xml_path.write_text("""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><CalendarTrigger><StartBoundary>2026-01-01T%s:%s:00</StartBoundary><Enabled>true</Enabled>
    <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
  <Settings><StartWhenAvailable>true</StartWhenAvailable><WakeToRun>true</WakeToRun>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit><MultipleInstances>IgnoreNew</MultipleInstances></Settings>
  <Actions Context="Author"><Exec><Command>%s</Command><Arguments>"%s" run</Arguments><WorkingDirectory>%s</WorkingDirectory></Exec></Actions>
</Task>
""" % (hh, mm, xml_escape(str(py)), xml_escape(str(script)), xml_escape(str(HOME))), encoding="utf-16")
        r = run(["schtasks", "/Create", "/TN", "NicheRadar", "/XML", str(xml_path), "/F"], timeout=30)
        if r.returncode != 0:
            raise SystemExit("schtasks hatasi: %s %s" % (r.stdout.strip(), r.stderr.strip()))
        print("kuruldu: her gun %s:%s (Task Scheduler, WakeToRun acik)" % (hh, mm))
        return
    print("Linux: crontab -e -> %s %s * * * %s %s run" % (int(mm), int(hh), py, script))


# ----------------------------------------------------------------- main
def positive_int(v: str) -> int:
    n = int(v)
    if n <= 0:
        raise argparse.ArgumentTypeError("pozitif bir sayi olmali")
    return n


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Niche Radar")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("install").set_defaults(fn=cmd_install)
    a = sub.add_parser("add-channel"); a.add_argument("channel", nargs="+"); a.set_defaults(fn=cmd_add)
    c = sub.add_parser("check-channel"); c.add_argument("channel", nargs="+"); c.set_defaults(fn=cmd_check)
    d = sub.add_parser("remove-channel"); d.add_argument("channel", nargs="+"); d.set_defaults(fn=cmd_remove)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--no-llm", action="store_true")
    r.add_argument("--limit", type=positive_int, default=0)
    r.add_argument("--only", default="")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("schedule"); s.add_argument("action", choices=["install", "remove", "status"]); s.set_defaults(fn=cmd_schedule)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
