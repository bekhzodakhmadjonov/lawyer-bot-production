# 🚨 Production Troubleshooting & Incident Runbook
**Project:** Advokat Jasurbek AI Bot (`lawyer-bot-production`)  
**Server:** Oracle Cloud Always Free (Ubuntu / Linux)  
**Domain:** `lawyer-bot-uz.duckdns.org`  

---

## ⚡ Quick Emergency Cheat-Sheet

| Problem | Fastest Fix Command |
|---|---|
| **Bot completely frozen / not responding** | `docker compose restart bot` |
| **Containers down or crashed** | `docker compose up -d` |
| **Read live logs of errors** | `docker compose logs -f --tail 100 bot` |
| **Check health of all 4 services** | `docker compose ps` |
| **Telegram webhook check** | Run the Telegram Webhook Diagnostic command (Section 1.2) |
| **Clear all Redis cache & locks** | `docker compose exec redis redis-cli FLUSHDB` |

---

## Scenario 1: Bot is NOT responding to users in Telegram

### Step 1.1: Check if containers are running
On the server, run:
```bash
docker compose ps
```
* **Expected:** All 4 services (`lawyer_bot`, `lawyer_bot_postgres`, `lawyer_bot_redis`, `lawyer_bot_caddy`) show `Up (healthy)`.
* **If any container says `Exit` or `Restarting`:**
  ```bash
  # Check why that specific container crashed:
  docker compose logs <service_name>
  # Example: docker compose logs bot
  ```

### Step 1.2: Check Telegram Webhook Status
Telegram might have paused sending updates if your server had an error:
```bash
curl -s "https://api.telegram.org/bot<YOUR_TELEGRAM_BOT_TOKEN>/getWebhookInfo" | jq .
```
*(Replace `<YOUR_TELEGRAM_BOT_TOKEN>` with your actual token from `.env`).*

* **Look for `last_error_message`:**
  * `"Connection refused"` → Caddy or port 80/443 is blocked or Caddy crashed.
  * `"Wrong response code: 502"` → Caddy is running, but the Python `bot` container is crashed or unhealthy.
  * `"SSL error"` → Caddy hasn't finished issuing the Let's Encrypt SSL certificate yet (wait 1 minute).
* **If `pending_update_count` is very high:** Messages are piling up because the bot is unresponsive.

### Step 1.3: Check DuckDNS IP
If your Oracle server's public IP changed (e.g. after a reboot without a reserved IP), DuckDNS might be pointing to the wrong IP.
```bash
# Check your server's current public IP:
curl -s ifconfig.me

# Check what DuckDNS thinks your IP is:
nslookup lawyer-bot-uz.duckdns.org
```
* If they don't match, log in to [DuckDNS.org](https://www.duckdns.org) and update the IP.

---

## Scenario 2: Gemini AI Errors (Quota, Billing, 429s, or 503s)

### Step 2.1: Detect AI errors in logs
```bash
docker compose logs --tail 200 bot | grep -E "gemini|RESOURCE_EXHAUSTED|429|ClientError"
```

### Step 2.2: Fix `429 RESOURCE_EXHAUSTED` (Quota Limit)
* **Cause:** The Google account ran out of quota or the billing card had an issue.
* **Fix:**
  1. Open [Google AI Studio](https://aistudio.google.com/) and check **Settings > Plan & Billing**.
  2. If you need to switch to a new API key:
     ```bash
     # Edit .env and replace GEMINI_API_KEY
     nano .env
     
     # Re-create the bot container with the new env var:
     docker compose up -d
     ```

### Step 2.3: Fix `503 Service Unavailable`
* **Cause:** Google's Gemini servers are momentarily overloaded.
* **Fix:** The bot has built-in 3-attempt exponential backoff retry logic. If it persists for minutes, it's a global Google outage. Users will receive the polite fallback message: *"Kechirasiz, hozir texnik sababga ko'ra javob tayyorlay olmadim..."*.

---

## Scenario 3: A User is Stuck on "⏳ Avvalgi xabaringizga javob tayyorlanmoqda..."

* **Cause:** A user sent a message, the server crashed midway before releasing the lock, and the lock hasn't expired yet. (Locks auto-expire in 60 seconds anyway).
* **Fix (Immediate Manual Unlock):**
```bash
# View active user locks:
docker compose exec redis redis-cli KEYS "processing:*"

# Delete all processing locks instantly:
docker compose exec redis redis-cli --scan --pattern "processing:*" | xargs -r docker compose exec -T redis redis-cli DEL
```

---

## Scenario 4: Server Out of Disk Space (`No space left on device`)

Oracle free tier servers can run out of disk space if old unused Docker images build up over months.

### Step 4.1: Check disk space
```bash
df -h /
```
If `Use%` is **90% or higher**, clean up immediately.

### Step 4.2: Clean unused Docker cache and old images
```bash
# Safely remove all dangling containers, build caches, and old images:
docker system prune -af --volumes=false
```

---

## Scenario 5: How to Update Code in the Future

When you push new bug fixes or features to GitHub, run this 3-command sequence on the Oracle server:

```bash
cd lawyer-bot-production

# 1. Pull the new code
git pull origin main

# 2. Rebuild and restart the bot without downtime to Postgres/Redis
docker compose up -d --build bot

# 3. Verify logs
docker compose logs -f bot
```

---

## Scenario 6: Total Emergency Reset (Nuclear Option)

If the database gets corrupted, or you want to wipe everything and start from scratch Day 1:

```bash
cd lawyer-bot-production

# Stop everything AND delete all database volumes:
docker compose down -v

# Start fresh:
docker compose up -d --build
```
