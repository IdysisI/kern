# Mission Kern — audit et refonte

## Cadre et invariants

Demande du 13 septembre 2026. Priorité : intelligence, organisation, fiabilité.
Outils minimaux par défaut ; MCP montés uniquement dans la session ; aucune
économie de requêtes au détriment de la qualité. Linux et Windows à exécuter.
Ne pas confondre résultats déterministes et performances mesurées des modèles.

## État initial

- Racine réelle : `kern/` dans le dossier de travail, dépôt Git initialisé.
- Branche dédiée : `audit/kern-reliability-2026-09-13`.
- Modifications préexistantes : dossier `scratch/` non suivi, à préserver.
- Python >=3.11, httpx, Rich, Textual, websockets ; GUI Qt optionnelle.
- Un audit antérieur existe ; ses affirmations doivent être revérifiées.
- L'environnement hôte est Windows ; runtimes Python/Linux à localiser.

## Phases et livrables

1. Audit initial consigné dans AUDIT.md ; revue continue avec reproductions.
2. Recherche consignée dans RECHERCHE.md, sources primaires et limites.
3. Conception consignée dans DESIGN.md.
4. Corrections cœur implémentées ; causes racines et correctifs dans RAPPORT_FINAL.md.
5. Mémoire remplacée par épisodes attribués, reçus indépendants et notes SQLite ;
   montage MCP minimal et limité à la session préservé.
6. TUI et application web implémentées ; tests et parcours navigateur exécutés.
7. Matrice finale exécutée : 58 tests Windows source, 58 Ubuntu/WSL,
   58 Windows depuis le wheel installé hors du dossier source.
8. Rapport français final livré ; limites fournisseur réel, Arch natif, bwrap et Qt
   explicitement conservées. Les essais VSLLM complètent les tests simulés ; aucune
   supériorité générale sur une baseline n'est démontrée.

## Décisions

- Aucun code d'implémentation avant l'exploration du dépôt entier.
- Rapports en Markdown versionnés dans le dépôt.
- Pas de sous-agents : aucune délégation demandée.
- Toute limite de validation sera explicitement consignée.

## Journal de progression

- Initialisation : inventaire des chemins, lecture README/pyproject/audit antérieur,
  création de la branche et du présent suivi. Aucun code modifié.
- Exploration : contradictions README/code ; défauts cœur, mémoire, MCP, interfaces
  documentés. Python Windows installé dans .venv, compileall réussi. fcntl et
  autorisations read-only reproduits. WSL Ubuntu-24.04 accessible hors sandbox.
- Recherche/conception : journal et preuves indépendants des récits ; épisodes
  incrémentaux, mémoire transactionnelle attribuée, projection avant requête,
  runtime par session, nouvelles sessions sans montages. Aucun score modèle promis.
- Stockage : verrous portables, append durable, récupération queue, snapshots
  hashés, undo restreint aux chemins snapshotés ; Unicode/newlines validés.
- Exécution : shell natif, background hors bwrap corrigé, approbations, appels
  bloquants déportés, REPL isolé tué au timeout. Windows tree-kill reste à renforcer.
- Mémoire : retrait piggyback/deepening, épisodes exacts et requête dédiée JSON,
  index récupérable, preuve indépendante, recherche d'événements, supersession
  uniquement clé explicite. Les 29 tests couvrent aussi MCP concurrent/paginé.
- Interfaces : HTTP/WS et TUI testés ; application web inspectée dans le navigateur.
  Catalogue réel indisponible sur le proxy local : les échanges utilisent un modèle
  simulé dans les tests, aucune performance de modèle réel n'est revendiquée.
- Validation : 34/34 Windows (16,70 s), 34/34 Ubuntu/WSL (14,36 s). Régression
  Textual copie Markdown corrigée ; snapshots annulés rendus inactifs ; arrêt
  parent/enfants réellement vérifié sur les deux OS par absence d'effet différé.
- Revue continue : erreurs RPC corrélées sans fermer la connexion, réservation
  concurrente du tour, statuts de fin explicites, retrait des snapshots implicites
  de fichiers utilisateur sales non modifiés par Kern, rendu Markdown web sûr.
- À vérifier ensuite : adaptateurs streaming interrompu et multimodal, historique
  ancien/migrations, protocole face aux requêtes invalides, packaging/CI/documentation.
- 14 septembre : 48 tests Windows passés (8,96 s), puis test de revue de fin ajouté
  et validé avec les 24 tests cœur. Streaming tronqué refusé, outils Anthropic mal
  formés conservés comme erreurs, usage cumulatif corrigé, PNG de probe réparé,
  health isolé par endpoint. Sources officielles consultées pour les adaptateurs.
