#!/usr/bin/env bash
# guard.sh - chien de garde pour benchmark sur machine partagee.
#
# Surveille la charge et la memoire disponible de l'HOTE. Si un seuil est
# franchi, arrete le conteneur d'inference et sort. A lancer dans un second
# terminal AVANT le benchmark, puis a laisser tourner.
#
#   chmod +x guard.sh
#   ./guard.sh                      # seuils automatiques
#   ./guard.sh --load 6 --min-free 2048
#   ./guard.sh --dry-run            # ne stoppe rien, journalise seulement
#
# Si docker exige sudo sur ta machine (cas frequent sur un serveur partage) :
#   DOCKER="sudo docker" ./guard.sh
# Verifie d'abord que sudo ne redemande pas le mot de passe en cours de
# route, sinon le chien de garde ne pourra pas arreter le conteneur :
#   sudo -n docker ps >/dev/null && echo "sudo sans mot de passe : ok"
#
# Seuils par defaut :
#   charge   : nproc x 1.5   (une machine a 8 cœurs declenche a 12.0)
#   memoire  : 1536 Mo disponibles
#   disque / : 90 % d'occupation
#
# Tout est journalise dans guard.log : en cas de declenchement, tu sais
# quand et pourquoi, ce qui est exactement ce qu'on te demandera.

set -uo pipefail

CONTAINER="${CONTAINER:-llm-lab-ollama}"
# Commande docker. Surcharge : DOCKER="sudo docker" ./guard.sh
DOCKER="${DOCKER:-docker}"
INTERVAL=5
DRY_RUN=0
LOG="guard.log"

NPROC="$(nproc 2>/dev/null || echo 2)"
LOAD_MAX="$(awk -v n="$NPROC" 'BEGIN{printf "%.1f", n*1.5}')"
MIN_FREE_MB=1536
DISK_MAX_PCT=90

while [ $# -gt 0 ]; do
    case "$1" in
        --load)      LOAD_MAX="$2"; shift 2 ;;
        --min-free)  MIN_FREE_MB="$2"; shift 2 ;;
        --disk)      DISK_MAX_PCT="$2"; shift 2 ;;
        --interval)  INTERVAL="$2"; shift 2 ;;
        --container) CONTAINER="$2"; shift 2 ;;
        --log)       LOG="$2"; shift 2 ;;
        --dry-run)   DRY_RUN=1; shift ;;
        -h|--help)   sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "option inconnue : $1" >&2; exit 2 ;;
    esac
done

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"
}

stop_container() {
    if [ "$DRY_RUN" -eq 1 ]; then
        log "DRY-RUN : le conteneur $CONTAINER aurait ete arrete"
        return 0
    fi
    if $DOCKER stop "$CONTAINER" >/dev/null 2>&1; then
        log "CONTENEUR ARRETE : $CONTAINER"
    else
        log "ECHEC de l'arret de $CONTAINER avec '$DOCKER stop'"
        log "  -> deja arrete, droits insuffisants, ou sudo demande un mot de passe"
        log "  -> ARRETE LE BENCHMARK A LA MAIN IMMEDIATEMENT"
    fi
}

# Verification des droits au demarrage : mieux vaut echouer maintenant
# qu'au moment ou il faut vraiment arreter le conteneur.
preflight() {
    if [ "$DRY_RUN" -eq 1 ]; then
        return 0
    fi
    if ! $DOCKER ps >/dev/null 2>&1; then
        echo "ERREUR : '$DOCKER ps' ne fonctionne pas." >&2
        echo "Si docker exige sudo : DOCKER=\"sudo docker\" $0 $*" >&2
        echo "Si sudo redemande le mot de passe, lance d'abord 'sudo -v'." >&2
        exit 3
    fi
}

preflight

log "--- demarrage : seuils charge=$LOAD_MAX memoire=${MIN_FREE_MB}Mo disque=${DISK_MAX_PCT}% (nproc=$NPROC) ---"
log "conteneur surveille : $CONTAINER, intervalle ${INTERVAL}s"

trap 'log "--- arret du chien de garde (interruption) ---"; exit 0' INT TERM

while true; do
    load1="$(cut -d' ' -f1 /proc/loadavg)"
    mem_avail_mb="$(awk '/^MemAvailable:/ {printf "%d", $2/1024}' /proc/meminfo)"
    disk_pct="$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')"
    disk_pct="${disk_pct:-0}"

    breach=""
    if awk -v a="$load1" -v b="$LOAD_MAX" 'BEGIN{exit !(a>b)}'; then
        breach="charge $load1 > $LOAD_MAX"
    elif [ "$mem_avail_mb" -lt "$MIN_FREE_MB" ]; then
        breach="memoire disponible ${mem_avail_mb}Mo < ${MIN_FREE_MB}Mo"
    elif [ "$disk_pct" -gt "$DISK_MAX_PCT" ]; then
        breach="disque / a ${disk_pct}% > ${DISK_MAX_PCT}%"
    fi

    if [ -n "$breach" ]; then
        log "SEUIL FRANCHI : $breach"
        stop_container
        log "--- arret du chien de garde ---"
        exit 1
    fi

    log "ok charge=$load1 mem_dispo=${mem_avail_mb}Mo disque=${disk_pct}%"
    sleep "$INTERVAL"
done
