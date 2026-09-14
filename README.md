# Kern

Agent IA personnel en Python, avec TUI et application web locale. Un même moteur
exécute les outils, conserve le journal et sert les interfaces. Le modèle est choisi
par l'utilisateur ; les sous-agents et les revues auxiliaires utilisent ce même modèle.

## Installation

Python 3.11 ou supérieur. Git est utile pour les projets versionnés, mais n'est pas
requis pour démarrer Kern. Le shell d'exécution est Bash/sh sur Linux et PowerShell
sur Windows. Les assets web sont inclus ; aucun serveur Node ni CDN n'est nécessaire.

Linux :

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
export KERN_BASE_URL=http://127.0.0.1:8790
export KERN_MODEL=identifiant-du-modele
# Si le fournisseur demande une clé : exporter KERN_API_KEY.
.venv/bin/python -m kern
```

Windows, PowerShell :

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
$env:KERN_BASE_URL = 'http://127.0.0.1:8790'
$env:KERN_MODEL = 'identifiant-du-modele'
# Si le fournisseur demande une clé : définir KERN_API_KEY dans l'environnement.
.\.venv\Scripts\python.exe -m kern
```

Le proxy local n'est pas livré avec Kern. `KERN_BASE_URL` doit désigner votre
fournisseur/proxy compatible. Le suffixe `/v1` est accepté. Par défaut, les noms
`claude-*` passent par Messages Anthropic ; les autres par Chat Completions.
`KERN_PROTOCOL=openai` ou `anthropic` permet un choix explicite pour les alias.
Le catalogue fournit les limites quand il les expose ; sinon les valeurs de repli
sont 32 768 tokens de contexte et 8 192 de sortie, configurables ci-dessous.

Pour une installation reproductible avec uv : `uv sync --locked --extra test`,
puis `uv run kern`. `uv.lock` inclut aussi les dépendances Qt optionnelles.

## Interfaces

```text
python -m kern                          TUI
python -m kern web                      Web + daemon sur http://127.0.0.1:8766
python -m kern serve                    Alias du même serveur
python -m kern --model <id>             TUI avec modèle explicite
python -m kern --probe <id>             Mesurer les capacités
python -m kern --task "..."             Exécution sans interface, approbation automatique
python -m kern --max-steps 30 --task "..."  Limite explicite pour un run automatisé
python -m kern --help                   Aide
```

La TUI s'attache au daemon local ; fermer un terminal n'arrête pas son travail.
Ctrl+P choisit le modèle, Ctrl+R reprend une session. Ctrl+C interrompt le tour,
efface le brouillon ou quitte quand l'interface est au repos. `/help` liste les
commandes ; `/context`, `/history <requête>`, `/tools`, `/undo` et `/fork` restent
accessibles. Sur terminal large, le panneau de droite montre plan et preuves.

Le web fournit sessions, recherche, modèle, streaming, résultats/diffs, approbations,
interruption, duplication, undo et historique. Sur petit écran, le bouton **État**
ouvre le plan, les montages et les reçus. Le rechargement reprend la session sans
renvoyer la demande. Les écritures restent soumises à l'approbation dans les interfaces.

L'ancienne interface Qt est conservée : installer `.[gui]`, puis `python -m kern gui`.
Elle n'est pas la nouvelle interface web et sa validation graphique n'est pas complète.

## Mémoire, organisation et outils

- Le journal garde demandes, intentions, résultats, plans, épisodes et revues.
  Une intention sans reçu reste incertaine ; une écriture réussie n'est pas un test réussi.
- Les épisodes couvrent des plages exactes récupérables. Une requête dédiée produit
  des notes de navigation avec provenance ; un index explicite remplace tout résumé
  invalide. Les anciennes passes de compression par seuil/piggyback ont été retirées.
- Le contexte est assemblé avant chaque requête : état de travail, reçus, épisodes
  pertinents et échanges récents ; les grosses sorties restent consultables sur disque.
- Les notes de projet utilisent SQLite. Seule une clé explicite remplace une note
  précédente ; les notes sans clé peuvent coexister. Les anciens Markdown restent
  lisibles et non vérifiés. La mémoire de projet est persistante, contrairement aux montages.
