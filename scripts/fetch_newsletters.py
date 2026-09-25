#!/usr/bin/env python3
"""
fetch_newsletters.py - Lector de Newsletters de Gmail vía IMAP para Trading Radar.
Optimizado para leer las etiquetas/carpetas de newsletters del usuario (ej. Newsletters/Crypto).
"""

import imaplib
import email
from email.header import decode_header
from html.parser import HTMLParser
import json
import os
import sys
import re
import argparse

CONFIG_PATH = os.path.expanduser("~/.gemini/antigravity-cli/gmail_config.json")
LOCAL_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")

class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reset()
        self.strict = False
        self.convert_charrefs = True
        self.text = []
        self.ignore_tags = {'script', 'style', 'head', 'meta', 'link'}
        self.current_tag = None

    def handle_starttag(self, tag, attrs):
        self.current_tag = tag.lower()
        if self.current_tag in ('p', 'br', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'tr'):
            self.text.append('\n')

    def handle_endtag(self, tag):
        if tag.lower() in ('p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'tr'):
            self.text.append('\n')
        self.current_tag = None

    def handle_data(self, data):
        if self.current_tag not in self.ignore_tags:
            cleaned = data.strip()
            if cleaned:
                self.text.append(data)

    def get_text(self):
        raw = "".join(self.text)
        lines = [line.strip() for line in raw.splitlines()]
        clean_lines = [line for line in lines if line]
        return "\n".join(clean_lines)

def load_credentials():
    # 1. Chequear .env local primero
    if os.path.exists(LOCAL_ENV_PATH):
        try:
            with open(LOCAL_ENV_PATH, "r", encoding="utf-8") as f:
                user, password = None, None
                for line in f:
                    line = line.strip()
                    if line.startswith("GMAIL_USER="):
                        user = line.split("=", 1)[1].strip().strip('"').strip("'")
                    elif line.startswith("GMAIL_APP_PASSWORD="):
                        password = line.split("=", 1)[1].strip().strip('"').strip("'").replace(" ", "")
                if user and password and "PEGA_AQUI" not in password:
                    return user, password
        except Exception:
            pass

    # 2. Chequear config global en ~/.gemini/antigravity-cli/gmail_config.json
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                pwd = data.get("app_password", "").replace(" ", "")
                if data.get("email") and pwd and "PEGA_AQUI" not in pwd:
                    return data.get("email"), pwd
        except Exception:
            pass

    # 3. Chequear Variables de Entorno
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if user and password and "PEGA_AQUI" not in password:
        return user, password.replace(" ", "")

    return None, None

def decode_mime_str(header_value):
    if not header_value:
        return ""
    decoded_fragments = decode_header(header_value)
    text = ""
    for frag, encoding in decoded_fragments:
        if isinstance(frag, bytes):
            try:
                text += frag.decode(encoding or "utf-8", errors="replace")
            except Exception:
                text += frag.decode("latin-1", errors="replace")
        else:
            text += str(frag)
    return text

def extract_body(msg):
    text_content = ""
    html_content = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))

            if "attachment" in content_disposition:
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            charset = part.get_content_charset() or "utf-8"
            try:
                decoded_str = payload.decode(charset, errors="replace")
            except Exception:
                decoded_str = payload.decode("latin-1", errors="replace")

            if content_type == "text/plain" and not text_content:
                text_content = decoded_str
            elif content_type == "text/html" and not html_content:
                html_content = decoded_str
    else:
        content_type = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                decoded_str = payload.decode(charset, errors="replace")
            except Exception:
                decoded_str = payload.decode("latin-1", errors="replace")

            if content_type == "text/plain":
                text_content = decoded_str
            elif content_type == "text/html":
                html_content = decoded_str

    if text_content:
        return text_content.strip()
    elif html_content:
        parser = HTMLTextExtractor()
        parser.feed(html_content)
        return parser.get_text().strip()
    return ""

def list_folders(mail):
    status, folders = mail.list()
    out = []
    if status == "OK":
        for f in folders:
            decoded = f.decode("utf-8", errors="replace")
            # formato usual: (\Flags) "/" "FolderName"
            parts = decoded.split(' "/" ')
            if len(parts) == 2:
                name = parts[1].strip('"')
                out.append(name)
            else:
                out.append(decoded)
    return out

