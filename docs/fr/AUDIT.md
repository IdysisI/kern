# Audit Kern

## Périmètre et état initial

Branche `audit/kern-reliability-2026-09-13`, Python >=3.11, 14 modules de
production, Textual/Rich, httpx, websockets, Qt6 optionnel. `serve.py` expose
uniquement un protocole WebSocket : aucun frontend web n'existe. `daemon.py`
est un second serveur, avec un cycle de vie différent. Aucun AGENTS.md local
supplémentaire trouvé. `scratch/` préexistant contient des expériences MCP/Unreal,
pas du code produit ; préservé. Inventaire de tous les chemins, lecture des modules
de production, inspection des scripts bench et des rapports historiques effectués.
Les scripts de débogage ne constituent pas une suite de tests isolée : plusieurs
font des appels réseau au chargement, utilisent /home/marty et des ports fixes.
Deux fichiers T13/T19 et bench/__init__.py sont vides. Les résultats JSON historiques
ne prouvent pas le fonctionnement de cette révision sur cette machine.

## Éléments solides à préserver

- Séparation moteur/interfaces et représentation intermédiaire des messages.
- Journal des intentions avant exécution, résultats et marqueurs de fin de tour.
- Édition exacte avec préconditions, snapshots, diff avant autorisation.
- Index compact des capacités et chargement MCP à la demande.
- Tests de flux fragmentés, interruptions, doublons visuels et concurrence.
- Sorties volumineuses déportées avec pointeurs ; plan visible dérivé du journal.

## Constats vérifiés dans le code initial

| ID | Gravité | Cause et conséquence | Localisation |
|---|---|---|---|
| C01 | critique | IDs `fenced-0`/`invoke-0` réutilisés ; confusion entre appels et reçus historiques | engine, pager |
| C02 | critique | Annotation d'incertitude sans clôture des tool_calls : messages invalides à la reprise | pager.materialize |
| C03 | haute | Détection de répétition après l'effet, résultat ancien choisi en premier | engine._prior_execution, _loop |
| C04 | critique | Appels synchrones exec/fetch/fichiers bloquent la boucle asyncio et donc interruption, WebSocket, approbations | engine._call_tool |
| C05 | critique | `background` exclut les approbations ; hors bwrap la commande devient synchrone | engine._loop, syscalls.tool_exec |
| C06 | critique | `env python ...`, `rg --pre=...` automatiquement jugés read-only ; reproduit sans exécuter la charge | syscalls.is_safe_readonly |
| C07 | critique | fcntl requis sur Windows ; écriture reproduite en échec ModuleNotFoundError | syscalls._locked_update |
| C08 | haute | bash/python3 imposés, killpg et noms de snapshot incompatibles chemins Windows | syscalls, journal |
| C09 | critique | Ligne déchirée ignorée mais non réparée : append concatène le nouveau reçu à la corruption | journal.Session |
| C10 | critique | État en mémoire modifié avant écriture durable, absence de verrou de journal | journal.emit |
| C11 | critique | Undo tronque avant sélection du snapshot ; snapshots comptés plutôt que max+1 ; `missing` jamais restauré | journal, daemon.undo, tui |
| C12 | critique | Rewind supprime tous les nouveaux fichiers non suivis, y compris fichiers créés par un tiers | journal.restore |
| C13 | haute | Chemin session non validé ; mémoire protège `/` mais pas traversal Windows ou symlinks | journal.Session, memory.read/write |
| C14 | haute | UTF-8 non explicite ; fichier vide absent non créé ; permissions et newline final perdus | syscalls._locked_update/edit, journal |
| M01 | critique | Seuil réel 40 000 tokens approximatifs, pas 500 000 ; aucune prise en compte du système, schémas ou plafond du modèle | pager.budget |
| M02 | critique | Résumé dédié omet user/objective et résumés précédents, tronque 60k caractères ; contraintes perdues | engine._dedicated_compaction |
| M03 | critique | Passes profondes suppriment plus d'événements avec le même résumé non actualisé | engine._apply_compaction |
| M04 | haute | Résultats non-zéro marqués `ok but exit=...`, résultats réussis sans détail, 60 dernières opérations seulement | engine._build_facts |
| M05 | haute | Projection modifie `paged` : deux lectures identiques produisent des contextes différents | pager.materialize |
| M06 | haute | Supersession floue par trois mots/sous-chaîne transforme hypothèses/résumés en « ground truth » | memory |
| M07 | haute | Pas de provenance précise, transactions ni gestion des conflits ; extraction regex automatique intersessions | memory.absorb/remember |
| M08 | haute | Objectif coupé à 400 caractères ; tout mot <=3 lettres traité comme continuation ; plan vide ignoré | engine.chat, _loop |
| L01 | haute | RPC MCP sans lecteur central, pagination ni erreurs isError ; notifications prolongent timeout | linker.MCPClient |
| L02 | haute | Schémas récursifs bouclent ; Any non importé ; résolution ref échappée incomplète | linker.sanitize_schema |
| L03 | haute | Remontage fuit les processus, pas de montage temporaire, skills perdus après compression | engine, linker |
| L04 | haute | Nouvelle Engine par tour perd les handles vivants des sous-agents et les caches ; statuts reconstruits « running » sans tâche | daemon/serve/tui/gui |
| P01 | haute | REPL thread continue les effets après timeout et partage un namespace sans verrou | syscalls.tool_py |
| P02 | haute | Parse fenced/XML même en mode natif : exemples de documentation potentiellement exécutés | engine._loop |
| P03 | haute | Vision probe construit content mais convertisseur ne lit que text ; image probe non transmise | client.probe, _ir_to_* |
| P04 | haute | Plafond sortie 128k généralisé sans catalogue ; health pas indexé par endpoint | client |
| I01 | critique | Deux connexions attach peuvent créer deux Workers ; mutation de session pendant run (serve/GUI) | daemon.Registry, serve, gui |
| I02 | critique | Après erreur d'envoi TUI démarre localement sur le même journal : exécution potentiellement double | tui._remote_send_chat_async |
| I03 | haute | Changement session laisse cwd/queue/stream périmés ; /new local pendant travail | tui |
| I04 | haute | WebSocket local accepte origine navigateur arbitraire ; aucune authentification en bind public | daemon, serve |
| I05 | haute | Redaction limitée text/preview, arguments/diff/facts non filtrés | journal.emit |
| I06 | moyenne | Tests sans assertions ou `or True` ; aucune CI portable ; minimum Textual incohérent avec APIs utilisées | bench, pyproject |

## Baseline exécutée

Windows Python 3.12.14 : environnement virtuel installé, compileall kern/bench
réussi (un SyntaxWarning de fixture). Reproduction C06 et C07 confirmée.
WSL Ubuntu-24.04 détecté après exécution hors sandbox : Linux est disponible,
son exécution de tests reste à faire. Aucun score LLM de cette mission encore
mesuré. Les conclusions de l'ancien audit « tous corrigés » sont contredites
par plusieurs chemins du code actuel ; elles ne sont pas reprises comme preuve.

## Stratégie de vérification

Suite pytest isolée (tmp_path, modèle simulé, MCP simulé, ports éphémères),
tests d'intégration transport/daemon/TUI/web, fautes injectées sur stockage,
exécution Windows et WSL. Évaluations réelles séparées des tests déterministes,
avec indisponibilité fournisseur rapportée distinctement d'un échec produit.
