# Lawyer Bot

Advokat Jasurbek jamoasi uchun Telegram asosidagi AI lead intake bot.

Botning vazifasi foydalanuvchiga yuridik maslahat berish emas. U mijoz vaziyatini tartibli aniqlaydi, lead sifatini oshiradi va tayyor murojaatlarni admin/yurist guruhiga yuboradi.

## Core Flow

1. **Channel gate** — foydalanuvchi rasmiy kanalga a'zo bo'lmasa, botdan foydalanishga yo'naltiriladi.
2. **Rate limit** — har bir foydalanuvchi uchun soatlik xabar limiti qo'llanadi.
3. **AI intake** — GPT foydalanuvchi tilida qisqa, empatik savollar beradi.
4. **Lead qualification** — bot muammo, hudud, muddat/shoshilinchlik, hujjatlar va telefon bor-yo'qligini baholaydi.
5. **Human handoff** — lead tayyor bo'lsa, suhbat admin guruhiga strukturali anketa va transcript bilan yuboriladi.
6. **Admin reply** — admin bot notification'iga reply qilsa, javob foydalanuvchiga boradi.
7. **Return to AI** — foydalanuvchi kerak bo'lsa AI yordamchiga qaytishi mumkin.

## Architecture

- **Domain** (`src/domain/`): `User`, `Conversation`, `Message`, `Lead` va value object'lar.
- **Application** (`src/application/`): suhbatni boshqarish, eskalatsiya, admin reply use case'lari.
- **Infrastructure** (`src/infrastructure/`): OpenAI, Telegram, SQLite adapterlari.
- **Interface** (`src/interface/`): FastAPI webhook va health endpoint.

## AI Behavior

AI intake assistant quyidagilarni qiladi:

- vaziyatni 2-3 gapda tushuntirishni so'raydi;
- bir javobda eng muhim 1-2 savolni beradi;
- hudud, hujjatlar, muddat va shoshilinchlikni aniqlaydi;
- handoffga yaqin telefon raqamni muloyim so'raydi;
- pullik konsultatsiya ekanini aniq aytadi;
- yuridik maslahat, qonun moddasi, jarima, muddat yoki kafolatli natija bermaydi.

## Admin Commands

Admin guruhida:

```text
/stats
/leads
/close
```

- `/stats` qisqa operational statistika beradi: 24 soatdagi suhbatlar, leadlar, jami suhbatlar, eskalatsiya kutayotganlar va conversion rate.
- `/leads` leadlar ro'yxatini pagination bilan ko'rsatadi. Filterlash va saralash mumkin:
  - `/leads` - barcha leadlar
  - `/leads ochiq` - ochiq leadlar
  - `/leads yangi` - yangi leadlar
  - `/leads yuqori` - yuqori balli leadlar
  - `/leads {raqam}` - bitta leadni ko'rsatadi va status o'zgartirish tugmalarini beradi
- `/close` bot notification'iga reply qilib yozilsa, shu foydalanuvchi suhbatini yopadi.

Lead notification'larida inline tugmalar ham bor:

- `Bog'landim`
- `Belgilandi`
- `To'langan`
- `Yo'qolgan`
- `Yopilgan`

## Environment Variables

`.env` faylida:

```env
OPENAI_API_KEY=

TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=
TELEGRAM_WEBHOOK_URL=https://your-domain.com
TELEGRAM_LEAD_CHAT_ID=
REQUIRED_CHANNEL_USERNAME=
REQUIRED_CHANNEL_ID=
```

## Local Development

```bash
python init_db.py
python scripts/migrations/migrate_add_lead_status.py
python scripts/migrations/migrate_add_message_count.py
python scripts/migrations/migrate_remove_citations_column.py
python -m src.interface.webhook_app
```

## Deployment Guide

### Docker Deployment

```bash
# Build image
docker build -t lawyer-bot:latest .

# Run with docker-compose
docker compose up -d

# Or run manually
docker run -d \
  --name lawyer_bot \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  -p 8000:8000 \
  --restart unless-stopped \
  lawyer-bot:latest
```

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
