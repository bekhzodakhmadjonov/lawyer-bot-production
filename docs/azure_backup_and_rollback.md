# Azure Backup and Rollback Strategy

This document outlines the backup and rollback procedures for the lawyer-bot deployment on Azure to ensure production stability and quick recovery from issues.

## Database Backups

### SQLite Database Backup Strategy

Since the application uses SQLite, we need automated backups to Azure Blob Storage:

```bash
# Backup script (backup_db.sh)
#!/bin/bash
BACKUP_DIR="/tmp/backups"
DB_PATH="/app/data/lawyer_bot.db"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="lawyer_bot_${TIMESTAMP}.db.gz"

mkdir -p $BACKUP_DIR
gzip -c $DB_PATH > $BACKUP_DIR/$BACKUP_FILE

# Upload to Azure Blob Storage
az storage blob upload \
  --account-name <storage-account> \
  --container-name lawyer-bot-backups \
  --name $BACKUP_FILE \
  --file $BACKUP_DIR/$BACKUP_FILE \
  --auth-mode login

# Clean up local backup
rm $BACKUP_DIR/$BACKUP_FILE

# Keep only last 7 days of backups
az storage blob list \
  --account-name <storage-account> \
  --container-name lawyer-bot-backups \
  --query "[?properties.lastModified < '$(date -d '7 days ago' +%Y-%m-%d)'].name" \
  --output tsv | xargs -I {} az storage blob delete \
  --account-name <storage-account> \
  --container-name lawyer-bot-backups \
  --name {}
```

### Automated Backup Schedule

- **Frequency**: Daily backups at 2:00 AM UTC
- **Retention**: 7 days of daily backups + 4 weekly backups
- **Storage**: Azure Blob Storage (LRS - Locally Redundant Storage)
- **Cost**: ~$0.02/GB/month for storage

### Azure Blob Storage Setup

```bash
# Create storage account
az storage account create \
  --name <lawyerbotstorage> \
  --resource-group <resource-group> \
  --location eastus \
  --sku Standard_LRS \
  --kind StorageV2

# Create container for backups
az storage container create \
  --account-name <lawyerbotstorage> \
  --name lawyer-bot-backups
```

## Application Rollback Strategy

### Azure Web App Deployment Slots

Use deployment slots for zero-downtime deployments:

```bash
# Create staging slot
az webapp deployment slot create \
  --resource-group <resource-group> \
  --name lawyer-bot \
  --slot staging

# Deploy to staging slot
az webapp deployment source sync \
  --resource-group <resource-group> \
  --name lawyer-bot \
  --slot staging

# Test staging slot
# Access: https://lawyer-bot-staging.azurewebsites.net

# Swap slots (promote staging to production)
az webapp deployment slot swap \
  --resource-group <resource-group> \
  --name lawyer-bot \
  --slot staging \
  --target-slot production
```

### Rollback Procedure

If issues occur after deployment:

```bash
# Immediate rollback - swap back
az webapp deployment slot swap \
  --resource-group <resource-group> \
  --name lawyer-bot \
  --slot production \
  --target-slot staging

# Or rollback to previous deployment
az webapp deployment list-publishing-profiles \
  --resource-group <resource-group> \
  --name lawyer-bot

# Redeploy previous version
az webapp deployment source sync \
  --resource-group <resource-group> \
  --name lawyer-bot \
  --revision <previous-revision>
```

### Git-Based Version Control

Tag each production deployment:

```bash
# Tag production deployment
git tag -a v1.0.0-prod -m "Production deployment v1.0.0"
git push origin v1.0.0-prod

# Rollback to previous tag
git checkout v0.9.0-prod
# Deploy this version
```

## Monitoring and Alerting

### Azure Application Insights

```python
# Add to pyproject.toml dependencies
"applicationinsights>=0.11.10"

# Initialize in application
from applicationinsights import TelemetryClient
telemetry = TelemetryClient('<instrumentation-key>')

# Track exceptions
try:
    # Your code
except Exception as e:
    telemetry.track_exception()
```

### Alert Configuration

Set up alerts in Azure Portal:

1. **Error Rate Alert**: > 5% error rate for 5 minutes
2. **Response Time Alert**: > 3 second response time for 5 minutes
3. **CPU Usage Alert**: > 80% CPU for 10 minutes
4. **Memory Usage Alert**: > 90% memory for 10 minutes

### Log Retention

- **Application Logs**: 30 days retention
- **Error Logs**: 90 days retention
- **Access Logs**: 7 days retention

## Disaster Recovery

### Recovery Time Objective (RTO)

- **Database Recovery**: 15 minutes
- **Application Recovery**: 5 minutes (slot swap)
- **Full Recovery**: 30 minutes

### Recovery Point Objective (RPO)

- **Data Loss**: Maximum 24 hours (daily backups)
- **Critical Data**: Maximum 1 hour (with frequent backups)

### Geographic Redundancy

For critical production deployments:

1. **Multi-Region Deployment**: Deploy to two Azure regions
2. **Traffic Manager**: Use Azure Traffic Manager for failover
3. **Database Replication**: Consider PostgreSQL with replication for multi-region

## Pre-Deployment Checklist

Before deploying to production:

- [ ] All tests pass locally
- [ ] Database backup completed
- [ ] Staging slot tested successfully
- [ ] Rollback procedure documented
- [ ] Monitoring configured
- [ ] Team notified of deployment
- [ ] Deployment window scheduled (low-traffic hours)

## Post-Deployment Verification

After deploying to production:

- [ ] Health check endpoint responds
- [ ] Error rate is normal (< 1%)
- [ ] Response time is acceptable (< 1s)
- [ ] Database connectivity verified
- [ ] Sample user flows tested
- [ ] Monitoring alerts verified

## Emergency Contacts

- **DevOps Lead**: [contact]
- **Developer**: [contact]
- **Azure Support**: [Azure support portal]

## Cost Considerations

- **Blob Storage**: ~$0.02/GB/month
- **Application Insights**: Free tier included
- **Deployment Slots**: Included in Standard tier
- **Traffic Manager**: ~$0.75/month per endpoint

## Security Considerations

- **Backup Encryption**: Enable encryption at rest
- **Access Control**: Use Azure RBAC for backup access
- **Key Management**: Use Azure Key Vault for secrets
- **Network Security**: VNET integration for secure access
