#!/usr/bin/env python3
"""AgBot - a personal Garmin coaching Telegram bot.

Long-polls Telegram and, for each message from the owner:
  * "GMS" / "AgBot: GMS" / "summary" / "/gms"  -> morning summary
  * anything else                              -> a coaching Q&A answer

Both are produced by pulling a fresh Garmin snapshot (garmin_coach.build_snapshot)
and asking a language-model backend (run_llm - the GitHub Copilot CLI by default;
swap in OpenAI / Anthropic / Ollama, see run_llm below) to write the reply. The
reply text is sent back to Telegram. No cloud services beyond the model provider
and Telegram are required.

If the owner has not received a summary by ~09:30 local time, one is pushed
automatically (once per day).

Runs as a single persistent process (see scripts/run_bridge.ps1 / run_bridge.sh).
A localhost port lock guarantees only one instance polls at a time.

Manual test modes (no Telegram needed):
  python telegram_bridge.py --selftest-summary
  python telegram_bridge.py --selftest-qa "how did I sleep?"
"""
import os
import re
import sys
import json
import html
import time
import socket
import logging
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state")
LOGS = os.path.join(BASE, "logs")
os.makedirs(STATE, exist_ok=True)
os.makedirs(LOGS, exist_ok=True)

TOKEN_FILE = os.path.join(STATE, "telegram_token.txt")
OWNER_FILE = os.path.join(STATE, "telegram_chat_id.txt")
OFFSET_FILE = os.path.join(STATE, "tg_offset.txt")
LAST_SUMMARY_FILE = os.path.join(STATE, "last_summary_date.txt")
HISTORY_FILE = os.path.join(STATE, "conversation.json")
TODAYS_BRIEF_FILE = os.path.join(STATE, "todays_brief.json")
PROMPTS = os.path.join(BASE, "prompts")
PROFILE_FILE = os.path.join(BASE, "profile.md")
SUMMARY_PROMPT_FILE = os.path.join(PROMPTS, "summary_prompt.md")
QA_PROMPT_FILE = os.path.join(PROMPTS, "qa_prompt.md")
IMAGE_PROMPT_FILE = os.path.join(PROMPTS, "image_prompt.md")
WEEKLY_PROMPT_FILE = os.path.join(PROMPTS, "weekly_prompt.md")
NUTRITION_PROMPT_FILE = os.path.join(PROMPTS, "nutrition_prompt.md")
DEBRIEF_PROMPT_FILE = os.path.join(PROMPTS, "debrief_prompt.md")
PERF_PROMPT_FILE = os.path.join(PROMPTS, "performance_prompt.md")
JOURNAL_FILE = os.path.join(STATE, "journal.jsonl")
HEALTH_FILE = os.path.join(STATE, "health.jsonl")
LAST_ACTIVITY_FILE = os.path.join(STATE, "last_activity_id.txt")
RED_FLAGS_FILE = os.path.join(STATE, "red_flags_date.txt")
MEAL_STATE_FILE = os.path.join(STATE, "meal_reminders.json")
FITNESS_FILE = os.path.join(STATE, "fitness_profile.json")
INCOMING = os.path.join(STATE, "incoming")
os.makedirs(INCOMING, exist_ok=True)

COPILOT_EXE = os.environ.get("COPILOT_EXE", "copilot")
COPILOT_HOME = os.environ.get("COPILOT_HOME", os.path.expanduser("~/.copilot"))

# Which model/agent backend generates replies. "copilot" = GitHub Copilot CLI
# (default, works out of the box). See run_llm() to enable openai / anthropic / ollama.
LLM_BACKEND = os.environ.get("AGBOT_LLM", "copilot")
# Optional: the user's first name, used only so the coach can greet by name. All real
# personalization lives in profile.md - this is just a convenience override.
USER_NAME = os.environ.get("AGBOT_USER_NAME", "").strip()

# Auto-summary window (local time). If no summary has been sent yet today and the
# current time falls in this window, one is pushed automatically.
AUTO_START = (9, 30)
AUTO_END = (20, 0)

# Daily meal-logging reminders (local time): (slot, hour, minute, message). Each fires
# once per day, guarded by a per-slot date marker in MEAL_STATE_FILE. If the bot was down
# at the scheduled minute it still nudges within MEAL_CATCHUP_MIN; past that it skips the
# slot for the day so you never get a stale ping hours late.
MEAL_REMINDERS = (
    ("breakfast", 8, 30,
     "\U0001F373 AgBot reminder \u00B7 Breakfast - log what you eat so I can track your "
     "fuel & protein through the day. Reply e.g. 'log: 3 eggs, oats, coffee'."),
    ("lunch", 12, 15,
     "\U0001F957 AgBot reminder \u00B7 Lunch - time to log your midday meal. "
     "Reply e.g. 'log: chicken, rice & salad'."),
    ("dinner", 21, 0,
     "\U0001F37D\uFE0F AgBot reminder \u00B7 Dinner - log tonight's meal so today's intake "
     "is complete. Reply e.g. 'log: salmon, potatoes, veg'."),
)
MEAL_CATCHUP_MIN = 90  # still nudge if the bot came online within this window after the time

POLL_TIMEOUT = 50          # long-poll seconds
COPILOT_TIMEOUT = 180      # seconds for a single generation
SINGLETON_PORT = 49517     # localhost lock so only one instance polls
MAX_STORED_TURNS = 40      # conversation turns kept on disk
FITNESS_MAX_AGE_H = 20     # refresh cached fitness profile if older than this
HISTORY_PROMPT_TURNS = 14  # recent turns injected into a Q&A prompt
HISTORY_PROMPT_CHARS = 5000  # char budget for injected history
JOURNAL_PROMPT_CHARS = 2500  # durable notes injected into every prompt
JOURNAL_KEEP = 60            # journal lines kept on disk
HEALTH_ACTIVE_DAYS = 45      # active injury/illness flags injected for up to this long
HEALTH_PROMPT_CHARS = 800    # char budget for injected health flags
ACTIVITY_CHECK_SECS = 300    # how often to poll for a finished workout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOGS, "telegram_bridge.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("agbot")

sys.path.insert(0, BASE)
import garmin_coach  # noqa: E402


