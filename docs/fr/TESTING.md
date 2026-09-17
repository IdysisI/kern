# Vérification de Kern 0.3

Les tests de `tests/` utilisent un `KERN_HOME` temporaire, des dossiers temporaires,
un modèle simulé, un vrai sous-processus MCP de test et des ports éphémères.
Ils ne demandent aucune clé API. `KERN_SANDBOX=0` isole les tests du support bwrap
propre à la machine. Ce sont des tests fonctionnels du harness, pas des scores LLM.

## Commandes

Linux :

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest tests -q
.venv/bin/python -m compileall -q kern
.venv/bin/python -m kern --help
```

Windows / PowerShell :

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m compileall -q kern
.\.venv\Scripts\python.exe -m kern --help
```

Si le répertoire temporaire partagé Windows a des ACL héritées incompatibles :

```powershell
New-Item -ItemType Directory -Force .test-runs | Out-Null
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp=".test-runs/run-$([guid]::NewGuid().ToString('N'))"
```

Le parent du `--basetemp` doit exister. Cette option ne modifie pas les ACL système.

Avec uv : `uv sync --locked --extra test`, puis `uv run pytest tests -q`.
La CI configure Windows/Linux et Python 3.11, 3.12, 3.13. Ajouter la CI au dépôt
n'est pas la preuve d'une exécution GitHub : seules les exécutions locales sont
rapportées dans RAPPORT_FINAL.md.

## Couverture

- Stockage : récupération après append déchiré, deux instances, validation de
  chemin, checkpoints, undo, fichiers tiers conservés, artefacts immuables, UTF-8,
  CRLF, redaction imbriquée.
- Exécution : arrêt réel des enfants, absence d'effet différé, REPL persistant puis
  tué au timeout, interruption avant reçu, verrou de tour, doublon bloqué avant effet.
- Contexte : objectif/plan, clôture des appels interrompus, projection répétable,
  épisodes exacts, dernières consignes d'une demande longue, budget de sortie réel,
  image comptée séparément de son encodage base64, critique de fin avec poursuite.
- Mémoire/MCP : notes sans fausse supersession, clés explicites, legacy, chemins,
  négociation stdio, pagination, RPC concurrents, isError, nettoyage, nouvelle
  session/fork sans montage, noms d'outils compatibles, schéma récursif.
- Adaptateurs : SSE complet/incomplet, fragments JSON, exemples non exécutés,
  erreur d'arguments Anthropic, usage cumulatif, images dans les bons rôles.
- Interfaces : HTTP/assets/Origin, session WebSocket/reconnexion, erreurs corrélées,
  fork multi-client, exclusion de deux tours concurrents, TUI headless avec plan,
  réponse et petit terminal.

## Parcours graphique reproductible sans fournisseur

```text
python tests/preview_server.py
```

Ouvrir `http://127.0.0.1:8976`. Envoyer une demande, autoriser l'écriture de `qa.txt`
dans le dossier temporaire annoncé, examiner la réponse, le plan et les reçus,
recharger la page et vérifier l'absence de nouvel effet. Le serveur simule le modèle ;
son texte et ses avis ne constituent pas une démonstration d'intelligence réelle.
Tester aussi le refus et Échap sur l'approbation. Arrêter le serveur après le contrôle.
`node --check kern/static/app.js` vérifie la syntaxe JS ; Node n'est pas nécessaire
au lancement du produit.

## Distribution

```text
uv build
uv venv .venv-package
uv pip install --python .venv-package/Scripts/python.exe "dist/kern_agent-0.3.0-py3-none-any.whl[test]"
```

Sous Linux, remplacer `Scripts/python.exe` par `bin/python`. Exécuter les tests
depuis un dossier extérieur au dépôt, en passant le chemin absolu de `tests` et
`--import-mode=importlib`, puis vérifier `kern.__file__` : il doit désigner
`site-packages`, et non l'arbre source. Le wheel doit contenir les trois assets web ;
le sdist ne doit contenir ni `scratch/`, ni venv, ni fichiers temporaires.

## Évaluations de modèles et anciens benchmarks

Les anciens scripts `bench/` et leurs résultats sont conservés comme historique.
Ils ne sont pas tous portables ni tous isolés et certains utilisent un fournisseur
au chargement. Les scénarios T20/T22 ciblent le piggyback supprimé : ils ne sont
plus les critères de conformité de la nouvelle mémoire. La suite actuelle ne
réutilise aucun ancien « tout passe » ni score comme preuve.

Une évaluation réelle doit fixer fournisseur, modèle exact, révision Kern, tâches,
répétitions et critères externes (fichiers produits, tests, opérations répétées,
contraintes rappelées après plusieurs épisodes). Comparer baseline et harness
sur les mêmes tâches ; distinguer timeout fournisseur, échec du harness et erreur
du modèle. Les scénarios fournisseurs exécutés sont documentés dans RAPPORT_FINAL.md ;
ils ne constituent pas une comparaison statistique avec une baseline.

### Scénario fournisseur opt-in

```text
python tests/live_eval.py --base-url URL_DU_PROXY --model IDENTIFIANT --output resultat.json
```

Ce script effectue de vrais appels facturables. Il crée son propre `KERN_HOME` et
projet temporaire, utilise uniquement des données synthétiques, puis conserve les
journaux et les critères dans le fichier JSON indiqué. Il vérifie création et lecture,
plan, note durable, épisode par appel auxiliaire, réouverture sans réécriture et
rappel mémoire dans une nouvelle session. Le pliage d'épisode est explicitement
invoqué pour tester le chemin mémoire sans fabriquer une conversation volumineuse.
Une exécution par modèle est un contrôle d'intégration, pas une mesure générale de
compétence ni une démonstration d'amélioration sur une baseline.
