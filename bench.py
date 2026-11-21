#!/usr/bin/env python3
"""
bench.py - banc de mesure pour une API d'inference compatible OpenAI
(Ollama, vLLM, llama.cpp server, LiteLLM, KServe...).

Mesure, par palier de concurrence :
  - TTFT (time to first token) : p50 / p95
  - latence totale par requete : p50 / p95
  - debit agregge en tokens/seconde
  - taux d'erreur

Aucune dependance : bibliotheque standard Python 3.9+ uniquement.

Exemples
--------
    # Ollama en local, modele 3B, paliers par defaut
    python3 bench.py

    # vLLM, paliers personnalises, 8 requetes par palier
    python3 bench.py \
        --url http://localhost:8000/v1/chat/completions \
        --model mistralai/Mistral-7B-Instruct-v0.3 \
        --levels 1,4,8,16,32,64 --repeat 2

    # Resultats dans un CSV a archiver dans le depot
    python3 bench.py --csv results/2026-11-21-ollama-3b.csv

Note sur le comptage des tokens : le script compte les fragments ("chunks")
renvoyes par le flux SSE. La plupart des moteurs emettent un fragment par
token, donc le chiffre est une tres bonne approximation, mais ce n'est pas
le compteur officiel du moteur. Pour une mesure exacte, croise avec les
metriques Prometheus du serveur.
"""

import argparse
import csv
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional

DEFAULT_PROMPT = (
    "Explique en un paragraphe pourquoi la latence du premier token est une "
    "metrique plus importante que le debit total pour une application de chat."
)


# --------------------------------------------------------------------------
# Une requete
# --------------------------------------------------------------------------

@dataclass
class Result:
    ok: bool
    ttft: Optional[float]      # secondes
    total: Optional[float]     # secondes
    out_tokens: int
    error: Optional[str] = None


def one_request(url: str, model: str, prompt: str, max_tokens: int,
                timeout: float, api_key: Optional[str]) -> Result:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
    }).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    start = time.perf_counter()
    ttft: Optional[float] = None
    ntok = 0

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    if ttft is None:
                        ttft = time.perf_counter() - start
                    ntok += 1
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return Result(False, None, None, 0, "HTTP {}: {}".format(exc.code, detail))
    except Exception as exc:
        return Result(False, None, None, 0, "{}: {}".format(type(exc).__name__, exc))

    total = time.perf_counter() - start
    if ttft is None:
        return Result(False, None, total, 0, "aucun token recu")
    return Result(True, ttft, total, ntok)


# --------------------------------------------------------------------------
# Statistiques
# --------------------------------------------------------------------------

def pct(values: List[float], p: float) -> float:
    """Percentile par interpolation lineaire. Renvoie nan si vide."""
    if not values:
        return float("nan")
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


@dataclass
class LevelStats:
    concurrency: int
    requests: int
    ok: int
    errors: int
    ttft_p50: float
    ttft_p95: float
    total_p50: float
    total_p95: float
    throughput: float          # tokens/s agregge
    wall: float                # duree du palier
    first_error: Optional[str]


def run_level(concurrency: int, n_requests: int, url: str, model: str,
              prompt: str, max_tokens: int, timeout: float,
              api_key: Optional[str]) -> LevelStats:
    def task(_):
        return one_request(url, model, prompt, max_tokens, timeout, api_key)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(task, range(n_requests)))
    wall = time.perf_counter() - t0

    good = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    ttfts = [r.ttft for r in good if r.ttft is not None]
    totals = [r.total for r in good if r.total is not None]
    tokens = sum(r.out_tokens for r in good)

    return LevelStats(
        concurrency=concurrency,
        requests=n_requests,
        ok=len(good),
        errors=len(bad),
        ttft_p50=pct(ttfts, 50),
        ttft_p95=pct(ttfts, 95),
        total_p50=pct(totals, 50),
        total_p95=pct(totals, 95),
        throughput=(tokens / wall) if wall > 0 else 0.0,
        wall=wall,
        first_error=bad[0].error if bad else None,
    )


# --------------------------------------------------------------------------
# Restitution
# --------------------------------------------------------------------------

def fmt_ms(seconds: float) -> str:
    if seconds != seconds:  # nan
        return "-"
    return "{:.0f}".format(seconds * 1000)


