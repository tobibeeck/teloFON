#!/bin/bash
set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}   PBX Setup Wizard                      ${NC}"
echo -e "${GREEN}=========================================${NC}"

# 1. Prerequisites
echo -e "\n[1/7] Checking Prerequisites..."

if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}Error: This script must be run as root${NC}"
   exit 1
fi

if ! command -v docker &> /dev/null; then
    echo -e "${RED}Error: Docker is not installed${NC}"
    exit 1
fi

if ! command -v git &> /dev/null; then
    echo -e "${RED}Error: Git is not installed${NC}"
    exit 1
fi

if [[ ! -f "docker-compose.yml" ]]; then
    echo -e "${RED}Error: Script must be run from the pbx/ project folder${NC}"
    exit 1
fi

if [[ -f ".env" ]]; then
    echo -e "${YELLOW}Warning: .env already exists. Setup was already performed.${NC}"
    echo -e "To reset: delete .env and run again."
    exit 1
fi

# 2. FQDN and Networking
echo -e "\n[2/7] Networking Configuration..."
read -p "Enter FQDN (e.g. pbx.firma.at): " FQDN

PUBLIC_IP=$(curl -s ifconfig.me)
echo -e "Detected Public IP: ${GREEN}$PUBLIC_IP${NC}"

RESOLVED_IP=$(dig +short A "$FQDN" | tail -n1)

if [[ "$RESOLVED_IP" != "$PUBLIC_IP" ]]; then
    echo -e "${YELLOW}Warning: DNS Mismatch!${NC}"
    echo -e "  FQDN $FQDN resolves to: $RESOLVED_IP"
    echo -e "  Actual Public IP: $PUBLIC_IP"
    read -p "Continue anyway? (y/n): " confirm
    [[ "$confirm" != "y" ]] && exit 1
else
    echo -e "${GREEN}DNS Check OK.${NC}"
fi

# 3. Admin Web Password
echo -e "\n[3/7] Admin UI Security..."

