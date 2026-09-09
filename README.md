# Lawyer Bot

Advokat Jasurbek jamoasi uchun Telegram asosidagi AI lead intake bot.

Botning vazifasi foydalanuvchiga yuridik maslahat berish emas. U mijoz vaziyatini tartibli aniqlaydi, lead sifatini oshiradi va tayyor murojaatlarni admin/yurist guruhiga yuboradi.

This bot provides:
- **Legal Information**: Answers general legal questions using Gemini Flash with web search and citations
- **Lead Qualification**: Qualifies high-intent users and escalates to lawyers
- **Intent-Based Routing**: Automatically detects informational queries vs service requests
- **Cost Optimization**: Tiered AI strategy with response caching (50-70% cost reduction)

## Version 2 Changes

### New Features
- **Legal Information Mode**: Uses Gemini Flash with web search to answer general legal questions
- **Intent-Based Routing**: Automatically routes informational queries to Gemini, service requests to OpenAI
- **Response Caching**: Redis-based caching with tiered TTLs (24h for informational, 1h for intake)
- **Improved Escalation**: Only escalates qualified leads, not informational queries

### Optimizations
- **Resource Usage**: Reduced PostgreSQL pool from 30 to 8 connections, Redis from 20 to 10
- **Message History**: Reduced from 16 to 8 messages for performance
- **Removed Components**: Multi-turn analyzer, conversation summarizer, SQLite rate limiter fallback
- **Code Cleanup**: Renamed sqlite_* repos to postgres_* for consistency

### Cost Savings
- **AI Costs**: 50-70% reduction via caching + tiered models
- **Infrastructure**: Near $0 (Oracle Free Tier)
- **Estimated Monthly**: $10-20 for 5K users (vs $30-50 previous)

## Architecture

```
┌─────────────┐
│  Telegram   │
│   Webhook   │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ FastAPI     │
│  Webhook    │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  Aiogram    │
│  Dispatcher │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ Intent      │
│  Router     │
└──────┬──────┘
       │
       ├─────────────┬─────────────┐
       │             │             │
       ▼             ▼             ▼
┌─────────┐  ┌──────────┐  ┌──────────┐
│ Gemini  │  │  OpenAI  │  │PostgreSQL│
│ (Info)  │  │ (Leads)  │  │  (Data)  │
└────┬────┘  └────┬─────┘  └────┬─────┘
     │            │              │
     └────────────┴──────────────┘
                  │
                  ▼
           ┌──────────┐
           │  Redis   │
           │ (Cache)  │
           └──────────┘
```

## AI Behavior

### Informational Queries (Gemini Flash)
- Answers general legal questions with citations
- Uses web search for up-to-date information
- Provides disclaimer: "This is general information, not legal advice"
- Includes soft CTA for lawyer consultation
- Cached for 24 hours

### Service Requests (OpenAI GPT-4o-mini)
- Collects user situation information
- Qualifies leads based on conversation signals
- Escalates qualified leads to admin/lawyer group
- Empathetic questioning about problem, location, urgency, documents, phone

## Admin Commands

Admin commands are available in the lawyer group only:

- `/stats` - View conversation statistics
- `/leads` - View list of leads with pagination
- `/users` - View list of users
- `/close` - Close a conversation

## Environment Variables

Required environment variables:

```bash
# Environment
ENVIRONMENT=production

# Database (PostgreSQL)
DATABASE_URL=postgresql+asyncpg://user:password@host/db

# Redis (for caching and rate limiting)
REDIS_URL=redis://localhost:6379/0

# AI Provider
OPENAI_API_KEY=your_openai_api_key
GEMINI_API_KEY=your_gemini_api_key

# Telegram Bot Config
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_WEBHOOK_SECRET=your_webhook_secret
TELEGRAM_WEBHOOK_URL=https://yourdomain.com
TELEGRAM_LEAD_CHAT_ID=your_admin_chat_id
REQUIRED_CHANNEL_USERNAME=@your_channel
REQUIRED_CHANNEL_ID=123456789
```

## Local Development

1. Copy `.env.example` to `.env` and fill in values
2. Install dependencies: `pip install -r requirements.txt`
3. Run with: `python -m src.interface.webhook_app`

## Deployment

### Docker Compose (Recommended)

```bash
docker-compose up -d
```

This starts:
- Bot application
- PostgreSQL database
- Redis cache
- Caddy reverse proxy (with SSL)

## Troubleshooting

### Bot not responding
- Check webhook is set: `curl https://yourdomain.com/health`
- Check logs: `docker logs lawyer_bot`
- Verify Telegram token is valid

### Database connection issues
- Check DATABASE_URL is correct
- Verify PostgreSQL is running
- Check network connectivity

### Rate limiting issues
- Check Redis is running
- Verify REDIS_URL is correct
- Check rate limit settings in code

### Gemini API issues
- Verify GEMINI_API_KEY is valid
- Check API quota (5K free requests/month)
- Fallback to OpenAI if Gemini fails

### Azure Container Instances Deployment

1. **Build and push image:**
```bash
docker build -t lawyer-bot:latest .
az acr create --resource-group <rg-name> --name <acr-name> --sku Basic
az acr login --name <acr-name>
docker tag lawyer-bot:latest <acr-name>.azurecr.io/lawyer-bot:latest
docker push <acr-name>.azurecr.io/lawyer-bot:latest
```

2. **Create Azure Container Instance:**
```bash
az container create \
  --resource-group <rg-name> \
  --name lawyer-bot \
  --image <acr-name>.azurecr.io/lawyer-bot:latest \
  --cpu 1 \
  --memory 1 \
  --ports 8000 \
  --environment-variables \
    OPENAI_API_KEY=$OPENAI_API_KEY \
    TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN \
    TELEGRAM_WEBHOOK_SECRET=$TELEGRAM_WEBHOOK_SECRET \
    TELEGRAM_WEBHOOK_URL=https://<your-domain>.com \
    TELEGRAM_LEAD_CHAT_ID=$TELEGRAM_LEAD_CHAT_ID \
    REQUIRED_CHANNEL_USERNAME=$REQUIRED_CHANNEL_USERNAME \
    REQUIRED_CHANNEL_ID=$REQUIRED_CHANNEL_ID \
  --restart-policy Always \
  --dns-name-label <unique-name>
```

3. **Set webhook:**
```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook?url=https://<unique-name>.<region>.azurecontainer.io/webhook&secret_token=$TELEGRAM_WEBHOOK_SECRET"
```

## Troubleshooting

### Bot not responding
- Check if webhook is set: `curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo`
- Check container logs: `docker logs lawyer_bot_app`
- Verify environment variables are set correctly

### Database errors
- Ensure SQLite file exists in `data/` directory
- Check file permissions on `data/` directory
- Run migrations: `python scripts/migrations/migrate_add_lead_status.py`

### Rate limiting issues
- Check rate limit in database: `SELECT * FROM rate_limits WHERE user_id = ?`
- Rate limit resets every hour automatically

### Admin commands not working
- Verify bot is admin in the group
- Check `TELEGRAM_LEAD_CHAT_ID` matches the group ID
- Ensure commands are registered (bot restart required)

## Checks

```bash
ruff check src tests
ruff format --check src tests
pytest --ignore=claude_folder tests src
```

`claude_folder/` loyiha nusxasi sifatida qaraladi va asosiy development/test oqimidan tashqarida qoldiriladi.
