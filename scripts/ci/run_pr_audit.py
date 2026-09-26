#!/usr/bin/env python3
"""
scripts/ci/run_pr_audit.py
Master PR Audit Orchestrator for Antigravity Trading Desk.

1. Runs deterministic triage (triage_pr.py) -> logs/pr_manifest.json.
2. Extracts git diff for the PR.
3. Formulates a rigorous Multi-Specialist Prompt grounded in:
   - Trading Risk (.agents/reviewers/trading_risk_reviewer.md)
   - Binance Microstructure (.agents/reviewers/binance_microstructure_reviewer.md)
   - Agentic Harness (.agents/reviewers/agentic_harness_reviewer.md)
   - Prompt Engineering (.agents/reviewers/prompt_engineering_reviewer.md)
4. Invokes the Antigravity agent (agy -p) to audit the diff.
5. Verifies review completeness with verify_review.py (Zero-Hallucination Gate).
"""

import os
import sys
import json
import shutil
import urllib.request
import subprocess
from pathlib import Path


def invoke_auditor(prompt: str) -> str:
    """Invokes the auditor agent via agy CLI (local) or direct Gemini API (cloud CI)."""
    # 1. Prefer local agy CLI if installed and available in PATH
    if shutil.which("agy"):
        agy_cmd = [
            "agy",
            "-p", prompt,
            "--dangerously-skip-permissions",
            "--effort", "high",
        ]
        try:
            print("  -> Usando Antigravity CLI ('agy') local...")
            res = subprocess.run(agy_cmd, capture_output=True, text=True, check=True)
            if res.stdout.strip():
                return res.stdout.strip()
        except Exception as e:
            print(f"  Aviso: agy falló ({e}), intentando fallback a API...")

    # 2. Fallback to Gemini REST API (Standard library, 0 dependencies)
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        print("  -> Usando Gemini REST API directa (Cloud CI mode)...")
        models_to_try = [
            os.getenv("GEMINI_MODEL", "").strip(),
            "gemini-3.8-flash",
            "gemini-3.8-pro",
            "gemini-3.5-flash",
            "gemini-2.0-flash",
        ]
        models_to_try = [m for m in models_to_try if m]

        data = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt}
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 8192
            }
        }
        encoded_data = json.dumps(data).encode("utf-8")

        for model_name in models_to_try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            req = urllib.request.Request(
                url,
                data=encoded_data,
                headers={"Content-Type": "application/json"}
            )
            try:
                print(f"  -> Consultando modelo '{model_name}'...")
                with urllib.request.urlopen(req, timeout=90) as response:
                    res_json = json.loads(response.read().decode("utf-8"))
                    candidates = res_json.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            print(f"  -> Respuesta recibida exitosamente de '{model_name}'.")
                            return parts[0].get("text", "").strip()
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="ignore")
                print(f"  Aviso: HTTP {e.code} con '{model_name}': {err_body[:200]}")
                continue
            except Exception as e:
                print(f"  Aviso: Excepción con '{model_name}': {e}")
                continue

    raise RuntimeError(
        "No se pudo invocar el auditor: no se encontró 'agy' en PATH "
        "ni se pudo obtener respuesta válida con GEMINI_API_KEY."
    )


