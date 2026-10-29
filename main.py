import os
import json
import hmac
import time
import shutil
import asyncio
import hashlib
import tempfile
import subprocess
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

app = FastAPI(title="Slack Video-to-GIF Bot (Serverless & Free-Tier Protected)")

# Required Secrets (Injected via environment variables — NEVER hardcode!)
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "").strip()

# Optional Configuration
ADMIN_SLACK_USER_ID = os.environ.get("ADMIN_SLACK_USER_ID", "").strip()
ALLOWED_SLACK_TEAM_ID = os.environ.get("ALLOWED_SLACK_TEAM_ID", "").strip()
SELF_BASE_URL = os.environ.get("SELF_BASE_URL", "").strip()

# Trigger emojis (without colons):
#   - Standard GIF triggers: 🎞️ (:film_frames:), 🎬 (:clapper:), ⚡ (:zap:), :gif:
#   - Clip-first triggers:   ✂️ (:scissors:)
CONVERT_EMOJIS = {
    e.strip()
    for e in os.environ.get("TRIGGER_EMOJIS", "film_frames,gif,clapper,zap").split(",")
    if e.strip()
}
CLIP_FIRST_EMOJIS = {
    e.strip()
    for e in os.environ.get("CLIP_EMOJIS", "scissors,black_nib").split(",")
    if e.strip()
}
ALL_TRIGGER_EMOJIS = CONVERT_EMOJIS | CLIP_FIRST_EMOJIS

# Smart Hybrid Threshold: Videos longer than this (in seconds) automatically prompt
# the user to select a clip & size first instead of encoding the entire long video.
AUTO_CLIP_PROMPT_SECONDS = float(os.environ.get("AUTO_CLIP_PROMPT_SECONDS", "10.0"))

VIDEO_EXTENSIONS = {"mp4", "mov", "webm", "m4v"}
VIDEO_MIMETYPES = {"video/mp4", "video/quicktime", "video/webm", "video/x-m4v"}

# ==============================================================================
# HARD FREE-TIER CIRCUIT BREAKER & WARNING THRESHOLDS ($0.00 GUARANTEE)
# ==============================================================================
MAX_GIF_BYTES = int(float(os.environ.get("MAX_GIF_MB", "6.0")) * 1024 * 1024)
MONTHLY_HARD_CAP_MB = float(os.environ.get("MONTHLY_HARD_CAP_MB", "700.0"))
MONTHLY_WARN_CAP_MB = MONTHLY_HARD_CAP_MB * 0.80
MONTHLY_HARD_CAP_COUNT = int(os.environ.get("MONTHLY_HARD_CAP_COUNT", "140"))
MONTHLY_WARN_CAP_COUNT = int(MONTHLY_HARD_CAP_COUNT * 0.80)

_PROCESSED_LOCK = threading.Lock()
_PROCESSED_KEYS: set[str] = set()
_BOT_USER_ID: str | None = None


# ==============================================================================
# SECURITY & CRYPTOGRAPHIC VERIFICATION
# ==============================================================================
def verify_slack_signature(raw_body: bytes, timestamp: str, signature: str) -> bool:
    """
    Fail-closed HMAC-SHA256 verification of Slack webhook requests with a 5-minute
    anti-replay window. Rejects all requests if SLACK_SIGNING_SECRET is unset.
    """
    if not SLACK_SIGNING_SECRET or not timestamp or not signature:
        return False
    try:
        req_ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - req_ts) > 60 * 5:
        return False

    basestring = f"v0:{timestamp}:{raw_body.decode('utf-8', errors='replace')}".encode("utf-8")
    expected_sig = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"), basestring, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_sig, signature)


def make_internal_token(payload_bytes: bytes) -> str:
    if not SLACK_SIGNING_SECRET:
        raise RuntimeError("SLACK_SIGNING_SECRET is required")
    return hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"), payload_bytes, hashlib.sha256
    ).hexdigest()


