# Production Deployment Guide

This guide covers deploying the lawyer bot to Azure Container Apps with Supabase database.

## Prerequisites

- Azure account with $100 student credits
- GitHub account for CI/CD
- Supabase account (free tier)
- Docker installed locally

## Step 1: Set Up Supabase Database

1. **Create Supabase Project**
   - Go to https://supabase.com
   - Sign up and create a new project
   - Choose a region close to your users
   - Wait for project to be ready (~2 minutes)

2. **Get Database Credentials**
   - Go to Project Settings → Database
   - Copy the connection string
   - Format: `postgresql+asyncpg://postgres:[YOUR-PASSWORD]@db.[PROJECT-REF].supabase.co:5432/postgres`

3. **Update Environment Variables**
   ```bash
   # In your .env file
   DATABASE_URL=postgresql+asyncpg://postgres:[YOUR-PASSWORD]@db.[PROJECT-REF].supabase.co:5432/postgres
   ```

4. **Run Database Migrations**
   ```bash
   # Install dependencies
   pip install -r requirements.txt
   
   # Initialize database
   python init_db.py
   
   # Run migrations
   python scripts/migrations/migrate_add_lead_status.py
   python scripts/migrations/migrate_add_message_count.py
   python scripts/migrations/migrate_remove_citations_column.py
   ```

## Step 2: Set Up Azure Resources

1. **Create Resource Group**
   ```bash
   az group create --name lawyer-bot-rg --location eastus
   ```

2. **Create Azure Container Registry**
   ```bash
   az acr create --resource-group lawyer-bot-rg --name lawyerbotacr --sku Basic
   az acr login --name lawyerbotacr
   ```

3. **Create Azure Container Apps Environment**
   ```bash
   az containerapp env create \
     --name lawyer-bot-env \
     --resource-group lawyer-bot-rg \
     --location eastus
   ```

## Step 3: Build and Push Docker Image

1. **Build Docker Image**
   ```bash
   docker build -t lawyer-bot:latest .
   ```

2. **Tag for Azure Container Registry**
   ```bash
   docker tag lawyer-bot:latest lawyerbotacr.azurecr.io/lawyer-bot:latest
   ```

3. **Push to Azure Container Registry**
   ```bash
   docker push lawyerbotacr.azurecr.io/lawyer-bot:latest
   ```

## Step 4: Deploy to Azure Container Apps

1. **Create Container App**
   ```bash
   az containerapp create \
     --name lawyer-bot \
     --resource-group lawyer-bot-rg \
     --environment lawyer-bot-env \
     --image lawyerbotacr.azurecr.io/lawyer-bot:latest \
     --target-port 8000 \
     --ingress external \
     --cpu 0.5 \
     --memory 1Gi \
     --min-replicas 1 \
     --max-replicas 10 \
     --secrets database-url=$DATABASE_URL \
     --secrets openai-api-key=$OPENAI_API_KEY \
     --secrets telegram-bot-token=$TELEGRAM_BOT_TOKEN \
     --secrets telegram-webhook-secret=$TELEGRAM_WEBHOOK_SECRET \
     --env-vars DATABASE_URL=secretref:database-url \
     --env-vars OPENAI_API_KEY=secretref:openai-api-key \
     --env-vars TELEGRAM_BOT_TOKEN=secretref:telegram-bot-token \
     --env-vars TELEGRAM_WEBHOOK_SECRET=secretref:telegram-webhook-secret \
     --env-vars TELEGRAM_WEBHOOK_URL=https://your-app-url \
     --env-vars TELEGRAM_LEAD_CHAT_ID=$TELEGRAM_LEAD_CHAT_ID \
     --env-vars REQUIRED_CHANNEL_USERNAME=$REQUIRED_CHANNEL_USERNAME \
     --env-vars REQUIRED_CHANNEL_ID=$REQUIRED_CHANNEL_ID \
     --env-vars ENVIRONMENT=production
   ```

2. **Get Application URL**
   ```bash
   az containerapp show \
     --name lawyer-bot \
     --resource-group lawyer-bot-rg \
     --query properties.configuration.ingress.fqdn \
     --output tsv
   ```

## Step 5: Set Up Telegram Webhook

1. **Set Webhook**
   ```bash
   curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook?url=https://YOUR-APP-URL/webhook&secret_token=$TELEGRAM_WEBHOOK_SECRET"
   ```

2. **Verify Webhook**
   ```bash
   curl https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo
   ```

## Step 6: Set Up GitHub Actions CI/CD

1. **Add GitHub Secrets**
   - Go to your repository → Settings → Secrets and variables → Actions
   - Add the following secrets:
     - `AZURE_CONTAINER_REGISTRY_NAME`: lawyerbotacr
     - `AZURE_REGISTRY_USERNAME`: [from Azure ACR]
     - `AZURE_REGISTRY_PASSWORD`: [from Azure ACR]
     - `AZURE_CREDENTIALS`: [Azure service principal JSON]
     - `APP_DOMAIN`: [your Container Apps domain]

2. **Create Azure Service Principal**
   ```bash
   az ad sp create-for-rbac \
     --name "github-deploy-lawyer-bot" \
     --role contributor \
     --scopes /subscriptions/{subscription-id}/resourceGroups/lawyer-bot-rg \
     --json-auth
   ```

3. **Push to GitHub**
   ```bash
   git add .
   git commit -m "Production deployment setup"
   git push origin main
   ```

## Step 7: Monitoring and Health Checks

1. **Check Health Endpoint**
   ```bash
   curl https://YOUR-APP-URL/health
   ```

2. **View Logs**
   ```bash
   az containerapp logs show \
     --name lawyer-bot \
     --resource-group lawyer-bot-rg \
     --follow
   ```

3. **Monitor Scaling**
   ```bash
   az containerapp revision list \
     --name lawyer-bot \
     --resource-group lawyer-bot-rg \
     --watch
   ```

## Cost Optimization Tips

1. **Monitor AI Costs**
   - Check OpenAI dashboard daily
   - Implement response caching for common queries
   - Use GPT-3.5 for simple questions

2. **Optimize Container Apps**
   - Set minimum replicas to 0 for cost savings (if acceptable latency)
   - Configure scale rules based on actual usage patterns
   - Monitor CPU/memory usage and adjust resources

3. **Database Optimization**
   - Supabase free tier handles 1k daily users easily
   - Monitor storage usage (500MB limit)
   - Implement connection pooling (already configured)

## Troubleshooting

### Database Connection Issues
- Check Supabase project status
- Verify connection string format
- Ensure network access from Azure

### Container Apps Scaling Issues
- Check resource limits
- Review scale rules
- Monitor CPU/memory usage

### Webhook Not Working
- Verify Telegram bot token
- Check webhook URL is accessible
- Ensure secret token matches

### High AI Costs
- Review OpenAI API usage
- Implement caching
- Optimize prompts

## Rollback Plan

If deployment fails:
1. Revert to previous Docker image
2. Restore database from backup
3. Check logs for errors
4. Fix issues and redeploy

## Next Steps

1. **Test with small user group** first
2. **Monitor costs** for first week
3. **Gather user feedback**
4. **Optimize based on usage patterns**
5. **Scale resources** as needed

## Support

- Azure Documentation: https://docs.microsoft.com/azure
- Supabase Documentation: https://supabase.com/docs
- GitHub Actions: https://docs.github.com/actions
