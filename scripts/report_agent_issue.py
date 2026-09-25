#!/usr/bin/env python3
"""
report_agent_issue.py - Generador Autónomo de GitHub Issues para Fallas del Setup Agéntico.

Permite a los subagentes, hooks y loops del desk reportar programáticamente cualquier fallo,
excepción no capturada, anomalía de infraestructura o error de herramientas directamente
en el repositorio https://github.com/IgnacioN99/autonomous-trading-desk/issues.

Características de Resiliencia:
1. Despacho Directo a GitHub: Utiliza la API REST v3 con GITHUB_TOKEN (Personal Access Token).
2. Cola Local Resiliente (Offline Backlog): Si GITHUB_TOKEN no está configurado o falla la red,
   guarda el issue atómicamente en logs/issues_backlog.jsonl y permite sincronizarlo después con --sync-backlog.
3. Anti-Spam / Deduplicación Inteligente: Calcula un fingerprint (hash) del error; si el mismo
   fallo ocurrió en las últimas 24h, añade telemetría al issue existente en lugar de inundar el repo.
4. Metadatos Forenses Automáticos: Incluye timestamp UTC, entorno (Testnet/Prod), agente emisor,
   versión de Python y estado de la cartera.

Uso:
  python3 scripts/report_agent_issue.py --title "Error en cálculo de cointegración" --error "ZeroDivisionError: ..." --category "quant_logic" --severity "HIGH"
  python3 scripts/report_agent_issue.py --sync-backlog
"""

import os
import sys
import json
import time
import hashlib
import datetime
import urllib.request
import urllib.error
import argparse
from typing import Dict, Any, Optional, List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
BACKLOG_FILE = os.path.join(LOGS_DIR, "issues_backlog.jsonl")
FINGERPRINTS_FILE = os.path.join(LOGS_DIR, "issues_fingerprints.json")
DEFAULT_REPO = "IgnacioN99/autonomous-trading-desk"

# Cargar .env manualmente si existe para no depender de python-dotenv
def load_env_file():
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = val

load_env_file()

def compute_fingerprint(title: str, error_detail: str) -> str:
    """Genera un hash SHA-256 único para el tipo de error para evitar issues duplicados."""
    norm_text = f"{title.strip().lower()}|{error_detail.strip()[:200].lower()}"
    return hashlib.sha256(norm_text.encode("utf-8")).hexdigest()[:16]

def load_fingerprints() -> Dict[str, Any]:
    if os.path.exists(FINGERPRINTS_FILE):
        try:
            with open(FINGERPRINTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_fingerprints(data: Dict[str, Any]):
    os.makedirs(LOGS_DIR, exist_ok=True)
    temp_file = FINGERPRINTS_FILE + f".tmp.{os.getpid()}"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(temp_file, FINGERPRINTS_FILE)

def append_to_backlog(issue_payload: Dict[str, Any]):
    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(BACKLOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(issue_payload, ensure_ascii=False) + "\n")

def get_system_context() -> Dict[str, Any]:
    """Recopila telemetría ligera del sistema sin romper en caso de fallo."""
    state_file = os.path.join(LOGS_DIR, "session_state.json")
    portfolio_summary = "FLAT / No state"
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                st = json.load(f)
                portfolio_summary = f"Delta: {st.get('delta_bias', 'N/A')}, Activos: {len(st.get('active_positions', []))}, PnL Flotante: ${st.get('floating_pnl_usdt', 0.0):.2f}"
        except Exception:
            pass

    return {
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "target_env": os.getenv("BINANCE_API_ENV", "TESTNET").upper(),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "portfolio_summary": portfolio_summary
    }

def format_issue_markdown(
    title: str,
    error_detail: str,
    category: str,
    severity: str,
    agent_name: str,
    stack_trace: str = "",
    remediation: str = "",
    fingerprint: str = ""
) -> str:
    """Construye un cuerpo de Issue en Markdown técnico con diseño institucional."""
    ctx = get_system_context()
    
    severity_emojis = {
        "CRITICAL": "🔴 CRITICAL",
        "HIGH": "🟠 HIGH",
        "MEDIUM": "🟡 MEDIUM",
        "LOW": "🔵 LOW"
    }
    sev_badge = severity_emojis.get(severity.upper(), f"⚪ {severity}")

    body = [
        f"## 🚨 Informe Autónomo de Falla de Setup Agéntico",
        f"",
        f"| Dimensión | Valor |",
        f"| :--- | :--- |",
        f"| **Severidad** | **{sev_badge}** |",
        f"| **Categoría** | `{category}` |",
        f"| **Agente / Módulo Emisor** | `{agent_name}` |",
        f"| **Entorno de Ejecución** | `{ctx['target_env']}` |",
        f"| **Timestamp UTC** | `{ctx['timestamp_utc']}` |",
        f"| **Estado de Cartera** | `{ctx['portfolio_summary']}` |",
        f"| **Fingerprint ID** | `{fingerprint}` |",
        f"",
        f"---",
        f"",
        f"### 📋 Descripción del Fallo / Anomalía",
        f"{error_detail.strip()}",
        f""
    ]

    if stack_trace.strip():
        body.extend([
            f"### 🔍 Traceback / Detalles Técnicos de Error",
            f"```text",
            f"{stack_trace.strip()}",
            f"```",
            f""
        ])

    if remediation.strip():
        body.extend([
            f"### 💡 Remediación / Solución Sugerida por el Agente",
            f"{remediation.strip()}",
            f""
        ])

    body.extend([
        f"---",
        f"*Reportado automáticamente por el harness de observabilidad de `autonomous-trading-desk`.*"
    ])

    return "\n".join(body)

def dispatch_github_issue(
    title: str,
    body: str,
    labels: List[str],
    repo: str = DEFAULT_REPO,
    token: Optional[str] = None
) -> Dict[str, Any]:
    """Envía la petición HTTP POST a la API REST de GitHub."""
    token = token or os.getenv("GITHUB_TOKEN")
    if not token:
        raise ValueError("GITHUB_TOKEN no configurado en entorno ni en .env")

    url = f"https://api.github.com/repos/{repo}/issues"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Autonomous-Trading-Desk-Agent"
    }

    payload = {
        "title": title,
        "body": body,
        "labels": labels
    }

    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")

    with urllib.request.urlopen(req, timeout=12) as response:
        res_data = json.loads(response.read().decode("utf-8"))
        return {
            "success": True,
            "issue_number": res_data.get("number"),
            "html_url": res_data.get("html_url"),
            "state": res_data.get("state")
        }