def run_command(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def get_diff(base_ref: str = "origin/main") -> str:
    """Gets the diff against base_ref or uncommitted working tree changes."""
    # First try branch diff
    res = subprocess.run(["git", "diff", f"{base_ref}...HEAD"], capture_output=True, text=True)
    diff = res.stdout.strip()

    if not diff:
        # Fallback to local working tree diff
        res_work = subprocess.run(["git", "diff", "HEAD"], capture_output=True, text=True)
        diff = res_work.stdout.strip()

    return diff


def build_orchestrator_prompt(manifest: dict, diff: str) -> str:
    required_reviewers = manifest.get("required_reviewers", [])
    
    # Read rubrics for the required reviewers
    rubrics = {}
    for rev in required_reviewers:
        doc_path = Path(manifest["reviewer_details"][rev]["doc"])
        if doc_path.exists():
            with open(doc_path, "r", encoding="utf-8") as f:
                rubrics[rev] = f.read()
        else:
            rubrics[rev] = f"Especialista {rev}: Audita cumplimiento de AGENTS.md en su dominio."

    prompt_parts = [
        "<identity_and_role>",
        "Eres el Lead PR Review Orchestrator de este desk de trading cuantitativo autónomo.",
        "Tu misión es coordinar la auditoría exhaustiva del siguiente código modificado.",
        "</identity_and_role>",
        "",
        "<operational_rules>",
        "1. Debes evaluar el diff de código contra cada una de las siguientes especialidades obligatorias.",
        "2. Es mandatorio generar una sección separada por cada revisor con el encabezado exacto: '### Veredicto: <nombre_revisor>'.",
        "3. El estado de cada revisor debe ser estrictamente '[APROBADO]' o '[CAMBIOS REQUERIDOS]'.",
        "4. Si se rechaza, debes proveer la justificación matemática/técnica y la corrección de código exacta.",
        "</operational_rules>",
        "",
        "<required_specialists_and_rubrics>",
    ]

    for rev in required_reviewers:
        prompt_parts.append(f"<!-- RUBRICA PARA {rev} -->")
        prompt_parts.append(f"Dominio: {manifest['reviewer_details'][rev]['name']}")
        prompt_parts.append(rubrics[rev])
        prompt_parts.append("")

    prompt_parts.extend([
        "</required_specialists_and_rubrics>",
        "",
        "<negative_constraints>",
        f"TIENES ESTRICTAMENTE PROHIBIDO omitir cualquiera de los siguientes revisores requeridos: {required_reviewers}.",
        "Prohibido emitir un veredicto genérico sin evaluar los criterios específicos de cada rúbrica.",
        "Prohibido aprobar código que viole los hard gates de AGENTS.md ($1.50 estándar / $3.75 YOLO, SL atómico, minNotional, friction gate).",
        "</negative_constraints>",
        "",
        "<code_diff_to_audit>",
        diff[:30000] if len(diff) > 30000 else diff,  # Safeguard length
        "</code_diff_to_audit>",
        "",
        "<output_contract>",
        "Genera un informe completo en Markdown que contenga:",
        "# Informe de Auditoría de Pull Request",
        "- **Resumen del PR:** (2 líneas sobre los cambios analizados)",
        "- **Revisores Convocados:** " + ", ".join(required_reviewers),
        "",
        "Para CADA revisor requerido, incluye su bloque:",
        "### Veredicto: <nombre_revisor>",
        "- **Estado:** [APROBADO] o [CAMBIOS REQUERIDOS]",
        "- **Resumen:** ...",
        "- **Hallazgos:**",
        "  - 🟢 Cumplimientos",
        "  - 🟡 Advertencias",
        "  - 🔴 Infracciones críticas",
        "- **Recomendación de Código:** (si aplica)",
        "",
        "### Veredicto Consolidado Final",
        "- **Estado General:** [APROBADO PARA MERGE] o [BLOQUEADO POR CAMBIOS REQUERIDOS]",
        "- **Acción para el Operador Humano:** ...",
        "</output_contract>",
    ])

    return "\n".join(prompt_parts)


def main():
    base_ref = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    report_file = sys.argv[2] if len(sys.argv) > 2 else "review_output.md"

    # Step 1: Run triage
    print("[1/4] Ejecutando Triage Determinístico...")
    triage_cmd = [sys.executable, "scripts/ci/triage_pr.py", base_ref]
    subprocess.run(triage_cmd, check=True)

    manifest_path = Path("logs/pr_manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    if not manifest["changed_files"]:
        print("No hay archivos modificados para auditar.")
        with open(report_file, "w", encoding="utf-8") as f:
            f.write("# Informe de Auditoría de PR\n\nNo se detectaron cambios de código para auditar.\n")
        sys.exit(0)

    # Step 2: Get code diff
    print(f"[2/4] Extrayendo diff de código ({len(manifest['changed_files'])} archivos)...")
    diff = get_diff(base_ref)
    if not diff:
        print("Diff vacío. Finalizando.")
        sys.exit(0)

    # Step 3: Build prompt and invoke auditor agent
    print(f"[3/4] Invocando al Agente Orquestador con {len(manifest['required_reviewers'])} revisores...")
    prompt = build_orchestrator_prompt(manifest, diff)

    review_text = invoke_auditor(prompt)

    with open(report_file, "w", encoding="utf-8") as f:
        f.write(review_text)
    print(f"Reporte generado en: {report_file}")

    # Step 4: Verify review completeness with deterministic gate
    print("[4/4] Verificando cobertura mecánica del reporte...")
    verify_cmd = [sys.executable, "scripts/ci/verify_review.py", str(manifest_path), report_file]
    verify_res = subprocess.run(verify_cmd)
    
    if verify_res.returncode != 0:
        print("❌ FALLO DE AUDITORÍA: El reporte no cumplió con las verificaciones mecánicas.")
        sys.exit(1)

    print("✅ AUDITORÍA EXITOSA: Todos los revisores requeridos aprobaron los cambios.")
    sys.exit(0)


if __name__ == "__main__":
    main()