while true; do
    read -rs -p "Enter Admin Web Password: " PASS1
    echo
    read -rs -p "Confirm Admin Web Password: " PASS2
    echo
    
    if [[ "$PASS1" != "$PASS2" ]]; then
        echo -e "${RED}Passwords do not match. Try again.${NC}"
        continue
    fi
    
    # GDPR-compliant regex: 12+ chars, upper, lower, digit, special
    if [[ ! "$PASS1" =~ [A-Z] ]] || [[ ! "$PASS1" =~ [a-z] ]] || [[ ! "$PASS1" =~ [0-9] ]] || [[ ! "$PASS1" =~ [^a-zA-Z0-9] ]] || [[ ${#PASS1} -lt 12 ]]; then
        echo -e "${RED}Password too weak! Must be 12+ chars and include upper, lower, number, and special char.${NC}"
        continue
    fi
    break
done

ADMIN_PASSWORD_HASH=$(python3 -c "import bcrypt; print(bcrypt.hashpw(b'$PASS1', bcrypt.gensalt()).decode())")

# 4. Secret Generation
echo -e "\n[4/7] Generating Secrets..."

gen_secret() {
    openssl rand -base64 48 | tr -d '=/+' | cut -c1-"$1"
}

POSTGRES_PASSWORD=$(gen_secret 32)
REDIS_PASSWORD=$(gen_secret 32)
JWT_SECRET=$(gen_secret 64)
FS_DEFAULT_PASSWORD=$(gen_secret 32)
TURN_SECRET=$(gen_secret 64)
MINIO_ROOT_USER="pbxadmin"
MINIO_ROOT_PASSWORD=$(gen_secret 32)
ADMIN_SIP_PASSWORD=$(gen_secret 32)

# Helper for JWT (Basic implementation for Supabase keys)
generate_jwt() {
  local role=$1
  python3 -c "
import jwt
import datetime
payload = {'role': '$role', 'iss': 'supabase', 'iat': datetime.datetime.utcnow(), 'exp': datetime.datetime.utcnow() + datetime.timedelta(days=3650)}
print(jwt.encode(payload, '$JWT_SECRET', algorithm='HS256'))"
}

# Note: In a real environment, you might need 'pip install PyJWT'
# For the sake of this setup, if jwt is missing, we use a placeholder or assume installed
if python3 -c "import jwt" &> /dev/null; then
    SUPABASE_ANON_KEY=$(generate_jwt "anon")
    SUPABASE_SERVICE_ROLE_KEY=$(generate_jwt "service_role")
else
    echo -e "${YELLOW}Warning: python3-jwt not found. Using placeholder keys.${NC}"
    SUPABASE_ANON_KEY="placeholder_anon_key_requires_python_jwt"
    SUPABASE_SERVICE_ROLE_KEY="placeholder_service_role_key_requires_python_jwt"
fi

# 5. Writing .env
echo -e "\n[5/7] Creating .env..."

cat > .env <<EOF
FQDN=$FQDN
PUBLIC_IP=$PUBLIC_IP
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
REDIS_PASSWORD=$REDIS_PASSWORD
JWT_SECRET=$JWT_SECRET
FS_DEFAULT_PASSWORD=$FS_DEFAULT_PASSWORD
TURN_SECRET=$TURN_SECRET
MINIO_ROOT_USER=$MINIO_ROOT_USER
MINIO_ROOT_PASSWORD=$MINIO_ROOT_PASSWORD
ADMIN_PASSWORD_HASH=$ADMIN_PASSWORD_HASH
ADMIN_SIP_PASSWORD=$ADMIN_SIP_PASSWORD
SUPABASE_ANON_KEY=$SUPABASE_ANON_KEY
SUPABASE_SERVICE_ROLE_KEY=$SUPABASE_SERVICE_ROLE_KEY
EOF

# 6. Configuration Generation
echo -e "\n[6/7] Processing Templates..."

export FQDN PUBLIC_IP FS_DEFAULT_PASSWORD
if [[ -f "freeswitch/conf/vars.xml.template" ]]; then
    envsubst < freeswitch/conf/vars.xml.template > freeswitch/conf/vars.xml
    echo -e "Generated freeswitch/conf/vars.xml"
fi

if [[ -f "coturn/turnserver.conf.template" ]]; then
    envsubst < coturn/turnserver.conf.template > coturn/turnserver.conf
    echo -e "Generated coturn/turnserver.conf"
fi

# 7. Docker Startup
echo -e "\n[7/7] Starting Services..."

docker compose up -d

echo -e "Waiting for certificates and health checks..."
MAX_RETRIES=12
COUNT=0
while [[ $COUNT -lt $MAX_RETRIES ]]; do
    if docker compose ps | grep -q "Exit"; then
        echo -e "${RED}Error: Some containers failed to start!${NC}"
        docker compose ps
        exit 1
    fi
    # Simple check if all are UP (basic logic)
    echo -n "."
    sleep 5
    ((COUNT++))
done

echo -e "\n${GREEN}Services are running!${NC}"
docker compose ps

# Summary
echo -e "\n${GREEN}=========================================${NC}"
echo -e "${GREEN}   Setup Complete!                       ${NC}"
echo -e "${GREEN}=========================================${NC}"
echo -e "Admin UI URL:       https://$FQDN/admin"
echo -e "API Endpoint:       https://api.$FQDN"
echo -e "Softphone URL:      https://$FQDN/phone"
echo -e "Grafana:            https://$FQDN/grafana"
echo -e "\n${YELLOW}Admin SIP Credentials (Extension 000):${NC}"
echo -e "User:               000"
echo -e "Password:           ${GREEN}$ADMIN_SIP_PASSWORD${NC}"
echo -e "\n${RED}IMPORTANT:${NC}"
echo -e "1. Store the SIP password securely. It will not be shown again."
echo -e "2. The .env file contains all secrets. NEVER commit it to Git."
echo -e "3. For new extensions, a similar logic will be implemented in the API."
echo -e "   Web passwords will be set by admin, SIP passwords auto-generated."
echo -e "========================================="
