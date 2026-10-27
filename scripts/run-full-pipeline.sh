#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export HOST_UID="${HOST_UID:-$(id -u)}" HOST_GID="${HOST_GID:-$(id -g)}"

mkdir -p logs
LOG_FILE="logs/run-full-pipeline_$(date '+%Y%m%d_%H%M%S').log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "Log completo: $LOG_FILE"

declare -a PHASE_NAMES=()
declare -a PHASE_SECS=()

fmt_dur() {
  local s=$1
  printf '%02dh%02dm%02ds' $((s / 3600)) $(((s % 3600) / 60)) $((s % 60))
}

run_phase() {
  local nome="$1"
  shift
  echo
  echo "==> $nome — inicio $(date '+%Y-%m-%d %H:%M:%S')"
  local inicio=$SECONDS
  "$@"
  local dur=$((SECONDS - inicio))
  PHASE_NAMES+=("$nome")
  PHASE_SECS+=("$dur")
  echo "==> $nome — fim $(date '+%Y-%m-%d %H:%M:%S') (durou $(fmt_dur "$dur"))"
}

for f in secrets/chatbot.env secrets/chatbot-common.env secrets/preprocessing.env secrets/evaluation.env; do
  [[ -f "$f" ]] || { echo "Falta $f (secrets locais, não versionados) — copia-o para este PC antes de continuar." >&2; exit 1; }
done

PIPELINE_INICIO=$SECONDS

run_phase "1/4 Preprocessing (containers, ingestao, enriquecimento, embeddings, finalizacao)" ./scripts/run-full-processing.sh

run_phase "2/4 Aplicacao (build + up)" ./scripts/run-application.sh up-build

set -a
source config/chatbot-common.env
source config/chatbot.env
source secrets/evaluation.env
set +a

echo
echo "==> A aguardar o Orchestrator ficar pronto em $ORCHESTRATOR_AVALIACAO_URL"
ESPERA_INICIO=$SECONDS
TENTATIVAS=0
until [[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 -X POST "$ORCHESTRATOR_AVALIACAO_URL" -H 'Content-Type: application/json' -d '{}' 2>/dev/null || echo 000)" =~ ^(200|422)$ ]]; do
  TENTATIVAS=$((TENTATIVAS + 1))
  echo "   ainda nao (tentativa $TENTATIVAS, $(fmt_dur $((SECONDS - ESPERA_INICIO))) decorridos), a tentar de novo em 5s..."
  sleep 5
done
ESPERA_DUR=$((SECONDS - ESPERA_INICIO))
PHASE_NAMES+=("Espera pelo Orchestrator ficar pronto")
PHASE_SECS+=("$ESPERA_DUR")
echo "Orchestrator pronto (esperou $(fmt_dur "$ESPERA_DUR"), $TENTATIVAS tentativas)."

run_phase "3/4 Recolha das respostas do Ollama (fase 2)" uv run ./scripts/run-evaluation.sh evaluate -- --fase 2

run_phase "4/4 Avaliacao do juiz RAGAS (fase 3)" uv run ./scripts/run-evaluation.sh evaluate -- --fase 3

PIPELINE_DUR=$((SECONDS - PIPELINE_INICIO))

echo
echo "===== Resumo de duracoes ====="
for i in "${!PHASE_NAMES[@]}"; do
  printf '  %-55s %s\n' "${PHASE_NAMES[$i]}" "$(fmt_dur "${PHASE_SECS[$i]}")"
done
echo "  ------------------------------------------------------------"
printf '  %-55s %s\n' "Total" "$(fmt_dur "$PIPELINE_DUR")"
echo
echo "Resultados em tests/qa_generation/output/ragas_evaluation_results.csv"
echo "Log completo em: $LOG_FILE"