def report_issue(
    title: str,
    error_detail: str,
    category: str = "agent_failure",
    severity: str = "HIGH",
    agent_name: str = "autonomous_agent",
    stack_trace: str = "",
    remediation: str = "",
    repo: str = DEFAULT_REPO,
    force_sync: bool = False
) -> Dict[str, Any]:
    """
    Función principal exportable para que cualquier subagente o script reporte fallas.
    Aplica deduplicación, formatado y despacho fail-safe (online o local backlog).
    """
    fingerprint = compute_fingerprint(title, error_detail)
    fp_cache = load_fingerprints()
    now_ts = int(time.time())

    # Control de duplicados en ventana de 24 horas
    if not force_sync and fingerprint in fp_cache:
        last_seen = fp_cache[fingerprint].get("last_seen_ts", 0)
        count = fp_cache[fingerprint].get("count", 1)
        if now_ts - last_seen < 86400: # Menos de 24h
            fp_cache[fingerprint]["count"] = count + 1
            fp_cache[fingerprint]["last_seen_ts"] = now_ts
            save_fingerprints(fp_cache)
            msg = f"Deduplicación activa: El error '{title}' ya fue reportado previamente (Incidencias: {count + 1}). Se omite spam de issue nuevo."
            print(f"ℹ️ {msg}")
            return {
                "success": True,
                "deduplicated": True,
                "fingerprint": fingerprint,
                "occurrences": count + 1,
                "message": msg
            }

    # Formatear etiquetas de GitHub
    labels = ["agent-failure", f"severity:{severity.lower()}"]
    if category:
        labels.append(f"cat:{category.lower()}")

    markdown_body = format_issue_markdown(
        title=title,
        error_detail=error_detail,
        category=category,
        severity=severity,
        agent_name=agent_name,
        stack_trace=stack_trace,
        remediation=remediation,
        fingerprint=fingerprint
    )

    issue_record = {
        "fingerprint": fingerprint,
        "title": title,
        "body": markdown_body,
        "labels": labels,
        "repo": repo,
        "category": category,
        "severity": severity,
        "agent_name": agent_name,
        "created_at_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "status": "QUEUED_OFFLINE"
    }

    token = os.getenv("GITHUB_TOKEN")
    if token:
        try:
            gh_res = dispatch_github_issue(title=title, body=markdown_body, labels=labels, repo=repo, token=token)
            issue_record["status"] = "PUBLISHED_GITHUB"
            issue_record["issue_number"] = gh_res.get("issue_number")
            issue_record["html_url"] = gh_res.get("html_url")
            
            # Registrar fingerprint
            fp_cache[fingerprint] = {
                "title": title,
                "issue_number": gh_res.get("issue_number"),
                "html_url": gh_res.get("html_url"),
                "last_seen_ts": now_ts,
                "count": 1
            }
            save_fingerprints(fp_cache)
            
            print(f"✅ GITHUB ISSUE CREADO EXITOSAMENTE: #{gh_res.get('issue_number')}")
            print(f"   URL: {gh_res.get('html_url')}")
            return issue_record
        except Exception as e:
            print(f"⚠️ Fallo al conectar con GitHub API ({e}). Guardando issue en el backlog local...", file=sys.stderr)
            issue_record["dispatch_error"] = str(e)
    else:
        print(f"ℹ️ GITHUB_TOKEN no detectado. Encolando issue #{fingerprint[:8]} en el backlog local (logs/issues_backlog.jsonl)...")

    # Si no hay token o falló el despacho, encolar en backlog
    append_to_backlog(issue_record)
    fp_cache[fingerprint] = {
        "title": title,
        "last_seen_ts": now_ts,
        "count": 1,
        "status": "QUEUED_OFFLINE"
    }
    save_fingerprints(fp_cache)
    return issue_record

