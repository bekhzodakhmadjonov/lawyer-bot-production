# Oracle Cloud Deployment Guide for Lawyer Bot

Complete guide to deploy the lawyer bot to Oracle Cloud Always Free tier with PostgreSQL, Caddy SSL, and automated backups.

## Prerequisites

- Oracle Cloud account with Always Free tier
- SSH key pair for instance access
- Domain name (free DuckDNS subdomain works)
- Basic knowledge of SSH and command-line operations

## Step 1: Set Up Oracle Cloud Instance

### 1.1 Create Oracle Cloud Account
1. Go to https://www.oracle.com/cloud/free/
2. Sign up for Always Free tier
3. Verify email and phone number
4. Wait for account activation (usually instant)

### 1.2 Provision Instance
1. Log into Oracle Cloud Console
2. Navigate to **Compute → Instances → Create Instance**
3. Configure instance:
   - **Name**: lawyer-bot
   - **Compartment**: Your compartment
   - **Image**: Canonical Ubuntu 24.04 (aarch64/ARM) - IMPORTANT: Must be ARM image
   - **Shape**: Ampere → A1.Flex → 2 OCPU / 12 GB RAM
   - **SSH Key**: Upload your public SSH key
   - **Networking**: Assign public IP
4. Click **Create** and wait for instance to be ready (~5 minutes)

### 1.3 Configure Security
1. Go to **Networking → Virtual Cloud Networks**
2. Find your VNC and click **Security Lists**
3. Add ingress rules:
   - **Port 22**: SSH access (your IP or 0.0.0.0/0)
   - **Port 80**: HTTP (0.0.0.0/0)
   - **Port 443**: HTTPS (0.0.0.0/0)
4. Configure Ubuntu firewall on instance:
   ```bash
   sudo ufw allow 22/tcp
   sudo ufw allow 80/tcp
   sudo ufw allow 443/tcp
   sudo ufw enable
   ```

## Step 2: Set Up Free Domain

### 2.1 Create DuckDNS Account
1. Go to https://www.duckdns.org
2. Sign up for free account
3. Create a subdomain (e.g., lawyer-bot.duckdns.org)
4. Get your DuckDNS token from account page

### 2.2 Configure DNS
1. In DuckDNS, add your subdomain
2. Point it to your Oracle instance public IP
3. Wait for DNS propagation (usually 5-10 minutes)
4. Verify with: `nslookup lawyer-bot.duckdns.org`

## Step 3: Connect to Instance and Install Docker

### 3.1 SSH into Instance
```bash
ssh -i /path/to/your/key ubuntu@<INSTANCE_PUBLIC_IP>
```

### 3.2 Install Docker
```bash
# Install Docker
curl -fsSL https://get.docker.com | sudo sh

# Add user to docker group
sudo usermod -aG docker $USER

# Log out and log back in for group changes to take effect
exit
ssh -i /path/to/your/key ubuntu@<INSTANCE_PUBLIC_IP>

# Verify Docker installation
docker --version
docker compose version
```

### 3.3 Create Project Directory
```bash
mkdir -p ~/lawyer-bot
cd ~/lawyer-bot
```

## Step 4: Deploy Application

### 4.1 Copy Project Files
From your local machine:
```bash
scp -r /path/to/lawyer-bot/* ubuntu@<INSTANCE_PUBLIC_IP>:~/lawyer-bot/
```

Or use git:
```bash
cd ~/lawyer-bot
git clone <your-repo-url> .
```

### 4.2 Configure Environment Variables
```bash
cd ~/lawyer-bot
cp .env.example .env
nano .env
```

Edit `.env` with your values:
```bash
ENVIRONMENT=production
POSTGRES_PASSWORD=your_secure_password_here
OPENAI_API_KEY=your_openai_api_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_WEBHOOK_SECRET=your_webhook_secret
TELEGRAM_WEBHOOK_URL=https://lawyer-bot.duckdns.org
TELEGRAM_LEAD_CHAT_ID=your_chat_id
REQUIRED_CHANNEL_USERNAME=your_channel
REQUIRED_CHANNEL_ID=your_channel_id
DOMAIN=lawyer-bot.duckdns.org
```

### 4.3 Update Caddyfile
```bash
nano Caddyfile
```

Replace `yourdomain.example.com` with your actual domain (e.g., lawyer-bot.duckdns.org) in both places.

