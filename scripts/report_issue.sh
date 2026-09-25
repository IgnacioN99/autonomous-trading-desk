#!/usr/bin/env bash
# ==============================================================================
# report_issue.sh - Reportador Nativo en Bash de GitHub Issues para Agentes
# ==============================================================================
# Diseñado para ejecutarse directamente desde el shell del agente (run_command)
# con cero dependencias de Python (solo requiere bash y curl).
#
# Si Python o el entorno virtual colapsan, este script sigue funcionando
# para reportar el incidente en https://github.com/IgnacioN99/autonomous-trading-desk/issues.
#
# Uso:
#   ./scripts/report_issue.sh --title "Fallo en Binance API" --error "Error 429 Too Many Requests" --severity "HIGH"
#   ./scripts/report_issue.sh --sync
# ==============================================================================

set -e

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGS_DIR="${BASE_DIR}/logs"
BACKLOG_FILE="${LOGS_DIR}/issues_backlog.jsonl"
DEFAULT_REPO="IgnacioN99/autonomous-trading-desk"

# 1. Cargar variables de .env si existe y no están exportadas
if [ -f "${BASE_DIR}/.env" ]; then
    while IFS='=' read -r key val || [ -n "$key" ]; do
        # Omitir comentarios y líneas vacías
        [[ "$key" =~ ^#.*$ ]] && continue
        [ -z "$key" ] && continue
        # Limpiar espacios y comillas
        key="$(echo "$key" | tr -d '[:space:]')"
        val="$(echo "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^["'"'"']//' -e 's/["'"'"']$//')"
        if [ -n "$key" ] && [ -z "${!key}" ]; then
            export "$key"="$val"
        fi
    done < "${BASE_DIR}/.env"
fi

REPO="${GITHUB_REPO:-$DEFAULT_REPO}"
TOKEN="${GITHUB_TOKEN:-}"

# Valores por defecto
TITLE=""
ERROR_DETAIL=""
SEVERITY="HIGH"
CATEGORY="agent_failure"
AGENT_NAME="autonomous_agent"
REMEDIATION=""
SYNC_MODE=false

# Parser de argumentos
while [[ $# -gt 0 ]]; do
    case "$1" in
        -t|--title)
            TITLE="$2"
            shift 2
            ;;
        -e|--error|-b|--body)
            ERROR_DETAIL="$2"
            shift 2
            ;;
        -s|--severity)
            SEVERITY="$2"
            shift 2
            ;;
        -c|--category)
            CATEGORY="$2"
            shift 2
            ;;
        -a|--agent)
            AGENT_NAME="$2"
            shift 2
            ;;
        -r|--remediation)
            REMEDIATION="$2"
            shift 2
            ;;
        --repo)
            REPO="$2"
            shift 2
            ;;
        --sync)
            SYNC_MODE=true
            shift
            ;;
        -h|--help)
            echo "Uso: $0 [opciones]"
            echo ""
            echo "Opciones:"
            echo "  -t, --title <texto>       Título del fallo"
            echo "  -e, --error <texto>       Detalle del error o mensaje devuelto"
            echo "  -s, --severity <nivel>    CRITICAL | HIGH | MEDIUM | LOW (default: HIGH)"
            echo "  -c, --category <tipo>     agent_failure | risk_gate | tool_error | infra (default: agent_failure)"
            echo "  -a, --agent <nombre>      Nombre del agente emisor (default: autonomous_agent)"
            echo "  -r, --remediation <texto> Solución o sugerencia"
            echo "  --sync                    Despacha issues pendientes en el backlog local"
            exit 0
            ;;
        *)
            echo "Opción desconocida: $1"
            exit 1
            ;;
    esac
done

mkdir -p "$LOGS_DIR"

# ------------------------------------------------------------------------------
# Función: Sincronizar Backlog Offline
# ------------------------------------------------------------------------------
sync_backlog() {
    if [ -z "$TOKEN" ]; then
        echo "❌ Error: GITHUB_TOKEN no está definido en el entorno ni en .env."
        exit 1
    fi
    if [ ! -f "$BACKLOG_FILE" ] || [ ! -s "$BACKLOG_FILE" ]; then
        echo "✅ El backlog local está vacío. No hay issues pendientes."
        exit 0
    fi

    echo "🔄 Sincronizando issues pendientes hacia https://github.com/${REPO}/issues..."
    TEMP_BACKLOG="${BACKLOG_FILE}.tmp.$$"
    touch "$TEMP_BACKLOG"

    count_success=0
    count_failed=0

    while IFS= read -r line || [ -n "$line" ]; do
        [ -z "$line" ] && continue
        
        # Extraer title y body con node/python/grep de forma segura
        item_title=$(echo "$line" | sed -n 's/.*"title": *\([^,]*\),.*/\1/p' | sed 's/^"//;s/"$//')
        
        http_code=$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST "https://api.github.com/repos/${REPO}/issues" \
            -H "Authorization: Bearer ${TOKEN}" \
            -H "Accept: application/vnd.github+json" \
            -H "User-Agent: Autonomous-Trading-Desk-Bash" \
            -d "$line")

        if [ "$http_code" = "201" ]; then
            echo "   ✅ Publicado exitosamente: ${item_title}"
            ((count_success++))
        else
            echo "   ❌ Error HTTP $http_code al publicar: ${item_title}"
            echo "$line" >> "$TEMP_BACKLOG"
            ((count_failed++))
        fi
        sleep 1
    done < "$BACKLOG_FILE"

    mv "$TEMP_BACKLOG" "$BACKLOG_FILE"
    echo "🏁 Sincronización finalizada: $count_success publicados, $count_failed pendientes."
    exit 0
}

