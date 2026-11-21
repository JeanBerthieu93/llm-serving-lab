# Commande docker. Si docker exige sudo sur cette machine : c'est donc
# le defaut. Sur une machine ou ton utilisateur est dans le groupe docker :
#   make up DOCKER=docker
DOCKER ?= sudo docker

# Modele par defaut : le plus petit qui donne des mesures exploitables.
# Voir les commentaires de docker-compose.yml pour les alternatives.
MODEL ?= llama3.2:1b

LOCAL_URL ?= http://localhost:11434/v1/chat/completions
K8S_URL ?= http://localhost:30080/v1/chat/completions
DATE := $(shell date +%Y-%m-%d)

.PHONY: help check up pull down logs bench k3d-up k3d-pull k3d-bench k3d-down \
        kind-up kind-down clean

help:
	@echo "Avant de commencer"
	@echo "  make check       verifie docker, compose, cpuset, espace disque"
	@echo ""
	@echo "Etape 1 - Docker"
	@echo "  make up          demarre le serveur d'inference"
	@echo "  make pull        telecharge le modele ($(MODEL))"
	@echo "  make bench       mesure les paliers de concurrence"
	@echo "  make logs/down   journaux / arret"
	@echo ""
	@echo "Etape 2 - Kubernetes jetable"
	@echo "  make k3d-up      cluster k3d + deploiement   (leger, ~1 Go)"
	@echo "  make kind-up     cluster kind + deploiement  (~2 Go)"
	@echo "  make k3d-pull    telecharge le modele dans le pod"
	@echo "  make k3d-bench   mesure via le NodePort 30080"
	@echo "  make k3d-down    detruit le cluster"
	@echo ""
	@echo "Variables : MODEL=$(MODEL)  DOCKER=$(DOCKER)"
	@echo "Exemple   : make bench MODEL=llama3.2:3b"

# --- Verifications prealables ---------------------------------------------

check:
	@echo "--- docker ---"
	@$(DOCKER) ps >/dev/null 2>&1 \
		&& echo "ok : '$(DOCKER) ps' fonctionne" \
		|| { echo "ECHEC : '$(DOCKER) ps'. Essaie 'sudo -v' puis relance."; exit 1; }
	@echo "--- compose (la balise !override exige >= 2.24) ---"
	@$(DOCKER) compose version
	@echo "--- cœurs et charge ---"
	@echo "nproc = $$(nproc), load = $$(cut -d' ' -f1-3 /proc/loadavg)"
	@grep -E '^\s+cpuset:' docker-compose.override.yml \
		|| echo "ATTENTION : pas de cpuset dans la surcouche"
	@echo "--- memoire ---"
	@free -h | head -2
	@echo "--- dossier des modeles ---"
	@. ./.env 2>/dev/null; \
	  d="$${MODELS_DIR:-/tmp}"; \
	  mkdir -p "$$d" && echo "$$d : $$(df -h "$$d" | tail -1 | awk '{print $$4" libres sur "$$6}')"

# --- Etape 1 : Docker ------------------------------------------------------

up:
	$(DOCKER) compose up -d
	@echo "Attente du serveur..."
	@until curl -sf http://localhost:11434/api/version >/dev/null 2>&1; do sleep 2; done
	@echo "Pret. Pense a : make pull"

pull:
	$(DOCKER) compose exec ollama ollama pull $(MODEL)

logs:
	$(DOCKER) compose logs -f ollama

down:
	$(DOCKER) compose down

bench:
	@mkdir -p results
	python3 bench.py --url $(LOCAL_URL) --model $(MODEL) \
		--csv results/$(DATE)-docker-$(subst :,-,$(MODEL)).csv

# --- Etape 2 : Kubernetes --------------------------------------------------
# k3d est prefere a kind sur une machine partagee : un seul serveur,
# pas d'agent, empreinte memoire bien plus faible.

k3d-up:
	k3d cluster create llm-lab --agents 0 \
		-p "30080:30080@server:0" \
		--k3s-arg "--disable=traefik@server:0"
	kubectl apply -f ollama-k8s.yaml
	kubectl -n llm-lab rollout status deploy/ollama --timeout=10m

k3d-pull:
	kubectl -n llm-lab exec deploy/ollama -- ollama pull $(MODEL)

k3d-bench:
	@mkdir -p results
	python3 bench.py --url $(K8S_URL) --model $(MODEL) \
		--csv results/$(DATE)-k8s-$(subst :,-,$(MODEL)).csv

k3d-down:
	k3d cluster delete llm-lab

kind-up:
	kind create cluster --config kind-cluster.yaml
	kubectl apply -f ollama-k8s.yaml
	kubectl -n llm-lab rollout status deploy/ollama --timeout=10m

kind-down:
	kind delete cluster --name llm-lab

clean: down
	-$(MAKE) k3d-down
	-$(MAKE) kind-down
