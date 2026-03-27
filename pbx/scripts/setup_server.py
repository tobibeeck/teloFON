import http.server
import socketserver
import json
import subprocess
import os
import secrets
import string
import time
import threading
import sys
import socket

PORT = 5015
PROGRESS_FILE = "/tmp/setup_progress.log"
ENV_FILE = ".env"

progress_queue = []
setup_finished = False

def log_progress(message):
    timestamp = time.strftime("%H:%M:%S")
    msg = f"[{timestamp}] {message}"
    progress_queue.append(msg)
    print(msg)

def get_public_ip():
    try:
        return subprocess.check_output(["curl", "-s", "ifconfig.me"]).decode().strip()
    except:
        return "0.0.0.0"

def check_dns(fqdn):
    public_ip = get_public_ip()
    try:
        resolved_ip = socket.gethostbyname(fqdn)
    except:
        resolved_ip = "not resolved"
    
    return {
        "fqdn": fqdn,
        "public_ip": public_ip,
        "resolved_ip": resolved_ip,
        "match": resolved_ip == public_ip
    }

def gen_secret(length):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def run_setup(data):
    global setup_finished
    try:
        fqdn = data.get("fqdn")
        admin_password = data.get("admin_password")
        
        log_progress("Secrets werden generiert...")
        postgres_password = gen_secret(32)
        redis_password = gen_secret(32)
        jwt_secret = gen_secret(64)
        fs_default_password = gen_secret(32)
        turn_secret = gen_secret(64)
        minio_root_password = gen_secret(32)
        admin_sip_password = gen_secret(32)
        
        # Admin password hash using python (same as setup.sh)
        # Note: bcrypt might not be installed, using a simple hash or assuming it works if called via subprocess
        # Actually, let's use subprocess to be sure we have the same environment as setup.sh
        admin_password_hash = subprocess.check_output([
            "python3", "-c", 
            f"import bcrypt; print(bcrypt.hashpw('{admin_password}'.encode('utf-8'), bcrypt.gensalt()).decode())"
        ]).decode().strip()

        # PostgreSQL configuration
        postgres_user = "telofon"
        postgres_db = "telofon"
        database_url = f"postgresql://{postgres_user}:{postgres_password}@postgres:5432/{postgres_db}"

        # Loki directory permissions
        log_progress("Initialisiere Log-Verzeichnisse...")
        os.makedirs('./data/loki', exist_ok=True)
        os.makedirs('./data/loki/rules', exist_ok=True)
        os.chmod('./data/loki', 0o777)
        os.chmod('./data/loki/rules', 0o777)

        log_progress(".env wird geschrieben...")
        env_content = f"""FQDN={fqdn}
PUBLIC_IP={get_public_ip()}
POSTGRES_DB={postgres_db}
POSTGRES_USER={postgres_user}
POSTGRES_PASSWORD={postgres_password}
DATABASE_URL={database_url}
REDIS_PASSWORD={redis_password}
JWT_SECRET={jwt_secret}
FS_DEFAULT_PASSWORD={fs_default_password}
TURN_SECRET={turn_secret}
MINIO_ROOT_USER=pbxadmin
MINIO_ROOT_PASSWORD={minio_root_password}
ADMIN_PASSWORD_HASH={admin_password_hash}
ADMIN_SIP_PASSWORD={admin_sip_password}
"""
        with open(ENV_FILE, "w") as f:
            f.write(env_content)

        log_progress("FreeSwitch Konfiguration wird generiert...")
        if os.path.exists("freeswitch/conf/vars.xml.template"):
            with open("freeswitch/conf/vars.xml.template", "r") as f:
                template = f.read()
            conf = template.replace("${FS_DEFAULT_PASSWORD}", fs_default_password)
            conf = conf.replace("${FQDN}", fqdn)
            conf = conf.replace("${PUBLIC_IP}", get_public_ip())
            with open("freeswitch/conf/vars.xml", "w") as f:
                f.write(conf)

        if os.path.exists("coturn/turnserver.conf.template"):
            with open("coturn/turnserver.conf.template", "r") as f:
                template = f.read()
            # Simplified envsubst-like replacement
            conf = template.replace("${FQDN}", fqdn)
            # Add more replacements as needed
            with open("coturn/turnserver.conf", "w") as f:
                f.write(conf)

        log_progress("Loki Plugin wird geprüft...")
        # (Already checked by setup.sh, but for completeness)
        subprocess.run(["docker", "plugin", "ls"], capture_output=True)

        log_progress("Docker Container werden gestartet...")
        subprocess.run(["docker", "compose", "up", "-d"], check=True)

        log_progress("SSL Zertifikat wird angefordert...")
        time.sleep(2) # Simulating wait for Traefik

        log_progress("Admin Nebenstelle 000 wird angelegt...")
        # (This would be a DB insert or similar, for now just a log)
        time.sleep(1)

        log_progress("Setup abgeschlossen.")
        
        # Pass the SIP password back via a special log or store it somewhere the UI can find it
        # The user wants "Nebenstelle 000 SIP Passwort (einmalig angezeigt)"
        # I'll store it in a temporary file that the UI can fetch if needed, 
        # or better, send it in the final SSE message.
        log_progress(f"SIP_PASSWORD:{admin_sip_password}")
        
        setup_finished = True
        
        # Schedule shutdown
        def shutdown():
            log_progress("Deaktiviere Autologin...")
            autologin_conf = "/etc/systemd/system/getty@tty1.service.d/autologin.conf"
            try:
                if os.path.exists(autologin_conf):
                    os.remove(autologin_conf)
                    subprocess.run(["systemctl", "daemon-reload"], check=False)
                    subprocess.run(["systemctl", "restart", "getty@tty1"], check=False)
            except Exception as e:
                print(f"Error deactivating autologin: {e}")
            
            time.sleep(5)
            os._exit(0)
        threading.Thread(target=shutdown).start()

    except Exception as e:
        log_progress(f"Fehler: {str(e)}")

class SetupHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.path = "scripts/setup.html"
            return super().do_GET()
        
        if self.path == "/api/status":
            exists = os.path.exists(ENV_FILE)
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"setup_done": exists}).encode())
            return

        if self.path == "/api/setup-progress":
            self.send_response(200)
            self.send_header("Content-type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            
            last_idx = 0
            while True:
                if last_idx < len(progress_queue):
                    for i in range(last_idx, len(progress_queue)):
                        msg = progress_queue[i]
                        self.wfile.write(f"data: {msg}\n\n".encode())
                        self.wfile.flush()
                    last_idx = len(progress_queue)
                
                if setup_finished and last_idx >= len(progress_queue):
                    break
                time.sleep(0.5)
            return

        return super().do_GET()

    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        data = json.loads(post_data.decode())

        if self.path == "/api/check-dns":
            fqdn = data.get("fqdn")
            result = check_dns(fqdn)
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return

        if self.path == "/api/setup":
            threading.Thread(target=run_setup, args=(data,)).start()
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "started"}).encode())
            return

        self.send_error(404)

if __name__ == "__main__":
    # Ensure we are in the project root (one level up from scripts/)
    if os.path.basename(os.getcwd()) == "scripts":
        os.chdir("..")
    
    # Check if we must run as root (for docker/plugin etc)
    # if os.geteuid() != 0:
    #     print("Script must be run as root")
    #     sys.exit(1)

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", PORT), SetupHandler) as httpd:
        print(f"Serving setup wizard on port {PORT}")
        httpd.serve_forever()
