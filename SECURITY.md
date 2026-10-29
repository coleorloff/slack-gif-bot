# Security Policy

## Architecture & Isolation

This repository contains **source code and deployment templates only**. It does not host a shared service, database, or multi-tenant backend. Every user who deploys this project runs an independent container on their own cloud infrastructure using their own Slack App credentials.

### Built-in Security Controls

1. **Zero Hardcoded Credentials:**
   - `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` are loaded strictly from runtime environment variables (`os.environ`).
   - `.gitignore` and `.dockerignore` explicitly block `.env` files, private keys, and service-account JSON files from ever being committed or baked into container images.

2. **Fail-Closed HMAC-SHA256 Request Verification:**
   - Every incoming webhook request (`/slack/events` and `/slack/interactions`) is verified using Slack's `v0` HMAC-SHA256 signature scheme (`X-Slack-Signature`) and a 5-minute replay-attack window (`X-Slack-Request-Timestamp`).
   - If `SLACK_SIGNING_SECRET` is unset or the signature fails, the server immediately returns `401 Unauthorized` before parsing payloads or invoking `ffmpeg`.

3. **Workspace Pinning (`ALLOWED_SLACK_TEAM_ID`):**
   - Operators can set `ALLOWED_SLACK_TEAM_ID` (`T0...`) so the deployment rejects events from any other Slack workspace with `403 Forbidden`.

4. **SSRF & Token Leak Protection:**
   - Before attaching `Authorization: Bearer <SLACK_BOT_TOKEN>` to download a video file, `is_valid_slack_download_url()` enforces `https://` and verifies the hostname belongs strictly to `*.slack.com` or `*.slack-edge.com`.

5. **Non-Root Container & Ephemeral Storage:**
   - The Docker image runs as an unprivileged user (`appuser`).
   - Video and GIF files are processed inside an isolated `tempfile.TemporaryDirectory()` and deleted immediately after upload.

6. **Hard Monthly Free-Tier Circuit Breaker:**
   - To prevent unexpected cloud egress or compute billing, the bot queries its own monthly uploads via Slack's `files.list` API before every job and halts automatically if monthly usage reaches `MONTHLY_HARD_CAP_MB` (`700 MB` default) or `MONTHLY_HARD_CAP_COUNT` (`140 GIFs` default).

---

## Reporting a Vulnerability

If you discover a security issue in this repository, please open a private security advisory on GitHub rather than a public issue.