def is_valid_slack_download_url(url: str) -> bool:
    """
    SSRF & Credential Leak Prevention:
    Ensures the bot NEVER sends its `Authorization: Bearer xoxb-...` header to any
    domain other than official `*.slack.com` / `*.slack-edge.com` HTTPS endpoints.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https":
            return False
        host = (parsed.hostname or "").lower()
        return host == "slack.com" or host.endswith(".slack.com") or host.endswith(".slack-edge.com")
    except Exception:
        return False


def resolve_self_base_url(request: Request) -> str:
    if SELF_BASE_URL:
        return SELF_BASE_URL.rstrip("/")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    if host:
        return f"{scheme}://{host}".rstrip("/")
    return ""


def dispatch_async_render_job(job: dict, base_url: str):
    """
    Fires a signed POST request to `/internal/render` on the service's own URL in a
    background daemon thread so `/slack/interactions` returns HTTP 200 to Slack in
    <100ms while keeping Cloud Run CPU active for the duration of the ffmpeg render.
    """
    body = json.dumps(job).encode("utf-8")
    sig = make_internal_token(body)

    def _send():
        if base_url:
            try:
                req = urllib.request.Request(
                    f"{base_url}/internal/render",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Internal-Signature": sig,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=55) as resp:
                    resp.read()
                return
            except Exception:
                pass
        execute_render_job(job)

    t = threading.Thread(target=_send, daemon=True)
    t.start()


# ==============================================================================
# SLACK QUOTA & HELPERS
# ==============================================================================
def get_bot_user_id(client: WebClient) -> str | None:
    global _BOT_USER_ID
    if _BOT_USER_ID:
        return _BOT_USER_ID
    try:
        auth = client.auth_test()
        _BOT_USER_ID = auth.get("user_id")
        return _BOT_USER_ID
    except Exception:
        return None


def check_monthly_free_tier_quota(client: WebClient) -> tuple[bool, bool, float, int]:
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, 0, 0, 0, tzinfo=timezone.utc)
    ts_from = str(int(month_start.timestamp()))

    bot_uid = get_bot_user_id(client)
    total_bytes = 0
    total_count = 0

    try:
        page = 1
        while page <= 5:
            kwargs = {"ts_from": ts_from, "count": 100, "page": page}
            if bot_uid:
                kwargs["user"] = bot_uid
            res = client.files_list(**kwargs)
            files = res.get("files", [])
            for f in files:
                total_bytes += int(f.get("size", 0))
                total_count += 1
            paging = res.get("paging", {})
            if page >= paging.get("pages", 1):
                break
            page += 1
    except Exception:
        pass

    used_mb = total_bytes / (1024 * 1024)
    is_blocked = (used_mb >= MONTHLY_HARD_CAP_MB) or (total_count >= MONTHLY_HARD_CAP_COUNT)
    is_warning = (not is_blocked) and (
        (used_mb >= MONTHLY_WARN_CAP_MB) or (total_count >= MONTHLY_WARN_CAP_COUNT)
    )
    return is_blocked, is_warning, used_mb, total_count


def notify_admin_dm(client: WebClient, text: str):
    if not ADMIN_SLACK_USER_ID:
        return
    try:
        client.chat_postMessage(channel=ADMIN_SLACK_USER_ID, text=text)
    except Exception:
        pass


# ==============================================================================
# FFMPEG VIDEO INSPECTION & 2-PASS ADAPTIVE GIF CONVERSION
# ==============================================================================
def round_even(val: float) -> int:
    return max(64, int(round(val / 2.0) * 2))


def compute_base_gif_dimensions(orig_w: int, orig_h: int, max_cap: int = 720) -> tuple[int, int]:
    """
    Computes the 100% full-optimized GIF dimensions (capped at 720p on the longest edge)
    while strictly preserving the video's native aspect ratio.
    """
    if orig_w <= 0 or orig_h <= 0:
        return 540, 540
    longest = max(orig_w, orig_h)
    if longest <= max_cap:
        return round_even(orig_w), round_even(orig_h)
    scale = max_cap / float(longest)
    return round_even(orig_w * scale), round_even(orig_h * scale)


def probe_media_info(path_or_url: str, is_remote_slack: bool = False) -> tuple[int, int, float]:
    """
    Uses ffprobe to inspect (width, height, duration_seconds).
    Can probe remote Slack URLs directly via HTTP range requests in <0.5s without
    downloading the full video file. Accounts for display rotation metadata.
    """
    try:
        cmd = ["ffprobe", "-v", "error"]
        if is_remote_slack and SLACK_BOT_TOKEN:
            cmd += ["-headers", f"Authorization: Bearer {SLACK_BOT_TOKEN}\r\n"]
        cmd += [
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:stream_tags=rotate:stream_side_data=rotation:format=duration",
            "-of", "json",
            path_or_url,
        ]
        out = subprocess.check_output(cmd, text=True, timeout=8)
        data = json.loads(out)
        streams = data.get("streams", [{}])
        st = streams[0] if streams else {}
        w = int(st.get("width", 720))
        h = int(st.get("height", 720))

        # Check rotation metadata
        rot = 0
        tags = st.get("tags", {})
        if "rotate" in tags:
            rot = int(float(tags["rotate"]))
        for sd in st.get("side_data_list", []):
            if "rotation" in sd:
                rot = int(float(sd["rotation"]))
        if abs(rot) in (90, 270):
            w, h = h, w

        dur_str = (data.get("format") or {}).get("duration") or st.get("duration") or "10.0"
        dur = max(0.5, float(dur_str))
        return w, h, dur
    except Exception:
        return 720, 720, 10.0


def get_video_duration(video_path: str) -> float:
    _, _, dur = probe_media_info(video_path, is_remote_slack=False)
    return dur


def get_media_dimensions(media_path: str) -> tuple[int, int]:
    w, h, _ = probe_media_info(media_path, is_remote_slack=False)
    return w, h


def compute_proportional_scales(base_w: int, base_h: int) -> list[dict]:
    """
    Returns 3 smaller dimension presets (75%, 50%, 33%) keeping the exact aspect ratio.
    """
    presets = []
    for pct, ratio in [(75, 0.75), (50, 0.50), (33, 0.3333)]:
        w = round_even(base_w * ratio)
        h = round_even(base_h * ratio)
        max_d = max(w, h)
        presets.append({"pct": pct, "w": w, "h": h, "max_dim": max_d})
    return presets


def parse_timestamp(val: str, default_val: float) -> float:
    if not val:
        return default_val
    s = val.strip().lower().rstrip("s")
    if not s:
        return default_val
    try:
        if ":" in s:
            parts = [float(p) for p in s.split(":")]
            if len(parts) == 2:
                return parts[0] * 60.0 + parts[1]
            elif len(parts) == 3:
                return parts[0] * 3600.0 + parts[1] * 60.0 + parts[2]
        return float(s)
    except Exception:
        return default_val


def format_seconds(sec: float) -> str:
    if sec < 60:
        return f"{sec:.1f}s" if (sec % 1 >= 0.05) else f"{int(round(sec))}s"
    mins = int(sec // 60)
    rem = sec - mins * 60
    return f"{mins}:{rem:04.1f}"


def download_slack_video(url: str, dest_path: str):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"})
    with urllib.request.urlopen(req, timeout=45) as resp, open(dest_path, "wb") as out_f:
        shutil.copyfileobj(resp, out_f, length=1024 * 1024)


def convert_video_to_optimized_gif(
    video_path: str,
    gif_path: str,
    start_s: float = 0.0,
    end_s: float | None = None,
    forced_max_dim: int | None = None,
) -> tuple[int, int, int, float, float]:
    """
    Adaptive 2-pass ffmpeg palettegen + paletteuse converter with optional trim and scale.
    Returns: (out_w, out_h, fps, size_mb, total_video_duration)
    """
    total_duration = get_video_duration(video_path)
    start_s = max(0.0, min(start_s, max(0.0, total_duration - 0.2)))
    if end_s is None or end_s <= start_s:
        clip_duration = min(30.0, total_duration - start_s)
    else:
        clip_duration = min(30.0, max(0.3, min(end_s, total_duration) - start_s))

    if forced_max_dim:
        if clip_duration <= 10.0:
            tiers = [(forced_max_dim, 16), (forced_max_dim, 12), (forced_max_dim, 10)]
        else:
            tiers = [(forced_max_dim, 14), (forced_max_dim, 11), (forced_max_dim, 8)]
    else:
        if clip_duration <= 6.0:
            tiers = [(720, 18), (600, 15), (480, 12), (400, 10)]
        elif clip_duration <= 14.0:
            tiers = [(600, 15), (540, 14), (480, 12), (400, 10)]
        else:
            tiers = [(540, 12), (480, 12), (400, 10), (360, 8)]

    palette_path = video_path + "_palette.png"

    time_args = []
    if start_s > 0.01:
        time_args += ["-ss", f"{start_s:.2f}"]
    time_args += ["-t", f"{clip_duration:.2f}"]

    chosen_fps = tiers[-1][1]
    for max_dim, fps in tiers:
        chosen_fps = fps
        scale_filter = (
            f"fps={fps},"
            f"scale='min({max_dim},iw)':'min({max_dim},ih)':force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad=ceil(iw/2)*2:ceil(ih/2)*2"
        )
        cmd_pal = (
            ["ffmpeg", "-y"]
            + time_args
            + [
                "-i", video_path,
                "-vf", f"{scale_filter},palettegen=stats_mode=diff",
                palette_path,
            ]
        )
        subprocess.run(cmd_pal, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        cmd_gif = (
            ["ffmpeg", "-y"]
            + time_args
            + [
                "-i", video_path,
                "-i", palette_path,
                "-lavfi", f"{scale_filter} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
                gif_path,
            ]
        )
        subprocess.run(cmd_gif, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        sz = os.path.getsize(gif_path)
        if sz <= MAX_GIF_BYTES:
            break

    out_w, out_h = get_media_dimensions(gif_path)
    sz_mb = os.path.getsize(gif_path) / (1024 * 1024)
    return out_w, out_h, chosen_fps, sz_mb, total_duration


# ==============================================================================
# INTERACTIVE SLACK UI (PRE-RENDER CLIP CARD, TRIM MODAL & RESIZE BUTTONS)
# ==============================================================================
def build_prerender_clip_blocks(
    channel: str,
    thread_ts: str,
    message_ts: str,
    file_id: str,
    orig_name: str,
    orig_w: int,
    orig_h: int,
    base_w: int,
    base_h: int,
    total_dur: float,
) -> list[dict]:
    """
    Shown BEFORE rendering when a video is >10s long or when triggered via ✂️ (:scissors:).
    Lets the user immediately open the Clip & Size modal without waiting for a full-video render.
    """
    default_end = round(min(10.0, total_dur), 1)
    meta_base = {
        "c": channel,
        "t": thread_ts,
        "m": message_ts,
        "f": file_id,
        "n": orig_name[:40],
        "bw": base_w,
        "bh": base_h,
        "dur": round(total_dur, 1),
        "ss": 0.0,
        "es": default_end,
    }

    buttons = [
        {
            "type": "button",
            "action_id": "open_trim_modal",
            "style": "primary",
            "text": {"type": "plain_text", "text": "✂️ Select Clip & Size...", "emoji": True},
            "value": json.dumps(meta_base),
        }
    ]

    if total_dur > 10.0:
        first10_meta = dict(meta_base)
        first10_meta["ss"] = 0.0
        first10_meta["es"] = 10.0
        buttons.append(
            {
                "type": "button",
                "action_id": "scale_gif_first10",
                "text": {"type": "plain_text", "text": "⚡ Convert First 10s", "emoji": True},
                "value": json.dumps(first10_meta),
            }
        )

    full_meta = dict(meta_base)
    full_meta["ss"] = 0.0
    full_meta["es"] = round(min(30.0, total_dur), 1)
    full_label = "🎞️ Convert Full Video" if total_dur <= 30.0 else "🎞️ Convert First 30s"
    buttons.append(
        {
            "type": "button",
            "action_id": "scale_gif_full",
            "text": {"type": "plain_text", "text": full_label, "emoji": True},
            "value": json.dumps(full_meta),
        }
    )

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🎬 *`{orig_name}`* is *{format_seconds(total_dur)}* long (`{orig_w}×{orig_h}px`).\n"
                    f"Select the section & dimensions you want to turn into a GIF:"
                ),
            },
        },
        {"type": "actions", "elements": buttons},
    ]


def build_control_blocks(
    channel: str,
    thread_ts: str,
    message_ts: str,
    file_id: str,
    orig_name: str,
    base_w: int,
    base_h: int,
    total_dur: float,
    start_s: float = 0.0,
    end_s: float | None = None,
) -> list[dict]:
    eff_end = end_s if (end_s is not None and end_s > start_s) else round(total_dur, 1)
    scales = compute_proportional_scales(base_w, base_h)

    meta_base = {
        "c": channel,
        "t": thread_ts,
        "m": message_ts,
        "f": file_id,
        "n": orig_name[:40],
        "bw": base_w,
        "bh": base_h,
        "dur": round(total_dur, 1),
        "ss": round(start_s, 2),
        "es": round(eff_end, 2),
    }

    buttons = [
        {
            "type": "button",
            "action_id": "open_trim_modal",
            "style": "primary",
            "text": {"type": "plain_text", "text": "✂️ Trim Time", "emoji": True},
            "value": json.dumps(meta_base),
        }
    ]

    for sc in scales:
        btn_meta = dict(meta_base)
        btn_meta["dim"] = sc["max_dim"]
        btn_meta["pct"] = sc["pct"]
        btn_meta["tw"] = sc["w"]
        btn_meta["th"] = sc["h"]
        buttons.append(
            {
                "type": "button",
                "action_id": f"scale_gif_{sc['pct']}",
                "text": {
                    "type": "plain_text",
                    "text": f"📐 {sc['pct']}% ({sc['w']}×{sc['h']})",
                    "emoji": True,
                },
                "value": json.dumps(btn_meta),
            }
        )

    return [{"type": "actions", "elements": buttons}]


def fetch_target_message(client: WebClient, channel: str, ts: str) -> dict | None:
    try:
        res = client.conversations_history(channel=channel, latest=ts, oldest=ts, inclusive=True, limit=1)
        msgs = res.get("messages", [])
        if msgs and msgs[0].get("ts") == ts:
            return msgs[0]
    except SlackApiError:
        pass

    try:
        res = client.conversations_replies(channel=channel, ts=ts, latest=ts, oldest=ts, inclusive=True, limit=1)
        msgs = res.get("messages", [])
        if msgs:
            return msgs[0]
    except SlackApiError:
        pass
    return None


def resolve_video_file_info(client: WebClient, channel: str, message_ts: str, file_id: str | None = None) -> dict | None:
    if file_id:
        try:
            res = client.files_info(file=file_id)
            f = res.get("file")
            if f and (f.get("url_private_download") or f.get("url_private")):
                return f
        except SlackApiError:
            pass

    msg = fetch_target_message(client, channel, message_ts)
    if not msg:
        return None
    for f in msg.get("files", []):
        if file_id and f.get("id") == file_id:
            return f
        if f.get("filetype", "").lower() in VIDEO_EXTENSIONS or f.get("mimetype", "").lower() in VIDEO_MIMETYPES:
            return f
    return None


def execute_render_job(job: dict):
    client = WebClient(token=SLACK_BOT_TOKEN)
    channel = job["c"]
    thread_ts = job["t"]
    message_ts = job["m"]
    file_id = job.get("f")
    start_s = float(job.get("ss", 0.0))
    end_s = float(job["es"]) if job.get("es") is not None else None
    forced_max_dim = job.get("dim")
    base_w = job.get("bw")
    base_h = job.get("bh")

    is_blocked, is_warning, used_mb, used_count = check_monthly_free_tier_quota(client)
    if is_blocked:
        stop_msg = (
            f"🛑 *Free-Tier Safety Shutoff Reached:* GIF Bot has hit its monthly `$0.00` safety cap "
            f"(`{used_mb:.1f} MB` / `{MONTHLY_HARD_CAP_MB:.0f} MB`) and is paused until the 1st of next month."
        )
        try:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=stop_msg)
        except SlackApiError:
            pass
        return

    vf = resolve_video_file_info(client, channel, message_ts, file_id)
    if not vf:
        return

    url = vf.get("url_private_download") or vf.get("url_private")
    if not url or not is_valid_slack_download_url(url):
        return

    orig_name = os.path.basename(vf.get("name", job.get("n", "video.mp4")))
    base_name = os.path.splitext(orig_name)[0]

    suffix_parts = []
    if start_s > 0.05 or (end_s is not None and end_s < float(job.get("dur", 9999)) - 0.1):
        end_label = f"{end_s:.1f}s" if end_s else "end"
        suffix_parts.append(f"{start_s:.1f}s-{end_label}")
    if job.get("tw") and job.get("th"):
        suffix_parts.append(f"{job['tw']}x{job['th']}")
    suffix_str = ("_" + "_".join(suffix_parts)) if suffix_parts else ""
    gif_filename = f"{base_name}{suffix_str}.gif"

    status_ts = None
    try:
        desc_bits = [" · ".join(suffix_parts)] if suffix_parts else []
        status_res = client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"⏳ Rendering GIF ({', '.join(desc_bits) or 'optimized'})...",
        )
        status_ts = status_res.get("ts")
    except SlackApiError:
        pass

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            local_video = os.path.join(tmpdir, "input_video")
            local_gif = os.path.join(tmpdir, gif_filename)

            download_slack_video(url, local_video)

            out_w, out_h, fps, sz_mb, total_dur = convert_video_to_optimized_gif(
                local_video,
                local_gif,
                start_s=start_s,
                end_s=end_s,
                forced_max_dim=forced_max_dim,
            )

            eff_end = min(total_dur, end_s) if end_s else total_dur
            trim_badge = (
                f" · ✂️ `{format_seconds(start_s)}–{format_seconds(eff_end)}`"
                if (start_s > 0.05 or (end_s and end_s < total_dur - 0.1))
                else ""
            )
            comment = (
                f"🎞️ `{gif_filename}` (`{out_w}×{out_h}px` · `{fps}fps` · `{sz_mb:.1f} MB`{trim_badge})"
            )

            new_used_mb = used_mb + sz_mb
            new_used_count = used_count + 1
            if is_warning or (new_used_mb >= MONTHLY_WARN_CAP_MB) or (new_used_count >= MONTHLY_WARN_CAP_COUNT):
                comment += (
                    f"\n⚠️ *Free-Tier Quota Warning:* `{new_used_mb:.1f} MB` / `{MONTHLY_HARD_CAP_MB:.0f} MB` used this month."
                )

            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=local_gif,
                filename=gif_filename,
                title=gif_filename,
                initial_comment=comment,
            )

            ref_w, ref_h = (base_w, base_h) if (base_w and base_h) else compute_base_gif_dimensions(out_w, out_h)
            blocks = build_control_blocks(
                channel=channel,
                thread_ts=thread_ts,
                message_ts=message_ts,
                file_id=vf.get("id", file_id or ""),
                orig_name=orig_name,
                base_w=ref_w,
                base_h=ref_h,
                total_dur=total_dur,
                start_s=start_s,
                end_s=eff_end,
            )
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="Trim or resize GIF:",
                blocks=blocks,
            )

            try:
                client.reactions_add(channel=channel, timestamp=message_ts, name="white_check_mark")
            except SlackApiError:
                pass
    finally:
        if status_ts:
            try:
                client.chat_delete(channel=channel, ts=status_ts)
            except SlackApiError:
                pass


def process_video_reaction(channel: str, message_ts: str, user_id: str, reaction: str = "film_frames"):
    client = WebClient(token=SLACK_BOT_TOKEN)
    msg = fetch_target_message(client, channel, message_ts)
    if not msg:
        return

    files = msg.get("files", [])
    video_files = [
        f for f in files
        if f.get("filetype", "").lower() in VIDEO_EXTENSIONS
        or f.get("mimetype", "").lower() in VIDEO_MIMETYPES
    ]
    if not video_files:
        return

    thread_ts = msg.get("thread_ts") or message_ts
    force_clip_first = reaction in CLIP_FIRST_EMOJIS

    is_blocked, is_warning, used_mb, used_count = check_monthly_free_tier_quota(client)
    if is_blocked:
        stop_msg = (
            f"🛑 *Free-Tier Safety Shutoff Reached:* GIF Bot has hit its monthly `$0.00` safety cap "
            f"(`{used_mb:.1f} MB` / `{MONTHLY_HARD_CAP_MB:.0f} MB` or `{used_count}` / `{MONTHLY_HARD_CAP_COUNT}` GIFs this month) "
            f"and is paused until the 1st of next month so Google Cloud never bills a cent."
        )
        try:
            client.reactions_add(channel=channel, timestamp=message_ts, name="octagonal_sign")
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=stop_msg)
        except SlackApiError:
            pass
        notify_admin_dm(client, stop_msg)
        return

    for vf in video_files:
        file_id = vf.get("id", "")
        mode_tag = "clip" if force_clip_first else "auto"
        dedup_key = f"{channel}:{message_ts}:{file_id}:{mode_tag}"
        with _PROCESSED_LOCK:
            if dedup_key in _PROCESSED_KEYS:
                continue
            _PROCESSED_KEYS.add(dedup_key)

        url = vf.get("url_private_download") or vf.get("url_private")
        if not url or not is_valid_slack_download_url(url):
            continue

        orig_name = os.path.basename(vf.get("name", "video.mp4"))

        # Fast-probe video duration & dimensions over HTTPS before deciding whether to full-render
        orig_w, orig_h, total_dur = probe_media_info(url, is_remote_slack=True)
        base_w, base_h = compute_base_gif_dimensions(orig_w, orig_h)

        # SMART HYBRID CHECK:
        # If the user reacted with ✂️ (:scissors:) OR the video is longer than 10 seconds,
        # immediately post the Clip & Size selector card in the thread without rendering the full video!
        if force_clip_first or total_dur > AUTO_CLIP_PROMPT_SECONDS:
            blocks = build_prerender_clip_blocks(
                channel=channel,
                thread_ts=thread_ts,
                message_ts=message_ts,
                file_id=file_id,
                orig_name=orig_name,
                orig_w=orig_w,
                orig_h=orig_h,
                base_w=base_w,
                base_h=base_h,
                total_dur=total_dur,
            )
            try:
                client.reactions_add(channel=channel, timestamp=message_ts, name="scissors")
            except SlackApiError:
                pass
            try:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"🎬 {orig_name} is {format_seconds(total_dur)} long — select clip & size:",
                    blocks=blocks,
                )
            except SlackApiError:
                pass
            continue

        # Otherwise (short video <= 10s), convert immediately!
        try:
            client.reactions_add(channel=channel, timestamp=message_ts, name="hourglass_flowing_sand")
        except SlackApiError:
            pass

        base_name = os.path.splitext(orig_name)[0]
        gif_filename = f"{base_name}.gif"

        with tempfile.TemporaryDirectory() as tmpdir:
            local_video = os.path.join(tmpdir, "input_video")
            local_gif = os.path.join(tmpdir, gif_filename)

            download_slack_video(url, local_video)

            out_w, out_h, fps, sz_mb, total_dur = convert_video_to_optimized_gif(local_video, local_gif)
            new_used_mb = used_mb + sz_mb
            new_used_count = used_count + 1

            comment = (
                f"🎞️ `{gif_filename}` (`{out_w}×{out_h}px` · `{fps}fps` · `{sz_mb:.1f} MB`)"
            )
            if is_warning or (new_used_mb >= MONTHLY_WARN_CAP_MB) or (new_used_count >= MONTHLY_WARN_CAP_COUNT):
                warn_line = (
                    f"\n⚠️ *Free-Tier Quota Warning:* `{new_used_mb:.1f} MB` / `{MONTHLY_HARD_CAP_MB:.0f} MB` "
                    f"(`{new_used_count}` / `{MONTHLY_HARD_CAP_COUNT}` GIFs) used this month. "
                    f"Bot will auto-shutoff at `{MONTHLY_HARD_CAP_MB:.0f} MB` to guarantee `$0.00` billing."
                )
                comment += warn_line
                notify_admin_dm(client, f"🎞️ GIF Bot Warning: {warn_line.strip()}")

            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=local_gif,
                filename=gif_filename,
                title=gif_filename,
                initial_comment=comment,
            )

            blocks = build_control_blocks(
                channel=channel,
                thread_ts=thread_ts,
                message_ts=message_ts,
                file_id=file_id,
                orig_name=orig_name,
                base_w=out_w,
                base_h=out_h,
                total_dur=total_dur,
                start_s=0.0,
                end_s=total_dur,
            )
            try:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text="Trim or resize GIF:",
                    blocks=blocks,
                )
            except SlackApiError:
                pass

        try:
            client.reactions_remove(channel=channel, timestamp=message_ts, name="hourglass_flowing_sand")
        except SlackApiError:
            pass
        try:
            client.reactions_add(channel=channel, timestamp=message_ts, name="white_check_mark")
        except SlackApiError:
            pass


def build_trim_modal_view(meta: dict) -> dict:
    base_w = int(meta.get("bw", 540))
    base_h = int(meta.get("bh", 540))
    total_dur = float(meta.get("dur", 10.0))
    start_s = float(meta.get("ss", 0.0))
    end_s = float(meta.get("es", min(10.0, total_dur)))

    scales = compute_proportional_scales(base_w, base_h)
    scale_options = [
        {
            "text": {"type": "plain_text", "text": f"100% — {base_w}×{base_h} px (Full Optimized)"},
            "value": f"100:{max(base_w, base_h)}:{base_w}:{base_h}",
        }
    ]
    for sc in scales:
        scale_options.append(
            {
                "text": {
                    "type": "plain_text",
                    "text": f"{sc['pct']}% — {sc['w']}×{sc['h']} px (Same Aspect Ratio)",
                },
                "value": f"{sc['pct']}:{sc['max_dim']}:{sc['w']}:{sc['h']}",
            }
        )

    return {
        "type": "modal",
        "callback_id": "trim_gif_modal_submit",
        "private_metadata": json.dumps(meta),
        "title": {"type": "plain_text", "text": "✂️ Clip & Resize GIF"},
        "submit": {"type": "plain_text", "text": "Render GIF"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Source video:* `{meta.get('n', 'video')}`\n"
                        f"*Total length:* `{format_seconds(total_dur)}` (`{total_dur:.1f}s`) · *Base GIF Canvas:* `{base_w}×{base_h}px`"
                    ),
                },
            },
            {
                "type": "input",
                "block_id": "start_block",
                "label": {"type": "plain_text", "text": "Start Time"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "start_input",
                    "initial_value": f"{start_s:.1f}" if start_s > 0 else "0",
                    "placeholder": {"type": "plain_text", "text": "e.g. 0 or 2.5 or 0:12"},
                },
                "hint": {
                    "type": "plain_text",
                    "text": "Enter seconds (e.g. 4.5) or MM:SS (e.g. 0:14.5)",
                },
            },
            {
                "type": "input",
                "block_id": "end_block",
                "label": {"type": "plain_text", "text": "End Time"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "end_input",
                    "initial_value": f"{end_s:.1f}",
                    "placeholder": {"type": "plain_text", "text": f"e.g. {min(10.0, total_dur):.1f} or 0:20"},
                },
                "hint": {
                    "type": "plain_text",
                    "text": f"Video ends at {format_seconds(total_dur)} ({total_dur:.1f}s). Clips up to 30s supported.",
                },
            },
            {
                "type": "input",
                "block_id": "scale_block",
                "label": {"type": "plain_text", "text": "Dimensions (Keeps Aspect Ratio)"},
                "element": {
                    "type": "static_select",
                    "action_id": "scale_select",
                    "initial_option": scale_options[0],
                    "options": scale_options,
                },
            },
        ],
    }


def open_trim_modal_for_user(trigger_id: str, meta: dict):
    client = WebClient(token=SLACK_BOT_TOKEN)
    view = build_trim_modal_view(meta)
    client.views_open(trigger_id=trigger_id, view=view)


def handle_message_shortcut(trigger_id: str, payload: dict):
    """
    Handles the Slack `⋮` (More actions) -> "✂️ Clip / Make GIF" message shortcut on any video message.
    Opens the modal immediately (<150ms) and updates it with exact ffprobe dimensions/duration.
    """
    client = WebClient(token=SLACK_BOT_TOKEN)
    channel = (payload.get("channel") or {}).get("id", "")
    msg = payload.get("message") or {}
    message_ts = msg.get("ts", "")
    thread_ts = msg.get("thread_ts") or message_ts

    files = msg.get("files", [])
    video_files = [
        f for f in files
        if f.get("filetype", "").lower() in VIDEO_EXTENSIONS
        or f.get("mimetype", "").lower() in VIDEO_MIMETYPES
    ]

    if not video_files:
        client.views_open(
            trigger_id=trigger_id,
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": "No Video Found"},
                "close": {"type": "plain_text", "text": "Close"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "⚠️ This message doesn't contain a supported video file (`.mp4`, `.mov`, `.webm`, `.m4v`).",
                        },
                    }
                ],
            },
        )
        return

    vf = video_files[0]
    file_id = vf.get("id", "")
    orig_name = os.path.basename(vf.get("name", "video.mp4"))
    url = vf.get("url_private_download") or vf.get("url_private") or ""

    # Open modal immediately with fast defaults so trigger_id never expires
    meta = {
        "c": channel,
        "t": thread_ts,
        "m": message_ts,
        "f": file_id,
        "n": orig_name[:40],
        "bw": 720,
        "bh": 406,
        "dur": 15.0,
        "ss": 0.0,
        "es": 6.0,
    }
    res = client.views_open(trigger_id=trigger_id, view=build_trim_modal_view(meta))
    view_id = (res.get("view") or {}).get("id")

    # Probe exact dimensions & duration in background thread and update modal seamlessly
    if view_id and url and is_valid_slack_download_url(url):
        def _probe_and_update():
            try:
                orig_w, orig_h, total_dur = probe_media_info(url, is_remote_slack=True)
                base_w, base_h = compute_base_gif_dimensions(orig_w, orig_h)
                meta["bw"] = base_w
                meta["bh"] = base_h
                meta["dur"] = round(total_dur, 1)
                meta["es"] = round(min(10.0, total_dur), 1)
                client.views_update(view_id=view_id, view=build_trim_modal_view(meta))
            except Exception:
                pass

        threading.Thread(target=_probe_and_update, daemon=True).start()


# ==============================================================================
# FASTAPI ROUTES
# ==============================================================================
@app.get("/")
def healthcheck():
    return {"status": "ok", "service": "slack-gif-bot"}


@app.post("/internal/render")
async def internal_render(request: Request):
    raw_body = await request.body()
    sig = request.headers.get("X-Internal-Signature", "")
    if not SLACK_SIGNING_SECRET or not hmac.compare_digest(make_internal_token(raw_body), sig):
        raise HTTPException(status_code=401, detail="Invalid internal signature")
    job = json.loads(raw_body.decode("utf-8"))
    await asyncio.to_thread(execute_render_job, job)
    return {"ok": True}


@app.post("/slack/interactions")
async def slack_interactions(request: Request):
    raw_body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not verify_slack_signature(raw_body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    parsed = urllib.parse.parse_qs(raw_body.decode("utf-8"))
    payload_str = parsed.get("payload", ["{}"])[0]
    payload = json.loads(payload_str)

    team_id = (payload.get("team") or {}).get("id", "")
    if ALLOWED_SLACK_TEAM_ID and team_id != ALLOWED_SLACK_TEAM_ID:
        raise HTTPException(status_code=403, detail="Unauthorized Slack workspace")

    ptype = payload.get("type")
    base_url = resolve_self_base_url(request)

    if ptype == "message_action" and payload.get("callback_id") == "clip_video_to_gif":
        trigger_id = payload.get("trigger_id", "")
        if trigger_id:
            handle_message_shortcut(trigger_id, payload)
        return {"ok": True}

    if ptype == "block_actions":
        actions = payload.get("actions", [])
        if not actions:
            return {"ok": True}
        act = actions[0]
        action_id = act.get("action_id", "")
        meta = json.loads(act.get("value", "{}"))

        if action_id == "open_trim_modal":
            trigger_id = payload.get("trigger_id", "")
            if trigger_id and meta:
                open_trim_modal_for_user(trigger_id, meta)
            return {"ok": True}

        elif action_id.startswith("scale_gif_"):
            dispatch_async_render_job(meta, base_url)
            return {"ok": True}

    elif ptype == "view_submission":
        view = payload.get("view", {})
        if view.get("callback_id") == "trim_gif_modal_submit":
            meta = json.loads(view.get("private_metadata", "{}"))
            vals = view.get("state", {}).get("values", {})

            start_str = (
                vals.get("start_block", {}).get("start_input", {}).get("value", "0")
            )
            end_str = (
                vals.get("end_block", {})
                .get("end_input", {})
                .get("value", str(meta.get("dur", 10.0)))
            )
            scale_val = (
                vals.get("scale_block", {})
                .get("scale_select", {})
                .get("selected_option", {})
                .get("value", "")
            )

            total_dur = float(meta.get("dur", 10.0))
            start_s = max(0.0, parse_timestamp(start_str, 0.0))
            end_s = min(total_dur, parse_timestamp(end_str, total_dur))

            if end_s <= start_s + 0.2:
                return {
                    "response_action": "errors",
                    "errors": {
                        "end_block": "End time must be at least 0.2 seconds after Start time."
                    },
                }

            meta["ss"] = round(start_s, 2)
            meta["es"] = round(end_s, 2)

            if scale_val:
                parts = scale_val.split(":")
                pct = int(parts[0])
                if pct < 100 and len(parts) == 4:
                    meta["pct"] = pct
                    meta["dim"] = int(parts[1])
                    meta["tw"] = int(parts[2])
                    meta["th"] = int(parts[3])
                else:
                    meta.pop("dim", None)
                    meta.pop("tw", None)
                    meta.pop("th", None)

            dispatch_async_render_job(meta, base_url)
            return {}

    return {"ok": True}


@app.post("/slack/events")
async def slack_events(request: Request):
    if request.headers.get("X-Slack-Retry-Num"):
        return {"ok": True}

    raw_body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not verify_slack_signature(raw_body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    payload = await request.json()

    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}

    team_id = payload.get("team_id", "")
    if ALLOWED_SLACK_TEAM_ID and team_id != ALLOWED_SLACK_TEAM_ID:
        raise HTTPException(status_code=403, detail="Unauthorized Slack workspace")

    if payload.get("type") == "event_callback":
        event = payload.get("event", {})
        if event.get("type") == "reaction_added":
            reaction = event.get("reaction", "")
            item = event.get("item", {})
            if reaction in ALL_TRIGGER_EMOJIS and item.get("type") == "message":
                channel = item.get("channel")
                message_ts = item.get("ts")
                user_id = event.get("user", "")
                if channel and message_ts:
                    await asyncio.to_thread(
                        process_video_reaction, channel, message_ts, user_id, reaction
                    )

    return {"ok": True}