### 4.4 Initialize Database
```bash
# Build and start containers
docker compose up -d --build

# Initialize database schema
docker compose exec bot python init_db.py

# Check logs
docker compose logs -f bot
```

### 4.5 Verify Deployment
```bash
# Check all containers are running
docker compose ps

# Check health endpoint
curl http://localhost:8000/health

# Check logs
docker compose logs bot
docker compose logs postgres
docker compose logs caddy
```

## Step 5: Configure Telegram Webhook

### 5.1 Set Webhook
```bash
curl -F "url=https://lawyer-bot.duckdns.org/webhook" \
     -F "secret_token=YOUR_WEBHOOK_SECRET" \
     https://api.telegram.org/botYOUR_BOT_TOKEN/setWebhook
```

### 5.2 Verify Webhook
```bash
curl https://api.telegram.org/botYOUR_BOT_TOKEN/getWebhookInfo
```

### 5.3 Test Bot
Send a message to your bot on Telegram to verify it's working.

## Step 6: Set Up Automated Backups

### 6.1 Install Oracle CLI
```bash
curl -sL https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh | bash
exec -l bash
oci setup config
```

### 6.2 Create Object Storage Bucket
1. Go to Oracle Cloud Console
2. Navigate to **Object Storage → Bucket → Create Bucket**
3. Name: lawyer-bot-backups
4. Storage tier: Standard
5. Click **Create**

### 6.3 Configure Backup Script
```bash
# Make backup script executable
chmod +x scripts/backup.sh

# Add environment variables for backup
echo "OCI_BUCKET=lawyer-bot-backups" >> .env
echo "OCI_NAMESPACE=your-namespace" >> .env

# Test backup script
./scripts/backup.sh
```

### 6.4 Set Up Cron Job
```bash
# Edit crontab
crontab -e

# Add daily backup at 2 AM UTC
0 2 * * * /home/ubuntu/lawyer-bot/scripts/backup.sh >> /var/log/lawyer_bot_backup.log 2>&1
```

## Step 7: Set Up Monitoring

### 7.1 Configure Monitoring Script
```bash
# Make monitoring script executable
chmod +x scripts/monitor.sh

# Add monitoring environment variables
echo "HEALTH_CHECK_URL=http://localhost:8000/health" >> .env
echo "DOMAIN=lawyer-bot.duckdns.org" >> .env
echo "TELEGRAM_BOT_TOKEN=your_bot_token" >> .env
echo "ADMIN_CHAT_ID=your_admin_chat_id" >> .env

# Test monitoring script
./scripts/monitor.sh
```

### 7.2 Set Up Monitoring Cron Job
```bash
# Edit crontab
crontab -e

# Add monitoring every 5 minutes
*/5 * * * * /home/ubuntu/lawyer-bot/scripts/monitor.sh >> /var/log/lawyer_bot_monitor.log 2>&1
```

### 7.3 Set Up Log Rotation
```bash
sudo nano /etc/logrotate.d/lawyer-bot
```

Add:
```
/var/log/lawyer_bot_*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 0644 ubuntu ubuntu
}
```

## Step 8: Security Hardening

### 8.1 Configure Fail2Ban
```bash
sudo apt update
sudo apt install fail2ban -y

# Create local configuration
sudo cp /etc/fail2ban/jail.conf /etc/fail2ban/jail.local

sudo nano /etc/fail2ban/jail.local
```

Add SSH protection:
```
[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
maxretry = 3
bantime = 3600
```

```bash
sudo systemctl enable fail2ban
sudo systemctl start fail2ban
```

### 8.2 Configure Firewall
```bash
# Check current rules
sudo ufw status

# Ensure only necessary ports are open
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw deny incoming
sudo ufw enable
```

### 8.3 Secure PostgreSQL
```bash
# PostgreSQL is already secured by Docker network isolation
# Only accessible from bot container, not exposed to internet
```

## Step 9: Performance Optimization

### 9.1 Tune PostgreSQL
```bash
docker compose exec postgres nano /var/lib/postgresql/data/postgresql.conf
```

Add optimizations:
```
shared_buffers = 256MB
effective_cache_size = 1GB
maintenance_work_mem = 64MB
checkpoint_completion_target = 0.9
wal_buffers = 16MB
default_statistics_target = 100
random_page_cost = 1.1
effective_io_concurrency = 200
work_mem = 1310kB
min_wal_size = 1GB
max_wal_size = 4GB
```

Restart PostgreSQL:
```bash
docker compose restart postgres
```

