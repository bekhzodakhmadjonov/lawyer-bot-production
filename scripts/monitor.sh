#!/bin/bash
# Health Monitoring Script for Lawyer Bot on Oracle Cloud
# Monitors application health, resource usage, and sends alerts

set -e

# Configuration
LOG_FILE="/var/log/lawyer_bot_monitor.log"
ALERT_LOG_FILE="/var/log/lawyer_bot_alerts.log"
HEALTH_CHECK_URL="${HEALTH_CHECK_URL:-http://localhost:8000/health}"
DOMAIN="${DOMAIN:-yourdomain.example.com}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN}"
ADMIN_CHAT_ID="${ADMIN_CHAT_ID}"

# Thresholds
CPU_THRESHOLD=80
MEMORY_THRESHOLD=80
DISK_THRESHOLD=80
ERROR_RATE_THRESHOLD=5
RESPONSE_TIME_THRESHOLD=5

# Log function
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Alert function
alert() {
    local message="🚨 ALERT: $1"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $message" | tee -a "$ALERT_LOG_FILE"
    
    # Send Telegram alert if configured
    if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$ADMIN_CHAT_ID" ]; then
        curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
            -d "chat_id=$ADMIN_CHAT_ID" \
            -d "text=$message" \
            -d "parse_mode=HTML" >> "$LOG_FILE" 2>&1
    fi
}

# Check application health
check_application_health() {
    log "Checking application health..."
    
    # Check if containers are running
    if ! docker ps | grep -q lawyer_bot; then
        alert "Bot container is not running"
        return 1
    fi
    
    if ! docker ps | grep -q lawyer_bot_postgres; then
        alert "PostgreSQL container is not running"
        return 1
    fi
    
    if ! docker ps | grep -q lawyer_bot_caddy; then
        alert "Caddy container is not running"
        return 1
    fi
    
    # Check health endpoint
    local health_response=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$HEALTH_CHECK_URL" || echo "000")
    
    if [ "$health_response" != "200" ]; then
        alert "Health check failed (HTTP $health_response)"
        return 1
    fi
    
    # Check response time
    local response_time=$(curl -s -o /dev/null -w "%{time_total}" --max-time 10 "$HEALTH_CHECK_URL")
    local response_time_ms=$(echo "$response_time * 1000" | bc | cut -d'.' -f1)
    
    if [ "$response_time_ms" -gt "$((RESPONSE_TIME_THRESHOLD * 1000))" ]; then
        alert "Slow response time: ${response_time_ms}ms (threshold: ${RESPONSE_TIME_THRESHOLD}s)"
    fi
    
    log "Application health: OK (response time: ${response_time_ms}ms)"
    return 0
}

# Check database health
check_database_health() {
    log "Checking database health..."
    
    # Check PostgreSQL connection
    if ! docker exec lawyer_bot_postgres pg_isready -U postgres -d lawyer_bot > /dev/null 2>&1; then
        alert "Database is not ready"
        return 1
    fi
    
    # Check database size
    local db_size=$(docker exec lawyer_bot_postgres psql -U postgres -d lawyer_bot -t -c "SELECT pg_size_pretty(pg_database_size('lawyer_bot'));" | xargs)
    log "Database size: $db_size"
    
    # Check active connections
    local connections=$(docker exec lawyer_bot_postgres psql -U postgres -d lawyer_bot -t -c "SELECT count(*) FROM pg_stat_activity;" | xargs)
    log "Active database connections: $connections"
    
    log "Database health: OK"
    return 0
}

# Check resource usage
check_resource_usage() {
    log "Checking resource usage..."
    
    # CPU usage
    local cpu_usage=$(top -bn1 | grep "Cpu(s)" | sed "s/.*, *\([0-9.]*\)%* id.*/\1/" | awk '{print 100 - $1}')
    local cpu_usage_int=${cpu_usage%.*}
    
    if [ "$cpu_usage_int" -gt "$CPU_THRESHOLD" ]; then
        alert "High CPU usage: ${cpu_usage}% (threshold: ${CPU_THRESHOLD}%)"
    else
        log "CPU usage: ${cpu_usage}%"
    fi
    
    # Memory usage
    local memory_usage=$(free | grep Mem | awk '{printf "%.0f", $3/$2 * 100.0}')
    
    if [ "$memory_usage" -gt "$MEMORY_THRESHOLD" ]; then
        alert "High memory usage: ${memory_usage}% (threshold: ${MEMORY_THRESHOLD}%)"
    else
        log "Memory usage: ${memory_usage}%"
    fi
    
    # Disk usage
    local disk_usage=$(df -h / | awk 'NR==2 {print $5}' | sed 's/%//')
    
    if [ "$disk_usage" -gt "$DISK_THRESHOLD" ]; then
        alert "High disk usage: ${disk_usage}% (threshold: ${DISK_THRESHOLD}%)"
    else
        log "Disk usage: ${disk_usage}%"
    fi
}

# Check SSL certificate
check_ssl_certificate() {
    log "Checking SSL certificate..."
    
    local cert_expiry=$(echo | openssl s_client -servername "$DOMAIN" -connect "$DOMAIN:443" 2>/dev/null | openssl x509 -noout -enddate | cut -d= -f2)
    local cert_expiry_date=$(date -d "$cert_expiry" +%s)
    local current_date=$(date +%s)
    local days_until_expiry=$(( (cert_expiry_date - current_date) / 86400 ))
    
    if [ "$days_until_expiry" -lt 7 ]; then
        alert "SSL certificate expires in $days_until_expiry days"
    else
        log "SSL certificate: OK (expires in $days_until_expiry days)"
    fi
}

# Check Docker container logs for errors
check_container_logs() {
    log "Checking container logs for errors..."
    
    # Check bot container logs for errors in last hour
    local error_count=$(docker logs --since 1h lawyer_bot 2>&1 | grep -i "error" | wc -l)
    
    if [ "$error_count" -gt 10 ]; then
        alert "High error rate in bot logs: $error_count errors in last hour"
    else
        log "Bot container errors: $error_count (last hour)"
    fi
    
    # Check PostgreSQL logs for errors
    local pg_error_count=$(docker logs --since 1h lawyer_bot_postgres 2>&1 | grep -i "error" | wc -l)
    
    if [ "$pg_error_count" -gt 5 ]; then
        alert "High error rate in PostgreSQL logs: $pg_error_count errors in last hour"
    else
        log "PostgreSQL container errors: $pg_error_count (last hour)"
    fi
}

# Main monitoring function
main() {
    log "=== Starting health monitoring check ==="
    
    local overall_status=0
    
    # Run all checks
    check_application_health || overall_status=1
    check_database_health || overall_status=1
    check_resource_usage || overall_status=1
    check_ssl_certificate || overall_status=1
    check_container_logs || overall_status=1
    
    log "=== Health monitoring check completed ==="
    
    if [ $overall_status -eq 0 ]; then
        log "All systems operational"
    else
        log "Some checks failed - see alerts above"
    fi
    
    return $overall_status
}

# Run main function
main

exit $?
