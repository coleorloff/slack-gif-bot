# 🎞️ Slack Video-to-GIF Bot (Serverless & Free-Tier Protected)

A zero-database, serverless Slack bot built with **FastAPI** and **FFmpeg** for **Google Cloud Run**. React to any Slack video message (`.mp4`, `.mov`, `.webm`, `.m4v`) with an emoji, and the bot replies in the thread with a custom-palette, adaptive 2-pass `.gif`—complete with interactive Slack buttons to **✂️ Trim Time** and **📐 Proportionally Resize (`75%`, `50%`, `33%`)** while preserving the exact aspect ratio.

Designed for creative studios, motion teams, and product teams who frequently need deck-ready GIFs without leaving Slack—and engineered with a **hard monthly Free-Tier Circuit Breaker** so your Google Cloud Run bill stays at **`$0.00/month`**.

---

## ✨ Features

* **Smart Hybrid Workflow (Instant Short Clips + Pre-Render Clipping for Long Videos):**
  * **Short Videos (`≤ 10s`):** Reacting with 🎞️ (`:film_frames:`), 🎬 (`:clapper:`), or ⚡ (`:zap:`) immediately renders the full optimized GIF into the thread, with one-click buttons underneath to trim or downscale.
  * **Long Videos (`> 10s`) or ✂️ (`:scissors:`) Reaction:** Instead of wasting time turning a 90-second video into a massive, blurry GIF, the bot inspects the video header in `< 0.5s` and posts a **Clip & Size Selector** in the thread (`[ ✂️ Select Clip & Size... ]`, `[ ⚡ Convert First 10s ]`, `[ 🎞️ Convert First 30s ]`) so you can pick your exact clip *before* rendering.
  * **Message Menu Shortcut (`⋮` → `✂️ Clip / Make GIF`):** Click the `⋮` (More actions) menu on any Slack video message to pop open the **Clip & Resize Modal** directly.
* **2-Pass Adaptive FFmpeg Palette Engine:** Uses `palettegen=stats_mode=diff` + `paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle` with automatic resolution/FPS stepping so every GIF stays crisp and under `6.0 MB` (ensuring Slack auto-expands and loops it inline).
* **Proportional Aspect-Ratio Downscaling (`75%`, `50%`, `33%`):** Inspects the video via `ffprobe` (accounting for portrait/landscape rotation tags) and provides 3 smaller dimension options keeping the exact native aspect ratio (e.g., `📐 75% (540×304)`, `📐 50% (360×202)`, `📐 33% (240×136)`).
* **Stateless `$0.00` Cloud Run Free-Tier Circuit Breaker:** Queries Slack's `files.list` API for the bot user's monthly upload total (zero external database required, persists across container restarts). Throws a warning at 80% (`560 MB`) and hard-pauses at `700 MB/month` to stay well inside Google Cloud Run's `1 GiB/month` free outbound egress tier.

---

## 🔒 Security Architecture

This project is hardened for public internet deployment on serverless platforms:

1. **Fail-Closed HMAC-SHA256 Signature Verification:** Every request to `/slack/events` and `/slack/interactions` must be cryptographically signed by Slack using your `SLACK_SIGNING_SECRET` (`X-Slack-Signature`) and pass a strict 5-minute replay-attack window (`X-Slack-Request-Timestamp`). Unauthenticated requests are rejected in `< 1ms` with `401 Unauthorized` before touching FFmpeg or Slack APIs.
2. **Workspace Pinning (`ALLOWED_SLACK_TEAM_ID`):** Optionally lock the service to a single Slack Workspace ID (`T0...`).
3. **SSRF & Credential Leak Prevention:** Before downloading any video with the bot's `Authorization: Bearer` header, `is_valid_slack_download_url()` verifies the URL uses `https://` and belongs strictly to `*.slack.com` or `*.slack-edge.com`.
4. **Least-Privilege Container Execution:** Runs inside `python:3.11-slim` as an unprivileged non-root user (`appuser`), with temporary files isolated per job via `tempfile.TemporaryDirectory()` and immediately purged upon completion.
5. **Zero Hardcoded Secrets:** All tokens and secrets are injected exclusively at runtime via environment variables.

