# Testing Guide for Lawyer Bot

## Admin Commands Testing

### 1. Test `/stats` Command
- Go to admin group
- Send `/stats`
- Verify statistics are displayed:
  - 24 soatdagi suhbatlar
  - Leadlar
  - Jami suhbatlar
  - AI rejimida
  - Admin kutmoqda
  - Bog'lanilgan, Belgilangan, To'langan, Yo'qolgan, Yopilgan
  - Lead konversiyasi

### 2. Test `/leads` Command (List View)
- Send `/leads`
- Verify lead list is displayed with:
  - Pagination (◀ Oldingi / Keyingi ▶)
  - Filter buttons (Hammasi, Ochiq, Yangi)
  - Sort button (⭐ Yuqori / 📅 Sana)
  - Lead status labels (✅ Yangi, 📞 Bog'landim, etc.)
  - Score indicators (⭐ Yuqori, ⚡ O'rtacha, 💡 Past)
- Test pagination: click "Keyingi ▶"
- Test filters: click "Ochiq", "Yangi"
- Test sort: click "⭐ Yuqori"

### 3. Test `/leads {number}` Command (Single Lead View)
- Send `/leads 1` (or any valid lead number)
- Verify single lead is displayed with:
  - Lead number
  - User display name (@username or ID)
  - Date
  - Contact info
  - Summary
  - Current status
  - Status change buttons (✅ Bog'landim, 📅 Belgilandi, etc.)
- Click a status button and verify it changes

### 4. Test Admin Reply Flow
- Use `/leads {number}` to show a lead
- Reply to that message with a test message
- Verify reply goes to the user
- Verify conversation is escalated (user replies go to admin)

### 5. Test `/close` Command
- Reply to a bot notification message with `/close`
- Verify conversation is closed
- Verify lead status is set to "Yopilgan"

## Health Check Testing

### Test `/health` Endpoint
```bash
curl http://localhost:8000/health
```

Expected response:
```json
{
  "status": "healthy",
  "service": "lawyer-bot",
  "mode": "ai-lead-intake",
  "database": "ok",
  "bot": "ok"
}
```

## Backup Script Testing

### Test Backup Script Locally
```bash
# Run backup script
./backup_db.sh

# Verify backup was created
ls -la data/backups/

# Verify backup file is valid
sqlite3 data/backups/lawyer_bot_YYYYMMDD_HHMMSS.db.gz
```

## Logging Testing

### Verify File Logging
```bash
# Check if logs directory exists
ls -la logs/

# Check log file
tail -f logs/lawyer_bot.log

# Send a message to the bot and verify it's logged
```

## Known Issues to Check

- [ ] Bot responds to messages in private chat
- [ ] Channel membership check works
- [ ] Rate limiting prevents abuse
- [ ] Admin commands only work in admin group
- [ ] Webhook is set correctly
- [ ] Database persists after container restart

## Test Results

- `/stats`: ✅ / ❌
- `/leads` (list): ✅ / ❌
- `/leads {number}`: ✅ / ❌
- Pagination: ✅ / ❌
- Filters: ✅ / ❌
- Sort: ✅ / ❌
- Admin reply: ✅ / ❌
- `/close`: ✅ / ❌
- Health check: ✅ / ❌
- Backup script: ✅ / ❌
- File logging: ✅ / ❌

## Notes