def print_table(stats: List[LevelStats]) -> None:
    header = ("| Concurrence | Req. | OK | Err | TTFT p50 (ms) | TTFT p95 (ms) "
              "| Total p50 (ms) | Total p95 (ms) | Debit (tok/s) |")
    sep = ("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    print()
    print(header)
    print(sep)
    for s in stats:
        print("| {} | {} | {} | {} | {} | {} | {} | {} | {:.1f} |".format(
            s.concurrency, s.requests, s.ok, s.errors,
            fmt_ms(s.ttft_p50), fmt_ms(s.ttft_p95),
            fmt_ms(s.total_p50), fmt_ms(s.total_p95),
            s.throughput))
    print()


def print_analysis(stats: List[LevelStats]) -> None:
    usable = [s for s in stats if s.ok > 0]
    if not usable:
        print("Aucun palier exploitable : le service n'a repondu a aucune requete.")
        return

    base = usable[0]
    print("Lecture des resultats")
    print("---------------------")
    print("Reference (concurrence {}) : TTFT p95 = {} ms, debit = {:.1f} tok/s".format(
        base.concurrency, fmt_ms(base.ttft_p95), base.throughput))

    knee = None
    saturation = None
    for prev, s in zip(usable, usable[1:]):
        if base.ttft_p95 == base.ttft_p95 and base.ttft_p95 > 0:
            ratio = s.ttft_p95 / base.ttft_p95
            if knee is None and ratio >= 3.0:
                knee = (s, ratio)
        # Saturation : doubler la concurrence n'apporte plus 10 % de debit.
        if saturation is None and prev.throughput > 0 \
                and s.throughput < prev.throughput * 1.1:
            saturation = prev
        if s.errors > 0:
            print("  - concurrence {} : {} erreur(s), premiere = {}".format(
                s.concurrency, s.errors, s.first_error))

    if knee:
        s, ratio = knee
        print("  - genou de latence a la concurrence {} : TTFT p95 x{:.1f} "
              "par rapport a la reference".format(s.concurrency, ratio))
    else:
        print("  - aucun genou de latence detecte : pousse les paliers plus haut "
              "(--levels) jusqu'a le trouver")

    best = max(usable, key=lambda s: s.throughput)
    print("  - debit maximal : {:.1f} tok/s a la concurrence {}".format(
        best.throughput, best.concurrency))
    if saturation and saturation.concurrency <= best.concurrency:
        print("  - le debit cesse de progresser des la concurrence {} : "
              "le batching ne compense plus".format(saturation.concurrency))
    print()
    print("A consigner dans le README : la concurrence maximale tenable, le TTFT p95 "
          "a ce palier, et la raison de la degradation au-dela.")
    print()


def write_csv(path: str, stats: List[LevelStats], meta: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["# " + k + "=" + str(v) for k, v in meta.items()])
        writer.writerow([
            "concurrency", "requests", "ok", "errors",
            "ttft_p50_ms", "ttft_p95_ms", "total_p50_ms", "total_p95_ms",
            "throughput_tok_s", "wall_s",
        ])
        for s in stats:
            writer.writerow([
                s.concurrency, s.requests, s.ok, s.errors,
                round(s.ttft_p50 * 1000, 1) if s.ttft_p50 == s.ttft_p50 else "",
                round(s.ttft_p95 * 1000, 1) if s.ttft_p95 == s.ttft_p95 else "",
                round(s.total_p50 * 1000, 1) if s.total_p50 == s.total_p50 else "",
                round(s.total_p95 * 1000, 1) if s.total_p95 == s.total_p95 else "",
                round(s.throughput, 2), round(s.wall, 2),
            ])


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Banc de mesure pour une API d'inference compatible OpenAI.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:11434/v1/chat/completions",
                    help="endpoint chat completions (defaut : Ollama en local)")
    ap.add_argument("--model", default="llama3.2:1b", help="nom du modele")
    ap.add_argument("--levels", default="1,2,5,10,20",
                    help="paliers de concurrence, separes par des virgules")
    ap.add_argument("--repeat", type=int, default=4,
                    help="requetes par worker a chaque palier (defaut 4)")
    ap.add_argument("--max-tokens", type=int, default=128,
                    help="plafond de tokens generes, identique pour tous les paliers")
    ap.add_argument("--timeout", type=float, default=300.0, help="timeout par requete (s)")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT, help="prompt de test")
    ap.add_argument("--csv", default=None, help="chemin du CSV de sortie")
    ap.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"),
                    help="jeton Bearer si le endpoint en exige un")
    ap.add_argument("--no-warmup", action="store_true",
                    help="ne pas faire la requete de chauffe")
    args = ap.parse_args()

    try:
        levels = [int(x) for x in args.levels.split(",") if x.strip()]
    except ValueError:
        print("--levels doit etre une liste d'entiers, ex : 1,5,10", file=sys.stderr)
        return 2
    if not levels or any(c < 1 for c in levels):
        print("--levels doit contenir des entiers >= 1", file=sys.stderr)
        return 2
    if args.repeat < 1:
        print("--repeat doit etre >= 1", file=sys.stderr)
        return 2

    print("Cible      : {}".format(args.url))
    print("Modele     : {}".format(args.model))
    print("Paliers    : {}".format(levels))
    print("Max tokens : {}".format(args.max_tokens))

    if not args.no_warmup:
        print("\nChauffe (chargement du modele en memoire)...", end=" ", flush=True)
        warm = one_request(args.url, args.model, args.prompt,
                           min(16, args.max_tokens), args.timeout, args.api_key)
        if warm.ok:
            print("ok, TTFT {} ms".format(fmt_ms(warm.ttft)))
        else:
            print("ECHEC")
            print("\nLa requete de chauffe a echoue : {}".format(warm.error))
            print("Verifie que le service repond et que le modele est bien charge.")
            print("  curl {}".format(args.url.rsplit("/v1/", 1)[0]))
            return 1

    stats: List[LevelStats] = []
    for c in levels:
        n = c * args.repeat
        print("Palier concurrence {:>3} ({} requetes)...".format(c, n),
              end=" ", flush=True)
        s = run_level(c, n, args.url, args.model, args.prompt,
                      args.max_tokens, args.timeout, args.api_key)
        stats.append(s)
        print("TTFT p95 {} ms, {:.1f} tok/s, {} erreur(s)".format(
            fmt_ms(s.ttft_p95), s.throughput, s.errors))

    print_table(stats)
    print_analysis(stats)

    if args.csv:
        meta = {
            "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "url": args.url,
            "model": args.model,
            "max_tokens": args.max_tokens,
            "repeat": args.repeat,
        }
        write_csv(args.csv, stats, meta)
        print("CSV ecrit : {}".format(args.csv))

    return 0


if __name__ == "__main__":
    sys.exit(main())