# --------------------------------------------------------------------- helpers
def read_file(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def write_file(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def append_history(role, text):
    hist = load_history()
    hist.append({"role": role, "text": text,
                 "ts": datetime.now().isoformat(timespec="seconds")})
    hist = hist[-MAX_STORED_TURNS:]
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as fh:
            json.dump(hist, fh, ensure_ascii=False, indent=1)
    except Exception as exc:  # noqa: BLE001
        log.error("history save failed: %s", exc)


def clear_history():
    try:
        os.remove(HISTORY_FILE)
    except FileNotFoundError:
        pass


def save_todays_brief(text):
    """Persist today's morning brief (recovery read + workout/recommendations) so the
    coach can still reference 'the plan/recommendations' later in the day even after the
    volatile chat window has scrolled past it."""
    try:
        with open(TODAYS_BRIEF_FILE, "w", encoding="utf-8") as fh:
            json.dump({"date": date.today().isoformat(), "text": text},
                      fh, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        log.error("brief save failed: %s", exc)


def todays_brief_text():
    """The brief sent earlier TODAY, or '' if none yet today (auto-expires by date)."""
    try:
        with open(TODAYS_BRIEF_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return ""
    if isinstance(d, dict) and d.get("date") == date.today().isoformat():
        return (d.get("text") or "").strip()
    return ""


def _day_tag(date_str):
    """Relative-day label so the model never has to do date math to place a dated entry
    ('today' / 'yesterday' / 'N days ago'). Empty for unparseable or future dates."""
    try:
        d = date.fromisoformat((date_str or "")[:10])
    except ValueError:
        return ""
    delta = (date.today() - d).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "yesterday"
    if delta > 1:
        return str(delta) + " days ago"
    return ""


def recent_history_text():
    hist = load_history()[-HISTORY_PROMPT_TURNS:]
    lines = []
    for turn in hist:
        who = "You" if turn.get("role") == "user" else "AgBot"
        raw = turn.get("ts") or ""
        short = raw[:16].replace("T", " ")
        tag = _day_tag(raw)
        stamp = ("[" + short + ((" " + tag) if tag else "") + "] ") if raw else ""
        lines.append(stamp + who + ": " + (turn.get("text") or "").strip())
    text = "\n".join(lines)
    if len(text) > HISTORY_PROMPT_CHARS:
        text = "..." + text[-HISTORY_PROMPT_CHARS:]
    return text


def append_journal(text):
    entry = {"date": date.today().isoformat(), "text": text.strip(),
             "ts": datetime.now().isoformat(timespec="seconds")}
    try:
        with open(JOURNAL_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        log.error("journal save failed: %s", exc)


def recent_journal_text():
    try:
        with open(JOURNAL_FILE, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().strip().split("\n") if ln]
    except FileNotFoundError:
        return ""
    entries = []
    for ln in lines[-JOURNAL_KEEP:]:
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        d = e.get("date", "")
        tag = _day_tag(d)
        label = d + ((" (" + tag + ")") if tag else "")
        entries.append((label + ": " + (e.get("text") or "")).strip())
    text = "\n".join(entries)
    if len(text) > JOURNAL_PROMPT_CHARS:
        text = "..." + text[-JOURNAL_PROMPT_CHARS:]
    return text


HEALTH_RE = re.compile(
    r"\b("
    r"injur\w*|hurts?|hurting|"
    r"pain\w*|sore(?:ness)?|aching|aches?|"
    r"sprain\w*|strained|straining|pulled|tweak\w*|twisted|"
    r"sick|ill|unwell|fever\w*|the flu|a cold|coughing|sore throat|"
    r"nause\w*|dizzy|migraine|headache|cramp\w*|"
    r"swollen|swelling|stiff\w*|niggl\w*|"
    r"food poisoning|throwing up|vomit\w*|"
    r"can'?t (?:walk|run|lift|train|move)"
    r")\b", re.IGNORECASE)


def append_health(text, status="active"):
    text = (text or "").strip()[:240]
    if not text:
        return
    entry = {"date": date.today().isoformat(), "text": text, "status": status,
             "ts": datetime.now().isoformat(timespec="seconds")}
    try:
        with open(HEALTH_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        log.error("health save failed: %s", exc)


def _load_health():
    try:
        with open(HEALTH_FILE, "r", encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        return []


def active_health_text():
    """Injuries/illness the user reported and hasn't marked recovered - injected into EVERY
    prompt so no recommendation ignores them, and they don't scroll out of chat."""
    items = _load_health()
    if not items:
        return ""
    cutoff = (date.today() - timedelta(days=HEALTH_ACTIVE_DAYS)).isoformat()
    active = [e for e in items if e.get("status", "active") == "active"
              and str(e.get("date", "")) >= cutoff]
    if not active:
        return ""
    lines = [(str(e.get("date", "")) + ": " + (e.get("text") or "")).strip() for e in active]
    text = "\n".join(lines)
    if len(text) > HEALTH_PROMPT_CHARS:
        text = text[-HEALTH_PROMPT_CHARS:]
    return text


def resolve_health():
    items = _load_health()
    cleared = [e for e in items if e.get("status", "active") == "active"]
    if not cleared:
        return []
    for e in items:
        if e.get("status", "active") == "active":
            e["status"] = "resolved"
    try:
        with open(HEALTH_FILE, "w", encoding="utf-8") as fh:
            for e in items:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        log.error("health resolve failed: %s", exc)
    return [(e.get("text") or "").strip() for e in cleared]


def _health_snippet(probe):
    """Keep only the clause(s) that actually mention the symptom, so a long
    multi-topic message isn't stored verbatim as a 'flag'."""
    frags = re.split(r"(?<=[.!?])\s+|\n+", probe)
    hits = [f.strip(" -\u2022\t") for f in frags if f.strip() and HEALTH_RE.search(f)]
    snip = "; ".join(hits) if hits else probe
    snip = re.sub(r"\s+", " ", snip).strip()
    return snip[:200]


def _maybe_capture_health(kind, text, payload):
    """Durably record a self-reported injury/illness so it's injected into every future
    prompt (until the user says 'recovered'), not just the ephemeral chat window."""
    if kind not in ("qa", "log"):
        return
    probe = ((payload if kind == "log" else text) or "").strip()
    if not probe or not HEALTH_RE.search(probe):
        return
    if not re.search(r"\b(i|i'?m|ive|i'?ve|my|me)\b", probe.lower()):
        return  # only first-person reports about the user's own state
    snippet = _health_snippet(probe)
    for e in _load_health():
        if e.get("status", "active") == "active" and (e.get("text") or "").strip() == snippet:
            return  # already on record
    append_health(snippet)
    log.info("Captured health flag: %r", snippet[:80])


def load_fitness_profile():
    """Return the cached slow-changing fitness profile (fitness age, race
    predictions, endurance/hill score, VO2max-driven metrics, FTP, weekly
    intensity minutes), refreshing at most once per FITNESS_MAX_AGE_H via a
    Garmin fetch. Falls back to the stale cache (or None) and never raises."""
    cached = None
    try:
        with open(FITNESS_FILE, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
    except Exception:  # noqa: BLE001
        cached = None
    if isinstance(cached, dict):
        try:
            age_h = ((datetime.now()
                      - datetime.fromisoformat(cached.get("generated_at")))
                     .total_seconds() / 3600)
            if age_h < FITNESS_MAX_AGE_H:
                return cached
        except Exception:  # noqa: BLE001
            pass
    prof = garmin_coach.fitness_profile()
    if isinstance(prof, dict) and "__error__" not in prof:
        try:
            with open(FITNESS_FILE, "w", encoding="utf-8") as fh:
                json.dump(prof, fh, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            log.error("fitness cache save failed: %s", exc)
        return prof
    return cached  # stale cache (or None) if the refresh failed


def load_token():
    raw = os.environ.get("AGBOT_TELEGRAM_TOKEN", "").strip() or read_file(TOKEN_FILE)
    m = re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", raw)
    if not m:
        raise SystemExit(
            "No valid Telegram bot token found. Set AGBOT_TELEGRAM_TOKEN or put the "
            "token in " + TOKEN_FILE)
    return m.group(0)


TOKEN = load_token()
API = "https://api.telegram.org/bot" + TOKEN


def tg(method, params=None, timeout=60):
    url = API + "/" + method
    data = urllib.parse.urlencode(params).encode("utf-8") if params else None
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(method, params, timeout=30):
    """POST that returns Telegram's JSON even on HTTP 4xx (never raises)."""
    url = API + "/" + method
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "error_code": getattr(exc, "code", 0),
                    "description": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "description": str(exc)}


def _chunks(text, n):
    if not text:
        return [""]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > n:
            if cur:
                out.append(cur)
            while len(line) > n:
                out.append(line[:n])
                line = line[n:]
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        out.append(cur)
    return out


# ---- lightweight Markdown -> Telegram-HTML renderer -----------------------
# The model writes natural Markdown; Telegram renders a small HTML subset
# (<b> <i> <u> <s> <code> <pre> <a>). We convert the constructs the coach
# actually uses and, if Telegram ever rejects the markup, fall back to plain
# text so a message is never lost. Underscore italics are deliberately NOT
# supported so identifiers like strain_yesterday survive intact.
_SENT_O, _SENT_C = "\ue000", "\ue001"
_RE_FENCE = re.compile(r"```[ \t]*[A-Za-z0-9_+-]*\n(.*?)```", re.DOTALL)
_RE_ICODE = re.compile(r"`([^`\n]+)`")
_RE_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_RE_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_RE_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])")
_RE_HEAD = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_RE_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_RE_HR = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_RE_RESTORE = re.compile(_SENT_O + r"(\d+)" + _SENT_C)


def md_to_html(text):
    """Convert the model's Markdown-ish text to Telegram-safe HTML."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Unwrap a single code fence that wraps the WHOLE message (model slip-up).
    s = text.strip()
    if s.startswith("```") and s.endswith("```") and s.count("```") == 2:
        s = re.sub(r"^```[ \t]*[A-Za-z0-9_+-]*\n?", "", s)
        text = s[:-3].strip("\n")

    store = []

    def _stash(frag):
        store.append(frag)
        return _SENT_O + str(len(store) - 1) + _SENT_C

    # Protect code first (its contents must not be re-formatted).
    text = _RE_FENCE.sub(
        lambda m: _stash("<pre>" + html.escape(m.group(1).rstrip("\n"), quote=False) + "</pre>"),
        text)
    text = _RE_ICODE.sub(
        lambda m: _stash("<code>" + html.escape(m.group(1), quote=False) + "</code>"),
        text)

    # Escape everything else, then inject the known-good tags.
    text = html.escape(text, quote=False)
    text = _RE_LINK.sub(
        lambda m: _stash('<a href="' + html.escape(m.group(2), quote=True) + '">'
                         + m.group(1) + "</a>"),
        text)
    text = _RE_BOLD.sub(lambda m: "<b>" + m.group(1) + "</b>", text)
    text = _RE_STRIKE.sub(lambda m: "<s>" + m.group(1) + "</s>", text)
    text = _RE_ITALIC.sub(lambda m: "<i>" + m.group(1) + "</i>", text)

    out = []
    for line in text.split("\n"):
        if _RE_HR.match(line):
            continue
        h = _RE_HEAD.match(line)
        if h:
            out.append("<b>" + h.group(1) + "</b>")
            continue
        b = _RE_BULLET.match(line)
        if b:
            out.append(("  " if b.group(1) else "") + "\u2022 " + b.group(2))
            continue
        out.append(line)
    text = "\n".join(out)

    return _RE_RESTORE.sub(lambda m: store[int(m.group(1))], text)


def _html_to_plain(html_text):
    """Strip tags / unescape entities for the plain-text fallback path."""
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


def send_message(chat_id, text):
    rendered = md_to_html(text)
    for chunk in _chunks(rendered, 4000):
        if not chunk.strip():
            continue
        resp = _post("sendMessage", {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        })
        if resp.get("ok"):
            continue
        # Telegram rejected the markup - resend as plain text so nothing is lost.
        log.warning("HTML send failed (%s); retrying as plain text",
                    resp.get("description"))
        resp = _post("sendMessage", {
            "chat_id": chat_id,
            "text": _html_to_plain(chunk),
            "disable_web_page_preview": "true",
        })
        if not resp.get("ok"):
            log.error("sendMessage failed (plain too): %s", resp.get("description"))


def owner():
    v = os.environ.get("AGBOT_OWNER_CHAT_ID", "").strip() or read_file(OWNER_FILE).strip()
    return v or None


def set_owner(chat_id):
    write_file(OWNER_FILE, str(chat_id))
    log.info("Owner locked to chat_id %s", chat_id)


def get_offset():
    v = read_file(OFFSET_FILE).strip()
    return int(v) if v.lstrip("-").isdigit() else None


def set_offset(update_id):
    write_file(OFFSET_FILE, str(update_id))


def download_file(file_id):
    info = tg("getFile", {"file_id": file_id})
    fp = (info.get("result") or {}).get("file_path")
    if not fp:
        return None
    url = "https://api.telegram.org/file/bot" + TOKEN + "/" + fp
    ext = os.path.splitext(fp)[1] or ".jpg"
    dest = os.path.join(INCOMING, "img_" + str(int(time.time())) + ext)
    urllib.request.urlretrieve(url, dest)
    return dest


# ---------------------------------------------------------------- transcription
WHISPER_MODEL_NAME = os.environ.get("AGBOT_WHISPER_MODEL", "base")
# Language hint: set to your spoken language ("en", "es", ...); "" to auto-detect.
WHISPER_LANG = os.environ.get("AGBOT_WHISPER_LANG", "en")
_whisper_model = None


def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel  # lazy import keeps startup light
        _whisper_model = WhisperModel(WHISPER_MODEL_NAME, device="cpu",
                                      compute_type="int8")
    return _whisper_model


def transcribe_audio(path):
    """Local Whisper transcription of a voice/audio file. Returns the text, or
    None if nothing intelligible was found or the model is unavailable."""
    try:
        model = _get_whisper()
    except Exception as exc:  # noqa: BLE001 - e.g. faster-whisper not installed
        log.error("whisper unavailable: %s", exc)
        return None
    try:
        kwargs = {"beam_size": 5, "vad_filter": True}
        if WHISPER_LANG:
            kwargs["language"] = WHISPER_LANG
        segments, _info = model.transcribe(path, **kwargs)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text or None
    except Exception as exc:  # noqa: BLE001
        log.error("transcription failed: %s", exc)
        return None


# ------------------------------------------------------------------- video frames
VIDEO_MAX_FRAMES = int(os.environ.get("AGBOT_VIDEO_FRAMES", "6"))
VIDEO_FRAME_MAXDIM = 1024


def _downscale(img, max_dim=VIDEO_FRAME_MAXDIM):
    w, h = img.size
    m = max(w, h)
    if m <= max_dim:
        return img
    s = max_dim / float(m)
    return img.resize((max(1, int(w * s)), max(1, int(h * s))))


def extract_video_frames(path, max_frames=VIDEO_MAX_FRAMES):
    """Sample up to max_frames evenly-spaced JPEG frames from a video with PyAV.

    Returns a list of file paths (possibly empty). No external ffmpeg needed -
    PyAV bundles the decoders (the same reason voice transcription works)."""
    try:
        import av  # noqa: PLC0415 - heavy optional dep, imported lazily
    except Exception as exc:  # noqa: BLE001
        log.error("PyAV unavailable, cannot read video: %s", exc)
        return []
    try:
        container = av.open(path)
    except Exception as exc:  # noqa: BLE001
        log.error("could not open video: %s", exc)
        return []
    out = []
    try:
        vs = next((s for s in container.streams if s.type == "video"), None)
        if vs is None:
            return []
        if container.duration:
            total = container.duration / 1_000_000.0
        elif vs.duration and vs.time_base:
            total = float(vs.duration * vs.time_base)
        else:
            total = 0.0
        stamp = int(time.time())
        if total and total > 0.6 and vs.time_base:
            targets = [total * (i + 0.5) / max_frames for i in range(max_frames)]
            for i, t in enumerate(targets):
                try:
                    container.seek(int(t / vs.time_base), stream=vs, backward=True)
                    frame = next(container.decode(vs), None)
                    if frame is None:
                        continue
                    fp = os.path.join(INCOMING, "vf_%d_%02d.jpg" % (stamp, i))
                    _downscale(frame.to_image()).save(fp, "JPEG", quality=85)
                    out.append(fp)
                except Exception as exc:  # noqa: BLE001
                    log.warning("frame at %.1fs failed: %s", t, exc)
        else:  # short or unknown-duration clip: take the first frames decoded
            for frame in container.decode(vs):
                fp = os.path.join(INCOMING, "vf_%d_%02d.jpg" % (stamp, len(out)))
                _downscale(frame.to_image()).save(fp, "JPEG", quality=85)
                out.append(fp)
                if len(out) >= max_frames:
                    break
    finally:
        try:
            container.close()
        except Exception:  # noqa: BLE001
            pass
    return out


# ------------------------------------------------------------------ generation
def _scrub(text):
    """Drop any stray Copilot stats footer that slips past -s."""
    lines = text.rstrip().split("\n")
    for i, ln in enumerate(lines):
        s = ln.strip()
        if (s.startswith("AI Credits") or s.startswith("Resume ")
                or s.startswith("Changes ") or s.startswith("Tokens ")):
            lines = lines[:i]
            break
    return "\n".join(lines).strip()


def run_llm(prompt, image=None, images=None):
    """Generate a reply from the configured model/agent backend.

    *** THIS IS THE ONE PLACE TO CHANGE TO USE A DIFFERENT MODEL. ***
    Select a backend with the AGBOT_LLM env var (default "copilot"). To add your
    own, write a _llm_<name>(prompt, images) -> str and register it in _LLM_BACKENDS
    below. `images` is a list of local image file paths; only vision-capable
    backends use them (text-only backends simply ignore them).
    """
    if len(prompt) > 120000:
        log.warning("prompt is very large (%d chars, ~%dK tokens)", len(prompt), len(prompt) // 4000)
    imgs = list(images or [])
    if image:
        imgs.insert(0, image)
    backend = _LLM_BACKENDS.get(LLM_BACKEND)
    if backend is None:
        log.error("Unknown AGBOT_LLM backend %r (available: %s)",
                  LLM_BACKEND, ", ".join(sorted(_LLM_BACKENDS)))
        return None
    try:
        return backend(prompt, imgs)
    except Exception as exc:  # noqa: BLE001
        log.error("LLM backend %r failed: %s", LLM_BACKEND, exc)
        return None


def _llm_copilot(prompt, images):
    """Default backend: the GitHub Copilot CLI (`copilot`). The prompt is piped via
    stdin (so the OS command-line length limit never applies); images are passed as
    --attachment (Copilot is vision-capable)."""
    env = dict(os.environ)
    for k in ("AGENCY_ENGINE", "AGENCY_SESSION_ID", "AGENCY_OPERATION_ID",
              "AGENCY_LOG_SESSION_DIR", "COPILOT_AGENT_SESSION_ID",
              "COPILOT_LOADER_PID", "COPILOT_CLI", "MSFT_AGENCY"):
        env.pop(k, None)
    env["COPILOT_HOME"] = COPILOT_HOME
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    cmd = [
        COPILOT_EXE, "-s",
        "--no-ask-user", "--allow-all-tools",
        "--no-custom-instructions",
        "--disable-builtin-mcps",
        "--no-color", "--no-remote", "--no-remote-export",
        "--reasoning-effort", "low",
    ]
    for att in images:
        if att:
            cmd += ["--attachment", att]
    try:
        res = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=COPILOT_TIMEOUT, env=env, cwd=BASE,
        )
    except subprocess.TimeoutExpired:
        log.error("copilot timed out after %ss", COPILOT_TIMEOUT)
        return None
    if res.returncode != 0:
        log.error("copilot exit %s: %s", res.returncode, (res.stderr or "")[:600])
    return _scrub(res.stdout or "") or None


# --- Optional alternative backends -------------------------------------------
# Uncomment one (and `pip install` its SDK + set its API key), then set AGBOT_LLM
# to its name. Each takes (prompt: str, images: list[str]) and returns reply text.
#
# def _llm_openai(prompt, images):
#     from openai import OpenAI              # pip install openai ; set OPENAI_API_KEY
#     import base64, mimetypes
#     content = [{"type": "text", "text": prompt}]
#     for p in images:
#         mt = mimetypes.guess_type(p)[0] or "image/jpeg"
#         b64 = base64.b64encode(open(p, "rb").read()).decode()
#         content.append({"type": "image_url", "image_url": {"url": f"data:{mt};base64,{b64}"}})
#     r = OpenAI().chat.completions.create(
#         model=os.environ.get("AGBOT_OPENAI_MODEL", "gpt-4o"),
#         messages=[{"role": "user", "content": content}])
#     return r.choices[0].message.content
#
# def _llm_anthropic(prompt, images):
#     import anthropic, base64, mimetypes    # pip install anthropic ; set ANTHROPIC_API_KEY
#     blocks = [{"type": "text", "text": prompt}]
#     for p in images:
#         mt = mimetypes.guess_type(p)[0] or "image/jpeg"
#         b64 = base64.b64encode(open(p, "rb").read()).decode()
#         blocks.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}})
#     msg = anthropic.Anthropic().messages.create(
#         model=os.environ.get("AGBOT_ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"),
#         max_tokens=1500, messages=[{"role": "user", "content": blocks}])
#     return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
#
# def _llm_ollama(prompt, images):
#     import base64, requests                # local Ollama server (ollama serve)
#     payload = {"model": os.environ.get("AGBOT_OLLAMA_MODEL", "llama3.1"),
#                "prompt": prompt, "stream": False}
#     if images:
#         payload["images"] = [base64.b64encode(open(p, "rb").read()).decode() for p in images]
#     return requests.post("http://localhost:11434/api/generate",
#                          json=payload, timeout=COPILOT_TIMEOUT).json().get("response")

_LLM_BACKENDS = {
    "copilot": _llm_copilot,
    # "openai": _llm_openai,
    # "anthropic": _llm_anthropic,
    # "ollama": _llm_ollama,
}


def today_human():
    return datetime.now().strftime("%a %d %b")


# Injected into every generation path (summary, Q&A, image, weekly, nutrition,
# performance). Forces the model to reason over the WHOLE data picture, not a
# single metric, then report tightly. The bot's standing coaching directive.
DATA_USE_DIRECTIVE = (
    "COACHING DIRECTIVE - read before you answer:\n"
    "Ground every recommendation in the user's FULL data picture above, never one metric in "
    "isolation. Weigh together, and only use fields that are actually present:\n"
    "- Recovery: for a morning brief prefer morning_readiness (wake-time, WELL_RECOVERED etc.); "
    "otherwise training-readiness score + sub-factors, recovery-time hours, HRV vs baseline, "
    "body_battery_current (Garmin's most-recent value; matches the Connect app/web), sleep "
    "score/duration/stages, stress.\n"
    "- Illness / overreaching watch: last_night_sleep.skin_temp_deviation_c is the deviation from "
    "the user's ~19-day baseline. A notable swing (roughly >=+/-0.5C) - ESPECIALLY together with a "
    "raised resting HR (wellness_today.rhr_today vs rhr_7d_avg), elevated respiration "
    "(wellness_today.respiration_sleep_avg / waking), low HRV, or breathing_disruptions - suggests "
    "the body is fighting illness, alcohol, heat or under-recovery; when several line up, back off "
    "intensity and say why. A small deviation with everything else normal is noise - don't alarm.\n"
    "- Day strain: wellness_today sedentary_h vs active_h/highly_active_h (long sedentary days are "
    "their own load), the stress-duration split (rest/low/medium/high_stress_min + stress_qualifier), "
    "and the resting-HR trend (rhr_today vs rhr_7d_avg - a multi-day rise is an early fatigue flag).\n"
    "- Load & direction: ACWR + acute/chronic load, training status/trend, and the last 7 days of "
    "sessions - including logged_sets (exercises, reps, top weight) so you know which muscle groups "
    "were just trained. Use training_status.load_focus (aerobic-low / aerobic-high / anaerobic load "
    "vs Garmin's target ranges + its feedback phrase, e.g. AEROBIC_LOW_FOCUS) to steer WHICH kind of "
    "session to add so the 4-week balance moves toward target.\n"
    "- Fitness: VO2max, fitness age, endurance score, hill score, race predictions, lactate-threshold "
    "HR, cycling FTP, weekly intensity minutes vs goal, and personal_records (nudge when a session is "
    "within reach of a PR).\n"
    "- Body & context: weight trend, remaining calorie budget, sweat_loss_ml (post-sweaty-session "
    "rehydration nudge vs hydration_goal_ml), and the NOTES the user logged.\n"
    "- Post-workout only: ACTIVITY_EXTRAS gives time-in-HR-zone (minutes per zone -> was it truly "
    "easy/hard, and did it match the plan) and, for outdoor sessions, the weather (heat/humidity "
    "context for pace and HR).\n"
    "Cross-check these against each other: don't program a hard or heavy session when readiness is "
    "LOW / ACWR is high / recovery-time is still counting down / skin temp + RHR + respiration point "
    "to illness / the same muscles were hit in the last ~48h; match intensity to recovery and pick "
    "novelty that avoids what was just trained; tie any nutrition advice to today's load and calorie "
    "budget. After reasoning over everything, report tightly and name the 2-3 signals that actually "
    "drove the advice. For body battery always use body_battery_current (Garmin's canonical value, "
    "same as the Connect app/web); if body_battery_current_age_min is above ~15, add its as-of "
    "time (body_battery_current_as_of) and note it reflects the last watch sync so it can lag the "
    "watch face until the watch uploads - otherwise just state the number. If last_sync_age_min is "
    "large (say >60), caveat that the whole picture predates the last sync. "
    "NOTES and RECENT CONVERSATION lines are DATE-STAMPED with a relative tag (today / "
    "yesterday / N days ago): attribute food, workouts and events to the day they happened, "
    "count only TODAY's entries (and anything the user shared today) as today's, and never "
    "present an earlier day's log as if it were today. "
    "PLANNED FOOD RULE: a meal counts as planned for today ONLY if the user, in a message from "
    "today, says they still intend to eat it (today or on a named upcoming day). Food they "
    "mentioned on an earlier day was eaten that day - treat it as already consumed, never "
    "as an upcoming meal, unless they restate it today. Read what they have eaten or plan to "
    "eat from the USER's own messages and logs, not from your own earlier replies; if you are "
    "unsure whether a meal is still coming, ask instead of assuming. "
    "HOLISTIC CONTEXT: you are given the user's FULL picture every time - recovery/vitals, "
    "training load, their logged and PLANNED food, and any ACTIVE HEALTH FLAGS. Use all of "
    "it together and connect the dots; don't answer as if you only know the last message. "
    "FOOD <-> TRAINING: tie them together - shape refuel/nutrition advice around what they "
    "have already eaten today and any meal they have genuinely planned for later today (don't "
    "re-recommend protein/calories they have already had; when they have a later meal planned "
    "today, say whether it fits or should change), and factor fuelling into session "
    "recommendations. "
    "HEALTH: if ACTIVE HEALTH FLAGS lists an injury or illness, RESPECT it - do not program "
    "through it: adapt the modality to avoid the affected area, cut intensity, or recommend "
    "REST/recovery outright when they're unwell, and briefly check how it's doing. If the user "
    "REPORTS a NEW injury or illness in their message, lead with ONE caring, specific "
    "clarifying question (what exactly, where, how bad, since when, and are they up for gentle "
    "movement or need rest) and clearly restate what you've noted, BEFORE any training push. "
    "Never invent numbers - if a field is missing say so rather than guessing."
)


def _assemble(prompt_file, question=None, include_history=True,
              data=None, data_key="GARMIN_JSON", include_brief=True):
    if data is None:
        data = garmin_coach.build_snapshot()
    js = json.dumps(data, indent=2, default=str)
    parts = [
        read_file(prompt_file),
        "\n\n---\nTODAY: " + today_human() + " (" + date.today().isoformat() + ")\n",
    ]
    if USER_NAME:
        parts.append("\nUSER: the person you are coaching is named " + USER_NAME
                     + " - you may address them by their first name.\n")
    flags = active_health_text()
    if flags:
        parts.append("\nACTIVE HEALTH FLAGS (injuries/illness the user reported and has NOT "
                     "marked recovered - respect these: adapt or rest, don't train through "
                     "them, and check how they're doing):\n" + flags + "\n")
    if include_brief:
        brief = todays_brief_text()
        if brief:
            parts.append("\nTODAY'S BRIEF YOU ALREADY SENT (the recovery read + workout and "
                         "recommendations you gave the user in this morning's brief - THIS is what "
                         "they mean by 'the plan' / 'the recommendations' / 'what you told me to "
                         "do'. Use it to judge whether they followed it and to stay consistent):\n"
                         + brief + "\n")
    notes = recent_journal_text()
    if notes:
        parts.append("\nNOTES YOU'VE SHARED (durable context the user logged):\n"
                     + notes + "\n")
    if include_history:
        hist = recent_history_text()
        if hist:
            parts.append("\nRECENT CONVERSATION (context, oldest to newest):\n"
                         + hist + "\n")
    fit = load_fitness_profile()
    if isinstance(fit, dict):
        fit_view = {k: v for k, v in fit.items() if k != "generated_at"}
        if fit_view:
            parts.append("\nFITNESS_PROFILE (slow-changing performance metrics "
                         "Garmin computes; times are h:mm:ss):\n"
                         + json.dumps(fit_view, indent=2, default=str) + "\n")
    parts.append("\nPROFILE:\n" + read_file(PROFILE_FILE))
    parts.append("\n\n" + data_key + ":\n" + js)
    parts.append("\n\n" + DATA_USE_DIRECTIVE)
    if question is not None:
        parts.append("\n\nQUESTION:\n" + question)
    return "".join(parts), data


def generate_summary():
    prompt, snap = _assemble(SUMMARY_PROMPT_FILE, include_history=True, include_brief=False)
    if isinstance(snap, dict) and "__error__" in snap:
        return None, snap
    text = run_llm(prompt)
    if text:
        append_history("user", "GMS (requested the morning brief)")
        append_history("agbot", text)
        save_todays_brief(text)
    return text, snap


def generate_qa(question):
    prompt, snap = _assemble(QA_PROMPT_FILE, question=question, include_history=True)
    if isinstance(snap, dict) and "__error__" in snap:
        return None, snap
    text = run_llm(prompt)
    if text:
        append_history("user", question)
        append_history("agbot", text)
    return text, snap


def _journal_shared_media(caption, analysis, n, media_label):
    """Record an EXPLICITLY LOGGED meal/note in the DATED journal. Only fires when
    the caption starts with 'log:' / 'log ' - i.e. the user is telling me they ATE (or did)
    something, not just asking about it. Photos they only ask about ('should I eat
    this?', 'what do you suggest?') are NOT journaled, so we never over-count food they
    only browsed. Non-logged shares still live in the recent-chat window for context."""
    cap = (caption or "").strip()
    low = cap.lower()
    if not (low.startswith("log:") or low.startswith("log ")):
        return  # not an explicit log - don't record as consumed
    note = cap[3:].lstrip(" :").strip()  # caption minus the 'log' prefix
    body = (analysis or "").strip()
    kept = [ln for ln in body.split("\n") if ln.strip()]
    if kept and kept[0].lstrip().startswith("\U0001F916"):  # drop the AgBot signature line
        kept = kept[1:]
    summary = " ".join(kept).strip()
    if len(summary) > 300:
        summary = summary[:300].rstrip() + "..."
    media = "video" if "video" in media_label else ("photos" if n > 1 else "photo")
    parts = ["logged"]
    if note:
        parts.append(note)
    parts.append("(via " + media + "):")
    if summary:
        parts.append(summary)
    append_journal(" ".join(parts).strip())


def generate_images(image_paths, caption, extra=None, media_label="photo"):
    n = len(image_paths)
    if n > 1:
        q = caption or ("(no caption - these items were sent together; analyze them AS "
                        "A SET and coach me on them)")
    else:
        q = caption or "(no caption - analyze this image and coach me on it)"
    prompt, snap = _assemble(IMAGE_PROMPT_FILE, question=q, include_history=True)
    if n > 1:
        prompt += ("\n\nATTACHMENTS: " + str(n) + " images are attached and belong to a "
                   "SINGLE request. Analyze them TOGETHER as one set and give ONE combined "
                   "answer - do not describe each image separately.")
    if extra:
        prompt += "\n\n" + extra
    text = run_llm(prompt, images=image_paths)
    if text:
        plural = "s" if n != 1 else ""
        label = caption or ("[shared " + str(n) + " " + media_label + plural + "]")
        append_history("user", label + " [" + media_label + plural + "]")
        append_history("agbot", text)
        _journal_shared_media(caption, text, n, media_label)
    return text, snap


def generate_image(image_path, caption):
    return generate_images([image_path], caption)


def generate_weekly():
    data = garmin_coach.build_weekly()
    if isinstance(data, dict) and "__error__" in data:
        return None, data
    prompt, _ = _assemble(WEEKLY_PROMPT_FILE, data=data, data_key="WEEKLY_JSON")
    text = run_llm(prompt)
    if text:
        append_history("user", "weekly review (requested)")
        append_history("agbot", text)
    return text, data


def generate_nutrition():
    prompt, snap = _assemble(NUTRITION_PROMPT_FILE)
    if isinstance(snap, dict) and "__error__" in snap:
        return None, snap
    text = run_llm(prompt)
    if text:
        append_history("user", "nutrition targets (requested)")
        append_history("agbot", text)
    return text, snap


def generate_debrief(activity):
    snap = garmin_coach.build_snapshot()
    parts = [
        read_file(DEBRIEF_PROMPT_FILE),
        "\n\n---\nTODAY: " + today_human() + "\n",
    ]
    flags = active_health_text()
    if flags:
        parts.append("\nACTIVE HEALTH FLAGS (injuries/illness to respect - adapt or rest, "
                     "don't train through them):\n" + flags + "\n")
    brief = todays_brief_text()
    if brief:
        parts.append("\nTODAY'S BRIEF YOU ALREADY SENT (the workout and recommendations you "
                     "gave the user this morning - THIS is 'the plan' they were asked to follow; "
                     "compare what they actually just did against it):\n" + brief + "\n")
    notes = recent_journal_text()
    if notes:
        parts.append("\nNOTES YOU'VE SHARED (durable, date-stamped - includes the meals "
                     "the user logged TODAY = their food intake so far):\n" + notes + "\n")
    hist = recent_history_text()
    if hist:
        parts.append("\nRECENT CONVERSATION (date-stamped today / yesterday / N days ago - "
                     "watch for a meal they said TODAY they still plan to eat later today, and "
                     "how they fuelled/felt around the session; a meal they mentioned on an "
                     "earlier day was already eaten, don't treat it as upcoming; oldest to "
                     "newest):\n" + hist + "\n")
    parts.append("\nPROFILE:\n" + read_file(PROFILE_FILE))
    atype = str((activity or {}).get("type") or "")
    if "strength" in atype:
        sets = garmin_coach.exercise_sets((activity or {}).get("activity_id"))
        if sets:
            parts.append("\n\nLOGGED_SETS (per exercise: #sets, rep range, top weight kg):\n"
                         + json.dumps(sets, indent=2, default=str))
    parts.append("\n\nJUST_FINISHED_ACTIVITY:\n"
                 + json.dumps(activity, indent=2, default=str))
    extras = garmin_coach.activity_extras((activity or {}).get("activity_id"))
    if isinstance(extras, dict) and extras and "__error__" not in extras:
        parts.append("\n\nACTIVITY_EXTRAS (time-in-HR-zone in minutes; weather if outdoor):\n"
                     + json.dumps(extras, indent=2, default=str))
    parts.append("\n\nGARMIN_JSON:\n"
                 + json.dumps(snap, indent=2, default=str))
    parts.append("\n\n" + DATA_USE_DIRECTIVE)
    text = run_llm("".join(parts))
    if text:
        append_history("agbot", "[post-workout debrief] " + text)
    return text


def do_summary(chat_id, auto=False):
    log.info("Generating summary (auto=%s) for %s", auto, chat_id)
    if auto:
        send_message(chat_id, "\U0001F916 AgBot: pulling your overnight Garmin data for today's brief...")
    text, snap = generate_summary()
    if isinstance(snap, dict) and "__error__" in snap:
        send_message(chat_id, "\U0001F916 AgBot: I couldn't read your Garmin data right now ("
                     + str(snap["__error__"])[:180] + "). I'll try again later.")
        return
    if not text:
        send_message(chat_id, "\U0001F916 AgBot: I hit a problem generating your brief. Try again in a minute.")
        return
    send_message(chat_id, text)
    write_file(LAST_SUMMARY_FILE, date.today().isoformat())


def do_qa(chat_id, question):
    log.info("Q&A: %s", question[:120])
    text, snap = generate_qa(question)
    if isinstance(snap, dict) and "__error__" in snap:
        send_message(chat_id, "\U0001F916 AgBot: I couldn't read your Garmin data right now. Try again shortly.")
        return
    if not text:
        send_message(chat_id, "\U0001F916 AgBot: Sorry, I couldn't generate an answer just now. Try again in a minute.")
        return
    send_message(chat_id, text)


def do_weekly(chat_id):
    log.info("Generating weekly review for %s", chat_id)
    text, data = generate_weekly()
    if isinstance(data, dict) and "__error__" in data:
        send_message(chat_id, "\U0001F916 AgBot: couldn't pull your weekly data right now. Try again shortly.")
        return
    if not text:
        send_message(chat_id, "\U0001F916 AgBot: I couldn't build your weekly review just now. Try again in a minute.")
        return
    send_message(chat_id, text)


def do_nutrition(chat_id):
    log.info("Generating nutrition targets for %s", chat_id)
    text, snap = generate_nutrition()
    if isinstance(snap, dict) and "__error__" in snap:
        send_message(chat_id, "\U0001F916 AgBot: couldn't read your Garmin data for nutrition targets. Try again shortly.")
        return
    if not text:
        send_message(chat_id, "\U0001F916 AgBot: I couldn't work out your targets just now. Try again in a minute.")
        return
    send_message(chat_id, text)


def generate_performance():
    prompt, snap = _assemble(PERF_PROMPT_FILE)
    if isinstance(snap, dict) and "__error__" in snap:
        return None, snap
    text = run_llm(prompt)
    if text:
        append_history("user", "performance / fitness stats (requested)")
        append_history("agbot", text)
    return text, snap


def do_performance(chat_id):
    log.info("Generating performance card for %s", chat_id)
    text, snap = generate_performance()
    if isinstance(snap, dict) and "__error__" in snap:
        send_message(chat_id, "\U0001F916 AgBot: couldn't read your Garmin data right now. Try again shortly.")
        return
    if not text:
        send_message(chat_id, "\U0001F916 AgBot: I couldn't build your performance card just now. Try again in a minute.")
        return
    send_message(chat_id, text)


def _photo_file_id(msg):
    photos = msg.get("photo") or []
    if photos:
        return photos[-1].get("file_id")  # largest rendition
    doc = msg.get("document") or {}
    if str(doc.get("mime_type", "")).startswith("image/"):
        return doc.get("file_id")
    return None


def _video_file(msg):
    for key in ("video", "video_note", "animation"):
        v = msg.get(key)
        if v and v.get("file_id"):
            return v
    doc = msg.get("document") or {}
    if str(doc.get("mime_type", "")).startswith("video/"):
        return doc
    return None


def do_images(chat_id, file_ids, caption, extra=None, media_label="photo"):
    """Download 1+ images (an album is analyzed together) and coach on them."""
    file_ids = [f for f in (file_ids or []) if f]
    if not file_ids:
        send_message(chat_id, "\U0001F916 AgBot: send it as an image (photo) and I'll take a look.")
        return
    n = len(file_ids)
    if media_label == "photo":
        send_message(chat_id, "\U0001F916 AgBot: looking at your %d photos together..." % n
                     if n > 1 else "\U0001F916 AgBot: looking at your photo...")
    paths = []
    try:
        for fid in file_ids:
            p = download_file(fid)
            if p:
                paths.append(p)
        if not paths:
            send_message(chat_id, "\U0001F916 AgBot: I couldn't download that image. Please try again.")
            return
        text, _snap = generate_images(paths, caption, extra=extra, media_label=media_label)
        if not text:
            send_message(chat_id, "\U0001F916 AgBot: I couldn't analyze %s just now. Try again in a minute."
                         % ("those" if n > 1 else "that photo"))
            return
        send_message(chat_id, text)
    finally:
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass


def do_image(chat_id, msg):
    do_images(chat_id, [_photo_file_id(msg)], (msg.get("caption") or "").strip())


def do_video(chat_id, msg):
    """Analyze a short video by sampling frames + transcribing any spoken audio."""
    v = _video_file(msg)
    if not v or not v.get("file_id"):
        send_message(chat_id, "\U0001F916 AgBot: I couldn't read that video.")
        return
    send_message(chat_id, "\U0001F3A5 AgBot: reviewing your video...")
    vpath, frames = None, []
    try:
        try:
            vpath = download_file(v.get("file_id"))
        except Exception as exc:  # noqa: BLE001
            log.error("video download failed: %s", exc)
            vpath = None
        if not vpath:
            send_message(chat_id, "\U0001F916 AgBot: I couldn't download that video - Telegram "
                                  "limits bots to ~20MB. Try a shorter or lower-res clip.")
            return
        frames = extract_video_frames(vpath)
        if not frames:
            send_message(chat_id, "\U0001F916 AgBot: I couldn't read any frames from that video. "
                                  "Try a shorter clip, or send a photo instead.")
            return
        transcript = transcribe_audio(vpath)  # spoken words over the clip, if any
        dur = v.get("duration")
        bits = ["VIDEO_CONTEXT: the user sent a short video. I sampled " + str(len(frames))
                + " still frames from it" + ((" over ~" + str(dur) + "s") if dur else "")
                + ", attached in time order - read them as a sequence (movement / form / a "
                  "pan across items over the clip), not as unrelated images."]
        if transcript:
            bits.append('Audio transcript of the video (what the user says): "' + transcript + '"')
        text, _snap = generate_images(frames, (msg.get("caption") or "").strip(),
                                      extra="\n".join(bits), media_label="video frame")
        if not text:
            send_message(chat_id, "\U0001F916 AgBot: I couldn't analyze that video just now. Try again in a minute.")
            return
        send_message(chat_id, text)
    finally:
        for p in frames:
            try:
                os.remove(p)
            except OSError:
                pass
        if vpath:
            try:
                os.remove(vpath)
            except OSError:
                pass


def do_voice(chat_id, msg):
    v = msg.get("voice") or msg.get("audio") or {}
    file_id = v.get("file_id")
    if not file_id:
        send_message(chat_id, "\U0001F916 AgBot: I couldn't read that audio clip.")
        return
    send_message(chat_id, "\U0001F3A4 AgBot: transcribing your voice note...")
    path = None
    try:
        path = download_file(file_id)
        transcript = transcribe_audio(path) if path else None
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
    if not path:
        send_message(chat_id, "\U0001F916 AgBot: couldn't download that audio. Please try again.")
        return
    if not transcript:
        send_message(chat_id, "\U0001F916 AgBot: I couldn't make out any speech there. "
                              "Try again a bit closer to the mic, or just type it.")
        return
    caption = (msg.get("caption") or "").strip()
    text = (caption + " " + transcript).strip() if caption else transcript
    send_message(chat_id, "\U0001F3A4 Heard: \u201c" + transcript + "\u201d")
    _route_text(chat_id, text)


# --------------------------------------------------------------------- routing
HELP_TEXT = (
    "\U0001F916 **AgBot** - your personal Garmin coach.\n\n"
    "- **GMS** (or 'summary') - your morning brief: recovery read + today's workout.\n"
    "- **week** - a 7-day trend review.  **nutrition** - today's calorie/protein targets.\n"
    "- **performance** - your fitness stats: VO2max, fitness age, race predictions, "
    "endurance & hill score, cycling FTP, weekly intensity minutes.\n"
    "- **Send a photo** (meal, machine screen, exercise) and I'll analyze it - add a "
    "caption to ask something specific.\n"
    "- **Send several photos together** (an album) - e.g. all the breakfast options - and "
    "I'll weigh them up as ONE set and recommend what to pick.\n"
    "- \U0001F3A5 **Send a short video** (form check, a machine, a pan across the buffet) - "
    "I sample frames + listen to any narration and coach on the whole clip.\n"
    "- \U0001F3A4 **Send a voice note** and I'll transcribe it, then answer - speak naturally.\n"
    "- **log:** note a meal or how you feel - e.g. 'log: ate 3 eggs, knee felt fine' - "
    "or just add **log:** as the caption on a food photo and I'll count it. I remember it across days.\n"
    "- \U0001FA79 **Tell me if you're hurt or unwell** ('my knee hurts', 'I feel sick') and "
    "I'll ask what's going on, remember it, and adapt your training (or call for rest) until "
    "you reply **recovered**.\n"
    "- \U0001F37D\uFE0F I nudge you to log meals at 8:30am, 12:15pm & 9pm - just reply "
    "'log: <what you ate>' and it feeds your nutrition coaching.\n"
    "- **Ask me anything**, e.g. 'how did I sleep?', 'what's my predicted 10K time?', "
    "'give me a 30-min rowing session'.\n"
    "- I auto-send your brief by ~9:30am, debrief you after a workout (with your logged "
    "sets/reps for strength), and warn you on a rough morning.\n"
    "- **/reset** clears our recent-chat memory.\n\n"
    "Everything is based on your live Garmin data and your gym equipment."
)

SUMMARY_TRIGGERS = {"gms", "summary", "/summary", "/gms", "morning", "brief",
                    "report", "morning summary", "/report", "/brief"}


def classify(text):
    t = (text or "").strip()
    low = t.lower()
    for pre in ("agbot:", "agbot"):
        if low.startswith(pre):
            t = t[len(pre):].strip(" :")
            low = t.lower()
            break
    if low in ("/start", "start"):
        return "start", ""
    if low in ("/help", "help", "?"):
        return "help", ""
    if low in ("/reset", "/clear", "reset", "forget", "new chat", "start over"):
        return "reset", ""
    if low in ("recovered", "resolved", "all better", "i'm fine now", "im fine now",
               "healed", "back to normal", "all good now", "feeling better now",
               "i'm better now", "im better now", "fully recovered"):
        return "recovered", ""
    if low.startswith("log ") or low.startswith("log:"):
        return "log", t[3:].lstrip(" :").strip()
    if low in ("week", "weekly", "/week", "/weekly", "week review",
               "weekly review", "review"):
        return "weekly", ""
    if low in ("nutrition", "/nutrition", "macros", "calories", "diet",
               "food targets", "targets"):
        return "nutrition", ""
    if low in ("performance", "/performance", "fitness", "stats", "/stats",
               "vo2", "vo2max", "race", "races", "race predictions",
               "fitness age", "endurance", "performance stats", "perf"):
        return "performance", ""
    if (low == "" or low in SUMMARY_TRIGGERS or low.startswith("gms")
            or ("morning" in low and "summ" in low)):
        return "summary", ""
    return "qa", t


def _owner_ok(chat_id):
    own = owner()
    if own is None:
        set_owner(chat_id)
        own = str(chat_id)
    if str(chat_id) != str(own):
        log.warning("Ignoring message from non-owner chat_id %s", chat_id)
        return False
    return True


def _route_text(chat_id, text):
    kind, payload = classify(text)
    log.info("Message kind=%s text=%r", kind, text[:120])
    _maybe_capture_health(kind, text, payload)
    if kind in ("start", "help"):
        send_message(chat_id, HELP_TEXT)
    elif kind == "reset":
        clear_history()
        send_message(chat_id, "\U0001F916 AgBot: conversation memory cleared - fresh start.")
    elif kind == "recovered":
        cleared = resolve_health()
        if cleared:
            send_message(chat_id, "\U0001F916 AgBot: great news \u2713 cleared your active "
                         "health flag(s): " + "; ".join(c[:60] for c in cleared)
                         + ". Back to normal programming - shout if anything still bothers you.")
        else:
            send_message(chat_id, "\U0001F916 AgBot: noted - you had no active injury/illness "
                         "flags on record. Glad you're feeling good!")
    elif kind == "log":
        if payload:
            append_journal(payload)
            send_message(chat_id, "\U0001F916 AgBot: logged \u2713 - I'll factor that into your coaching.")
        else:
            send_message(chat_id, "\U0001F916 AgBot: tell me what to log, e.g. 'log: ate 3 eggs and oats'.")
    elif kind == "weekly":
        send_message(chat_id, "\U0001F916 AgBot: crunching your last 7 days...")
        do_weekly(chat_id)
    elif kind == "nutrition":
        send_message(chat_id, "\U0001F916 AgBot: working out today's targets...")
        do_nutrition(chat_id)
    elif kind == "performance":
        send_message(chat_id, "\U0001F916 AgBot: pulling your performance metrics...")
        do_performance(chat_id)
    elif kind == "summary":
        send_message(chat_id, "\U0001F916 AgBot: on it - pulling your Garmin data...")
        do_summary(chat_id)
    else:
        send_message(chat_id, "\U0001F916 AgBot: let me check your data...")
        do_qa(chat_id, payload)


# Album (media-group) buffering: Telegram delivers each photo of an album as a
# SEPARATE update sharing one media_group_id, and only one carries the caption. We
# collect them briefly and analyze the whole set in a single reply.
ALBUM_DEBOUNCE_SEC = 1.6
_album_buffer = {}  # media_group_id -> {chat_id, file_ids, caption, ts}


def _buffer_album(chat_id, gid, file_id, caption):
    grp = _album_buffer.get(gid)
    if grp is None:
        grp = {"chat_id": chat_id, "file_ids": [], "caption": "", "ts": 0.0}
        _album_buffer[gid] = grp
    if file_id:
        grp["file_ids"].append(file_id)
    if caption and not grp["caption"]:
        grp["caption"] = caption
    grp["ts"] = time.time()


def flush_ready_albums():
    """Process any album whose photos have stopped arriving (idle > debounce)."""
    if not _album_buffer:
        return
    now = time.time()
    ready = [gid for gid, g in _album_buffer.items()
             if now - g["ts"] >= ALBUM_DEBOUNCE_SEC]
    for gid in ready:
        grp = _album_buffer.pop(gid, None)
        if not grp or not grp["file_ids"]:
            continue
        log.info("Album %s complete: %d photos", gid, len(grp["file_ids"]))
        try:
            do_images(grp["chat_id"], grp["file_ids"], grp["caption"])
        except Exception as exc:  # noqa: BLE001
            log.exception("album handler error: %s", exc)


def dispatch(msg):
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    if chat_id is None:
        return
    if not _owner_ok(chat_id):
        return
    gid = msg.get("media_group_id")
    photo_fid = _photo_file_id(msg)
    if gid and photo_fid:  # one item of a photo album - buffer, handle as a set
        _buffer_album(chat_id, gid, photo_fid, (msg.get("caption") or "").strip())
        return
    if _video_file(msg):
        log.info("Message kind=video")
        do_video(chat_id, msg)
        return
    if photo_fid:
        log.info("Message kind=image caption=%r", (msg.get("caption") or "")[:120])
        do_image(chat_id, msg)
        return
    if msg.get("voice") or msg.get("audio"):
        log.info("Message kind=voice")
        do_voice(chat_id, msg)
        return
    text = msg.get("text", "")
    _route_text(chat_id, text)


def maybe_auto_summary():
    own = owner()
    if not own:
        return
    if read_file(LAST_SUMMARY_FILE).strip() == date.today().isoformat():
        return
    now = datetime.now()
    cur = (now.hour, now.minute)
    if cur < AUTO_START or cur >= AUTO_END:
        return
    log.info("Auto-summary trigger at %s", now.strftime("%H:%M"))
    do_summary(own, auto=True)


def maybe_meal_reminders():
    own = owner()
    if not own:
        return
    now = datetime.now()
    today = date.today().isoformat()
    try:
        sent = json.loads(read_file(MEAL_STATE_FILE) or "{}")
    except Exception:  # noqa: BLE001
        sent = {}
    if not isinstance(sent, dict):
        sent = {}
    changed = False
    for slot, hh, mm, text in MEAL_REMINDERS:
        if sent.get(slot) == today:
            continue
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < target:
            continue
        late_min = (now - target).total_seconds() / 60
        if late_min <= MEAL_CATCHUP_MIN:
            log.info("Meal reminder: %s at %s", slot, now.strftime("%H:%M"))
            send_message(own, text)
        else:
            log.info("Meal reminder %s missed window (%.0f min late) - skipping",
                     slot, late_min)
        sent[slot] = today  # mark handled either way so it won't fire again today
        changed = True
    if changed:
        write_file(MEAL_STATE_FILE, json.dumps(sent))


_last_activity_check = 0.0


def maybe_post_workout():
    global _last_activity_check
    own = owner()
    if not own:
        return
    now = time.time()
    if now - _last_activity_check < ACTIVITY_CHECK_SECS:
        return
    _last_activity_check = now
    act = garmin_coach.latest_activity()
    if not isinstance(act, dict) or "__error__" in act:
        return
    aid = str(act.get("activity_id") or "")
    if not aid:
        return
    seen = read_file(LAST_ACTIVITY_FILE).strip()
    if aid == seen:
        return
    write_file(LAST_ACTIVITY_FILE, aid)
    if not seen:
        return  # first run - set the baseline, don't debrief historical activities
    log.info("New activity %s (%s) - sending debrief", aid, act.get("type"))
    text = generate_debrief(act)
    if text:
        send_message(own, text)


def evaluate_red_flags(snap):
    flags = []
    r = snap.get("training_readiness") or {}
    if str(r.get("level", "")).upper() in ("LOW", "VERY_LOW"):
        flags.append("readiness LOW")
    sl = snap.get("last_night_sleep") or {}
    secs = sl.get("time_asleep_s")
    if isinstance(secs, (int, float)) and secs < 5.5 * 3600:
        flags.append("short sleep")
    hv = snap.get("hrv") or {}
    last = hv.get("last_night_avg")
    base_low = hv.get("baseline_balanced_low")
    if (isinstance(last, (int, float)) and isinstance(base_low, (int, float))
            and last < base_low):
        flags.append("HRV below baseline")
    return flags


def maybe_red_flags():
    own = owner()
    if not own:
        return
    today = date.today().isoformat()
    if read_file(RED_FLAGS_FILE).strip() == today:
        return
    if read_file(LAST_SUMMARY_FILE).strip() == today:
        write_file(RED_FLAGS_FILE, today)  # the brief already covers this ground
        return
    now = datetime.now()
    cur = (now.hour, now.minute)
    if cur < (5, 30) or cur >= AUTO_START:
        return
    snap = garmin_coach.build_snapshot()
    if isinstance(snap, dict) and "__error__" in snap:
        return
    flags = evaluate_red_flags(snap)
    write_file(RED_FLAGS_FILE, today)
    if not flags:
        return
    log.info("Red flags this morning: %s", flags)
    prompt, _ = _assemble(SUMMARY_PROMPT_FILE, data=snap)
    prompt += ("\n\nURGENT_CONTEXT: It is early morning and these red flags fired: "
               + ", ".join(flags) + ". Instead of a full brief write a SHORT (< 90 "
               "words) early heads-up: name the concern(s) plainly and give ONE "
               "adjustment for today (dial back intensity / prioritise recovery). "
               "First line: \U0001F916 AgBot \u26A0\uFE0F Heads-up \u00B7 " + today_human() + ".")
    text = run_llm(prompt)
    if text:
        append_history("agbot", "[early red-flag alert] " + text)
        send_message(own, text)


def acquire_singleton_lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLETON_PORT))
        s.listen(1)
    except OSError:
        log.error("Another AgBot instance is already running. Exiting.")
        sys.exit(0)
    return s  # keep referenced for process lifetime


def main():
    _lock = acquire_singleton_lock()  # noqa: F841
    me = tg("getMe")
    log.info("AgBot online as @%s", me.get("result", {}).get("username"))
    offset = get_offset()
    while True:
        try:
            pt = 1 if _album_buffer else POLL_TIMEOUT
            params = {"timeout": pt}
            if offset is not None:
                params["offset"] = offset
            resp = tg("getUpdates", params, timeout=pt + 15)
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                set_offset(offset)
                msg = upd.get("message") or upd.get("edited_message")
                if msg:
                    try:
                        dispatch(msg)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("dispatch error: %s", exc)
            flush_ready_albums()
            maybe_auto_summary()
            maybe_red_flags()
            maybe_post_workout()
            maybe_meal_reminders()
        except Exception as exc:  # noqa: BLE001
            log.error("poll loop error: %s", exc)
            time.sleep(5)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if "--selftest-summary" in sys.argv:
        text, _snap = generate_summary()
        print("---- OUTPUT ----")
        print(text)
        sys.exit(0)
    if "--selftest-voice" in sys.argv:
        idx = sys.argv.index("--selftest-voice")
        audio = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        print("---- OUTPUT ----")
        print("transcript:", transcribe_audio(audio) if audio else "(pass an audio path)")
        sys.exit(0)
    if "--selftest-qa" in sys.argv:
        q = sys.argv[-1]
        text, _snap = generate_qa(q)
        print("---- OUTPUT ----")
        print(text)
        sys.exit(0)
    if "--selftest-weekly" in sys.argv:
        text, _ = generate_weekly()
        print("---- OUTPUT ----")
        print(text)
        sys.exit(0)
    if "--selftest-nutrition" in sys.argv:
        text, _ = generate_nutrition()
        print("---- OUTPUT ----")
        print(text)
        sys.exit(0)
    if "--selftest-performance" in sys.argv:
        text, _ = generate_performance()
        print("---- OUTPUT ----")
        print(text)
        sys.exit(0)
    if "--selftest-debrief" in sys.argv:
        act = garmin_coach.latest_activity()
        print("ACTIVITY:", json.dumps(act, default=str)[:300])
        text = (generate_debrief(act)
                if isinstance(act, dict) and "__error__" not in act else None)
        print("---- OUTPUT ----")
        print(text)
        sys.exit(0)
    if "--selftest-image" in sys.argv:
        idx = sys.argv.index("--selftest-image")
        img = sys.argv[idx + 1]
        cap = sys.argv[idx + 2] if len(sys.argv) > idx + 2 else ""
        text, _ = generate_image(img, cap)
        print("---- OUTPUT ----")
        print(text)
        sys.exit(0)
    main()