- Parcours navigateur exécuté à 1042 px et 390 px : demande/approbation/écriture/
  rendu Markdown/plan/reçus/rechargement. Une seule écriture sur disque après reprise.
  Ajout de l'accès au panneau d'état sur petit écran et reset du choix d'approbation.
- Fork multi-client séparé ; RPC invalide renvoie son req_id et la connexion survit ;
  notes legacy migrées/lisibles/oubliables ; noms MCP compatibles sans ambiguïté ;
  snapshots créés sous verrou au moment exact de l'écriture.
- Interruption de commande longue : signal coopératif vers le thread, arrêt de
  l'arbre puis reçu incertain avant clôture. Test Windows réussi, matrice à relancer.
- Revue de fin implémentée : jusqu'à deux critiques JSON du même modèle sur preuves,
  continuation ciblée ou blocage/non-vérification explicite. Scénario simulé validé :
  la lecture manquante est effectuée, aucune réécriture du travail terminé.
- Distribution : wheel construit et assets présents ; sdist contenait scratch/
  préexistant, règle d'inclusion explicite ajoutée. Rebuild/install isolée à vérifier.
  CLI --help/--model/--max-steps cohérente ; CI Windows/Linux Python 3.11–3.13 ajoutée.

- Clôture du 14 septembre : artefacts hors contexte immuables par contenu, provenance
  des notes, Unicode du shell Windows et budget de sortie réellement disponible
  couverts par les dernières régressions. Aucun changement de code après la matrice.
- Matrice finale : Windows Python 3.12.14, 54/54 (9,68 s) ; Ubuntu 24.04/WSL
  Python 3.12.3, 54/54 (10,06 s) ; wheel installé sous Windows Python 3.11.15
  hors source, 54/54 (10,36 s). Compilation/CLI, syntaxe JavaScript, verrou de
  dépendances et cohérence de l'installation vérifiés.
- Distribution : wheel 25 fichiers dont trois assets web ; sdist 37 fichiers,
  rapports inclus, sans scratch, environnements virtuels ni données de tests.
- Rapport final : audit, corrections, architecture, sources, lancement et résultats
  consignés. Proxy local indisponible : aucune évaluation de capacité LLM réelle.

- Reprise : adresse VSLLM fournie par l'utilisateur et catalogue joignable. Nouvelle
  validation fournisseur en cours (Haiku/Anthropic et MiniMax/OpenAI), entièrement
  isolée dans des projets synthétiques. Le script initial utilisait le nom d'outil
  au lieu de la description pour l'approbation ; tentatives interrompues et exclues.
  Les limites « aucun fournisseur réel » du rapport seront révisées avec les résultats.

- VSLLM réel : premières exécutions exploitables ont révélé les contrats JSON trop
  étroits (listes textuelles et null), ainsi qu'une adresse SQLite absente du schéma.
  Normalisation contrôlée et adresse note:<id> explicite implémentées. Le crash CLI
  Unicode sous redirection ANSI a été reproduit puis corrigé par flux UTF-8.
- Nouvelle matrice après corrections : 58/58 Windows source (11,39 s),
  58/58 Ubuntu/WSL (13,44 s), 58/58 wheel Windows Python 3.11 (11,53 s).
  MiniMax réel : 12 critères réussis, 18 requêtes incluant les probes, 66,70 s.

- Résultats fournisseurs finaux : MiniMax 12/12 Windows (18 requêtes, 66,70 s)
  et Ubuntu/WSL (17 requêtes, 60,14 s). Haiku 10/12 : premier tour et nouvelle
  session réussis, reprise échouée par timeout puis 503 no upstream keys available.
  Sonnet avait réussi le premier parcours (11/11), mais dernier probe bloqué par
  le même 503. Aucun échec fournisseur n'est masqué ni compté comme un succès.
- Résultats synthétiques versionnés dans tests/results/vsllm-2026-09-14.json ;
  rapport mis à jour avec les limites et sans prétendre à une comparaison baseline.
- Script d'évaluation : son propre flux ANSI a aussi empêché l'affichage initial
  de symboles Unicode ; corrigé en UTF-8 avant les exécutions comptabilisées.

- Clôture : wheel final 25 fichiers / sdist 39 fichiers vérifiés, rapports et
  résultats fournisseurs inclus. Aucun processus d'évaluation restant ; onglets
  de QA déjà fermés. Changements préparés pour commit local, scratch préservé.