- Après des effets, une revue auxiliaire confronte réponse, objectif, plan et preuves.
  Elle peut demander un travail manquant. Deux revues par tour limitent les boucles
  de critique ; un avis de modèle ne devient jamais une preuve d'exécution.
- Un effet identique déjà réussi dans le même tour, ou resté incertain, est bloqué
  avant répétition. `_kern_repeat_reason` permet de justifier une répétition volontaire.

Le noyau expose `read`, `write`, `edit`, `exec`, `proc`, `fetch`, `memory`, `todo`,
`spawn`, `subagent`. `py` n'est chargé qu'après mesure de sa capacité ou
`KERN_FORCE_PY=1`. Aucun schéma MCP n'est chargé au démarrage d'une nouvelle session.

## MCP et skills

Configurer les serveurs stdio dans `~/.kern/mcp.json` (ou `$KERN_HOME/mcp.json`) :

```json
{
  "mon-serveur": {
    "description": "Description courte et concrète",
    "command": ["chemin-vers-executable", "argument"],
    "env": {"EXEMPLE_OPTION": "valeur"},
    "cwd": "chemin-absolu-du-serveur"
  }
}
```

`command` est une liste, pas une commande shell. Sous Windows, utiliser le chemin
réel de l'exécutable ; un script `.cmd` demande un lanceur adapté. Aucun serveur
n'est installé automatiquement. Les skills sont cherchés dans `~/.kern/skills/`
et `~/.agents/skills/`, avec un `SKILL.md` par sous-dossier.

Le modèle peut émettre une directive sur sa propre ligne :

```text
[list capabilities]
[mount: mon-serveur]
[mount-once: mon-serveur]
[unmount: mon-serveur]
```

`mount` dure pendant la session ; `mount-once` dure pendant le tour, puis nettoyage
même en cas d'erreur. Une **nouvelle session ou un fork repart sans MCP monté**.
La reprise explicite de la même session peut restaurer ses montages ; cela n'écrit
jamais de montage global dans la configuration. Les outils MCP demandent approbation.
Le transport livré est stdio ; le support MCP HTTP distant n'est pas implémenté.

## Réglages et données

| Variable | Usage |
|---|---|
| `KERN_HOME` | Répertoire d'état ; défaut `~/.kern` |
| `KERN_BASE_URL`, `KERN_API_KEY`, `KERN_MODEL` | Fournisseur, authentification, modèle |
| `KERN_PROTOCOL` | Forcer `openai` ou `anthropic` |
| `KERN_CONTEXT_WINDOW` | Fenêtre vérifiée du modèle, prioritaire sur le catalogue |
| `KERN_MAX_OUTPUT_TOKENS` | Limite de sortie vérifiée |
| `KERN_CONTEXT_TARGET` | Taille de travail visée, défaut 16 000 tokens estimés |
| `KERN_SERVE_PORT` | Port partagé TUI/web, défaut 8766 |
| `KERN_LOCAL=1` | TUI sans daemon, notamment pour diagnostic |
| `KERN_SANDBOX=0` | Désactive bwrap ; sinon activé sur Linux quand disponible |

L'estimation de contexte n'est pas un tokenizer du fournisseur. Une entrée qui
reste trop grande après maintenance est refusée explicitement. Les tâches ne sont
pas réputées finies parce qu'un budget est atteint ; le mode headless sort non-zéro
en cas d'erreur, de limite ou de fin non vérifiée.

`events.jsonl` conserve la trajectoire. Undo/rewind restaurent les fichiers suivis
par `write`/`edit` et archivent le journal annulé. Ils n'annulent pas les effets
arbitraires d'`exec`, de Python ou de MCP. Les états remplacés sont sauvegardés sous
`restore-backups/`. Les écritures externes ne prenant pas les verrous Kern peuvent
concurrencer les outils : garder le contrôle de qui modifie un fichier.

## Vérifier et comprendre la refonte

Voir [TESTING.md](TESTING.md) pour les commandes, [AUDIT.md](AUDIT.md) pour les causes
initiales, [RECHERCHE.md](RECHERCHE.md) pour les sources comparées,
[DESIGN.md](DESIGN.md) pour les choix et [RAPPORT_FINAL.md](RAPPORT_FINAL.md) pour
le bilan et les limites réellement mesurées.