if [ "$SYNC_MODE" = true ]; then
    sync_backlog
fi

# ------------------------------------------------------------------------------
# Validación de Entrada
# ------------------------------------------------------------------------------
if [ -z "$TITLE" ] || [ -z "$ERROR_DETAIL" ]; then
    echo "❌ Error: Parámetros obligatorios faltantes (--title y --error)."
    echo "Ejemplo: $0 --title 'Fallo en Binance API' --error 'HTTP 502 Bad Gateway'"
    exit 1
fi

TIMESTAMP_UTC=$(date -u +"%Y-%m-%d %H:%M:%S UTC")
TARGET_ENV="${BINANCE_API_ENV:-TESTNET}"

# Seleccionar emoji de severidad
case "${SEVERITY^^}" in
    CRITICAL) SEV_BADGE="🔴 CRITICAL" ;;
    HIGH)     SEV_BADGE="🟠 HIGH" ;;
    MEDIUM)   SEV_BADGE="🟡 MEDIUM" ;;
    LOW)      SEV_BADGE="🔵 LOW" ;;
    *)        SEV_BADGE="⚪ $SEVERITY" ;;
esac

# ------------------------------------------------------------------------------
# Construcción del Cuerpo en Markdown
# ------------------------------------------------------------------------------
MD_BODY=$(cat <<EOF
## 🚨 Informe Autónomo de Falla de Setup Agéntico

| Dimensión | Valor |
| :--- | :--- |
| **Severidad** | **${SEV_BADGE}** |
| **Categoría** | \`${CATEGORY}\` |
| **Agente Emisor** | \`${AGENT_NAME}\` |
| **Entorno** | \`${TARGET_ENV}\` |
| **Timestamp UTC** | \`${TIMESTAMP_UTC}\` |

---

### 📋 Descripción del Fallo / Anomalía
${ERROR_DETAIL}
EOF
)

if [ -n "$REMEDIATION" ]; then
    MD_BODY="${MD_BODY}

### 💡 Remediación Sugerida
${REMEDIATION}"
fi

MD_BODY="${MD_BODY}

---
*Reportado nativamente vía shell bash por el harness de observabilidad de \`autonomous-trading-desk\`.*"

# ------------------------------------------------------------------------------
# Serialización JSON del Payload (Segura ante caracteres especiales)
# ------------------------------------------------------------------------------
# Usamos un escape seguro en bash
json_escape() {
    python3 -c 'import json, sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || \
    echo -n "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e ':a' -e 'N' -e '$!ba' -e 's/\n/\\n/g'
}

JSON_TITLE=$(echo -n "$TITLE" | json_escape 2>/dev/null || echo "\"$TITLE\"")
JSON_BODY=$(echo -n "$MD_BODY" | json_escape 2>/dev/null || echo "\"$MD_BODY\"")

PAYLOAD=$(cat <<EOF
{
  "title": ${JSON_TITLE},
  "body": ${JSON_BODY},
  "labels": ["agent-failure", "severity:${SEVERITY,,}", "cat:${CATEGORY,,}"]
}
EOF
)

# ------------------------------------------------------------------------------
# Envío a GitHub API o Fallback a Backlog Local
# ------------------------------------------------------------------------------
if [ -n "$TOKEN" ]; then
    HTTP_RESPONSE=$(curl -s -w "\n%{http_code}" \
        -X POST "https://api.github.com/repos/${REPO}/issues" \
        -H "Authorization: Bearer ${TOKEN}" \
        -H "Accept: application/vnd.github+json" \
        -H "User-Agent: Autonomous-Trading-Desk-Bash" \
        -d "$PAYLOAD")

    HTTP_STATUS=$(echo "$HTTP_RESPONSE" | tail -n1)
    RESPONSE_BODY=$(echo "$HTTP_RESPONSE" | sed '$d')

    if [ "$HTTP_STATUS" = "201" ]; then
        ISSUE_URL=$(echo "$RESPONSE_BODY" | grep -o '"html_url": *"[^"]*"' | head -n1 | cut -d'"' -f4)
        ISSUE_NUM=$(echo "$RESPONSE_BODY" | grep -o '"number": *[0-9]*' | head -n1 | cut -d':' -f2 | tr -d ' ')
        echo "✅ GITHUB ISSUE CREADO EXITOSAMENTE: #${ISSUE_NUM}"
        echo "   URL: ${ISSUE_URL}"
        exit 0
    else
        echo "⚠️ Fallo al conectar con GitHub API (HTTP ${HTTP_STATUS}). Encolando en backlog local..."
    fi
else
    echo "ℹ️ GITHUB_TOKEN no detectado en .env. Encolando issue en backlog local (${BACKLOG_FILE})..."
fi

# Fallback: guardar en backlog local
echo "$PAYLOAD" | tr '\n' ' ' >> "$BACKLOG_FILE"
echo "" >> "$BACKLOG_FILE"
echo "📁 Issue guardado en el backlog local (${BACKLOG_FILE})."
echo "   Para publicarlo cuando configures GITHUB_TOKEN: ./scripts/report_issue.sh --sync"