### 9.2 Monitor Resource Usage
```bash
# Check CPU and memory
htop

# Check disk usage
df -h

# Check Docker resource usage
docker stats
```

## Step 10: Testing and Validation

### 10.1 Load Testing
```bash
# Install Apache Bench
sudo apt install apache2-utils -y

# Test health endpoint
ab -n 1000 -c 10 http://localhost:8000/health
```

### 10.2 Database Performance Test
```bash
docker compose exec postgres psql -U postgres -d lawyer_bot -c "EXPLAIN ANALYZE SELECT * FROM conversations LIMIT 10;"
```

### 10.3 SSL Certificate Check
```bash
echo | openssl s_client -servername lawyer-bot.duckdns.org -connect lawyer-bot.duckdns.org:443 2>/dev/null | openssl x509 -noout -dates
```

## Troubleshooting

### Container Won't Start
```bash
# Check logs
docker compose logs bot
docker compose logs postgres
docker compose logs caddy

# Check container status
docker compose ps

# Restart containers
docker compose restart
```

### Database Connection Issues
```bash
# Check PostgreSQL is ready
docker compose exec postgres pg_isready -U postgres

# Check database logs
docker compose logs postgres

# Verify connection string
docker compose exec bot env | grep DATABASE_URL
```

### SSL Certificate Issues
```bash
# Check Caddy logs
docker compose logs caddy

# Check Caddy data
docker compose exec caddy ls -la /data/caddy/

# Manually trigger certificate renewal
docker compose restart caddy
```

### High Resource Usage
```bash
# Check resource usage
docker stats

# Restart containers
docker compose restart

# Check for memory leaks
docker compose exec bot python -c "import psutil; print(psutil.virtual_memory())"
```

### Backup Failures
```bash
# Check backup logs
tail /var/log/lawyer_bot_backup.log

# Test backup manually
./scripts/backup.sh

# Check OCI CLI configuration
oci setup config
```

## Maintenance

### Daily Tasks
- Check monitoring alerts
- Review error logs
- Verify backups completed

### Weekly Tasks
- Review resource usage
- Test backup restoration
- Check SSL certificate expiry
- Review security logs

### Monthly Tasks
- Update dependencies
- Review and optimize performance
- Security audit
- Cost analysis

## Emergency Procedures

### Application Down
1. Check container status: `docker compose ps`
2. Review logs: `docker compose logs`
3. Restart services: `docker compose restart`
4. Check database connectivity
5. Escalate if unresolved in 15 minutes

### Database Issues
1. Check PostgreSQL logs: `docker compose logs postgres`
2. Verify disk space: `df -h`
3. Restore from backup if needed
4. Contact support if data loss suspected

### SSL Certificate Issues
1. Check certificate expiry
2. Restart Caddy: `docker compose restart caddy`
3. Update webhook URL if changed
4. Test webhook connectivity

## Cost Monitoring

### Current Costs
- **Infrastructure**: $0/month (Always Free tier)
- **AI Costs**: ~$8/month (monitor OpenAI dashboard)
- **Total**: ~$8/month

### Cost Optimization Tips
- Monitor OpenAI API usage daily
- Implement response caching
- Use cheaper model for simple queries
- Review database storage usage

## Scaling

### When to Scale
- CPU usage consistently > 80%
- Memory usage consistently > 80%
- Response times > 5 seconds
- Database query times > 1 second

### Scaling Options
1. **Vertical Scaling**: Upgrade to paid Oracle Cloud instance
2. **Horizontal Scaling**: Add load balancer + multiple instances
3. **Database Scaling**: Move to managed PostgreSQL service

## Support Resources

- **Oracle Cloud Documentation**: https://docs.oracle.com/en-us/iaas/
- **Docker Documentation**: https://docs.docker.com/
- **Caddy Documentation**: https://caddyserver.com/docs/
- **PostgreSQL Documentation**: https://www.postgresql.org/docs/

## Next Steps

1. Monitor the deployment for 24-48 hours
2. Test with real users
3. Gather performance metrics
4. Optimize based on usage patterns
5. Set up additional monitoring if needed

## Success Criteria

- All containers running successfully
- SSL certificate valid and working
- Telegram webhook receiving messages
- Automated backups running daily
- Monitoring alerts configured
- Response times < 2 seconds
- 99.9% uptime during business hours

Your lawyer bot is now deployed to Oracle Cloud Always Free tier with automated backups, monitoring, and SSL certificates!