---

## 🚀 5-Minute Deployment Guide

### Step 1: Create the Slack App
1. Go to **[api.slack.com/apps](https://api.slack.com/apps)** → **Create New App** → **From an app manifest**.
2. Select your Slack workspace, switch to the **YAML** tab, and paste the contents of [`slack_app_manifest.yaml`](./slack_app_manifest.yaml).
3. Click **Create**, then click **Install to Workspace** in the left sidebar.
4. Copy two values from your Slack App settings:
   * **Bot User OAuth Token (`xoxb-...`)** from **OAuth & Permissions**
   * **Signing Secret** from **Basic Information → App Credentials**

---

### Step 2: Deploy to Google Cloud Run
Make sure you have the [Google Cloud SDK (`gcloud`)](https://cloud.google.com/sdk/docs/install) installed and authenticated (`gcloud auth login`), then run:

```bash
gcloud run deploy slack-gif-bot \
  --source . \
  --project YOUR_GCP_PROJECT_ID \
  --region us-central1 \
  --allow-unauthenticated \
  --max-instances 1 \
  --timeout 60s \
  --memory 2Gi \
  --cpu 2 \
  --set-env-vars "SLACK_BOT_TOKEN=xoxb-YOUR-TOKEN,SLACK_SIGNING_SECRET=YOUR-SIGNING-SECRET"
```

Once finished, Cloud Run will output your **Service URL**, e.g.:
```text
https://slack-gif-bot-xxxxxxxxxx.us-central1.run.app
```

---

### Step 3: Connect Slack Webhooks
Back in your Slack App dashboard at **[api.slack.com/apps](https://api.slack.com/apps)**:

1. **Event Subscriptions** → Enable Events → Paste:
   ```text
   https://YOUR-CLOUD-RUN-SERVICE-URL.run.app/slack/events
   ```
   Confirm `reaction_added` is listed under **Subscribe to bot events** and click **Save Changes**.
2. **Interactivity & Shortcuts** → Enable Interactivity → Paste:
   ```text
   https://YOUR-CLOUD-RUN-SERVICE-URL.run.app/slack/interactions
   ```
   Click **Save Changes**.

---

## 💬 Usage in Slack

1. Invite the bot to any channel once:
   ```text
   /invite @GIF Bot
   ```
2. React to any message containing a video with 🎞️ (`:film_frames:`), 🎬 (`:clapper:`), or ⚡ (`:zap:`).
3. The bot adds a ⏳ reaction while rendering, uploads the optimized `.gif` into the thread along with interactive **`✂️ Trim Time`** and **`📐 75% / 50% / 33%`** resize buttons, and updates its reaction to ✅.

---

## ⚙️ Environment Variables

| Variable | Required | Default | Description |
| :--- | :---: | :---: | :--- |
| `SLACK_BOT_TOKEN` | **Yes** | — | Slack Bot User OAuth Token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | **Yes** | — | Slack App Signing Secret for HMAC-SHA256 verification |
| `ALLOWED_SLACK_TEAM_ID` | No | `""` | Optional Slack Team ID (`T0...`) to restrict access to a single workspace |
| `ADMIN_SLACK_USER_ID` | No | `""` | Optional Slack User ID (`U0...`) to DM when monthly quota hits 80% or 100% |
| `TRIGGER_EMOJIS` | No | `film_frames,gif,clapper,zap` | Comma-separated list of Slack emoji names that trigger conversion |
| `MAX_GIF_MB` | No | `6.0` | Maximum file size (MB) per generated GIF |
| `MONTHLY_HARD_CAP_MB` | No | `700.0` | Monthly total upload cap (MB) before free-tier circuit breaker pauses the bot |
| `MONTHLY_HARD_CAP_COUNT` | No | `140` | Monthly total GIF count cap before free-tier circuit breaker pauses the bot |

---

## 📄 License

Released under the [MIT License](./LICENSE).