def fetch_emails(folder="Newsletters/Crypto", query=None, sender=None, limit=5, test_only=False, list_all_folders=False, output_format="json"):
    user, password = load_credentials()
    if not user or not password:
        print(json.dumps({
            "status": "error",
            "message": "Credenciales válidas no encontradas. Configura .env o ~/.gemini/antigravity-cli/gmail_config.json"
        }, indent=2))
        sys.exit(1)

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(user, password)
    except Exception as e:
        print(json.dumps({
            "status": "error",
            "message": f"Error de autenticación IMAP: {str(e)}"
        }, indent=2))
        sys.exit(1)

    if test_only:
        print(json.dumps({
            "status": "success",
            "message": f"Conexión exitosa a Gmail IMAP para {user}."
        }, indent=2))
        mail.logout()
        return

    if list_all_folders:
        folders = list_folders(mail)
        print(json.dumps({
            "status": "success",
            "folders": folders
        }, indent=2))
        mail.logout()
        return

    # Formatear nombre de carpeta con comillas si contiene espacios o barras
    folder_quoted = f'"{folder}"' if not (folder.startswith('"') and folder.endswith('"')) else folder
    status, count = mail.select(folder_quoted, readonly=True)
    if status != "OK":
        # Intento fallback a INBOX
        status, count = mail.select("INBOX", readonly=True)
        if status != "OK":
            print(json.dumps({
                "status": "error",
                "message": f"No se pudo acceder a la carpeta '{folder}' ni a INBOX."
            }, indent=2))
            mail.logout()
            sys.exit(1)
        folder = "INBOX"

    # Construir búsqueda
    search_criteria = []
    if sender:
        search_criteria.append(f'FROM "{sender}"')
    if query:
        search_criteria.append(f'TEXT "{query}"')

    search_command = " ".join(search_criteria) if search_criteria else 'ALL'

    status, messages = mail.search(None, search_command)
    if status != "OK" or not messages[0]:
        print(json.dumps({
            "status": "success",
            "folder": folder,
            "count": 0,
            "emails": [],
            "message": f"No se encontraron correos con el filtro: {search_command} en la carpeta '{folder}'"
        }, indent=2))
        mail.logout()
        return

    msg_ids = messages[0].split()
    selected_ids = msg_ids[-limit:]
    selected_ids.reverse()

    results = []
    for mid in selected_ids:
        res, data = mail.fetch(mid, "(RFC822)")
        if res != "OK":
            continue

        raw_email = data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject = decode_mime_str(msg.get("Subject", ""))
        from_hdr = decode_mime_str(msg.get("From", ""))
        date_hdr = decode_mime_str(msg.get("Date", ""))
        body = extract_body(msg)

        # Snippet limpio para visualización rápida
        clean_preview = " ".join(body.split())[:300]

        results.append({
            "id": mid.decode(),
            "subject": subject,
            "from": from_hdr,
            "date": date_hdr,
            "snippet": clean_preview,
            "content": body[:4000],  # Primeros 4000 caracteres para análisis de sentimiento/catalizadores
            "full_length": len(body)
        })

    mail.logout()

    if output_format == "md":
        print(f"## 📬 Newsletters en `{folder}` ({len(results)} correos analizados)\n")
        for i, em in enumerate(results, 1):
            print(f"### {i}. {em['subject']}")
            print(f"- **De:** `{em['from']}`")
            print(f"- **Fecha:** {em['date']}")
            print(f"- **Extracto:** {em['snippet']}...\n")
    else:
        output = {
            "status": "success",
            "folder": folder,
            "count": len(results),
            "query_used": search_command,
            "emails": results
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lector de Newsletters Gmail IMAP")
    parser.add_argument("--test", action="store_true", help="Probar autenticación")
    parser.add_argument("--list-folders", action="store_true", help="Listar todas las etiquetas/carpetas")
    parser.add_argument("--folder", type=str, default="Newsletters/Crypto", help="Carpeta a consultar (default: Newsletters/Crypto)")
    parser.add_argument("--sender", type=str, default="", help="Filtrar por remitente")
    parser.add_argument("--query", type=str, default="", help="Buscar texto específico")
    parser.add_argument("--limit", type=int, default=5, help="Cantidad de correos a traer")
    parser.add_argument("--format", type=str, choices=["json", "md"], default="json", help="Formato de salida (json o md)")
    args = parser.parse_args()

    fetch_emails(folder=args.folder, query=args.query, sender=args.sender, limit=args.limit, test_only=args.test, list_all_folders=args.list_folders, output_format=args.format)