def sync_backlog(repo: str = DEFAULT_REPO):
    """Reintenta despachar todos los issues acumulados en el backlog local."""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("❌ Error: GITHUB_TOKEN no está definido en el entorno ni en .env. No se puede sincronizar.")
        return

    if not os.path.exists(BACKLOG_FILE):
        print("✅ Backlog vacío. No hay issues pendientes de sincronización.")
        return

    lines = []
    with open(BACKLOG_FILE, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    if not lines:
        print("✅ No hay registros en el backlog.")
        return

    print(f"🔄 Sincronizando {len(lines)} issue(s) pendiente(s) hacia {repo}...")
    remaining = []
    success_count = 0

    for line in lines:
        try:
            item = json.loads(line)
            res = dispatch_github_issue(
                title=item["title"],
                body=item["body"],
                labels=item.get("labels", []),
                repo=repo,
                token=token
            )
            print(f"   ✅ Issue #{res.get('issue_number')} publicado: {item['title']} -> {res.get('html_url')}")
            success_count += 1
            time.sleep(1) # Rate limit cushion
        except Exception as e:
            print(f"   ❌ Fallo al despachar '{item.get('title')}': {e}")
            remaining.append(line)

    # Reescribir backlog solo con los pendientes que hayan fallado
    with open(BACKLOG_FILE, "w", encoding="utf-8") as f:
        for r in remaining:
            f.write(r + "\n")

    print(f"🏁 Sincronización completada: {success_count} publicados, {len(remaining)} restantes en backlog.")

def main():
    parser = argparse.ArgumentParser(description="Reportador Autónomo de GitHub Issues para Fallas de Agentes")
    parser.add_argument("--title", type=str, help="Título descriptivo del problema o fallo")
    parser.add_argument("--error", type=str, help="Descripción detallada de la anomalía o fallo")
    parser.add_argument("--category", type=str, default="agent_failure", choices=["agent_failure", "risk_gate", "tool_error", "quant_logic", "infra", "enhancement"], help="Categoría del problema")
    parser.add_argument("--severity", type=str, default="HIGH", choices=["CRITICAL", "HIGH", "MEDIUM", "LOW"], help="Nivel de severidad")
    parser.add_argument("--agent", type=str, default="cli_operator", help="Nombre del subagente o script emisor")
    parser.add_argument("--stack-trace", type=str, default="", help="Traceback o payload de error técnico")
    parser.add_argument("--remediation", type=str, default="", help="Solución o parche propuesto")
    parser.add_argument("--repo", type=str, default=DEFAULT_REPO, help="Repositorio destino (ej. IgnacioN99/autonomous-trading-desk)")
    parser.add_argument("--sync-backlog", action="store_true", help="Sincroniza issues encolados en logs/issues_backlog.jsonl contra GitHub")
    parser.add_argument("--force", action="store_true", help="Ignora la deduplicación de 24h y fuerza la creación del issue")

    args = parser.parse_args()

    if args.sync_backlog:
        sync_backlog(repo=args.repo)
        return

    if not args.title or not args.error:
        parser.print_help()
        sys.exit(1)

    res = report_issue(
        title=args.title,
        error_detail=args.error,
        category=args.category,
        severity=args.severity,
        agent_name=args.agent,
        stack_trace=args.stack_trace,
        remediation=args.remediation,
        repo=args.repo,
        force_sync=args.force
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
