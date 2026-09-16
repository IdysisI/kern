# Kern 0.3 — rapport de refonte

Mission commencée le 13 septembre 2026, poursuivie le 14 septembre.
Branche : `audit/kern-reliability-2026-09-13`.

## Résultat et degré de confiance

Le moteur, le stockage, les adaptateurs et la mémoire ont été corrigés en profondeur.
La compression ancienne est remplacée, le montage MCP par session est conservé,
la TUI possède un panneau de travail et une application web utilisable a été créée.
Les changements sont accompagnés d'une suite isolée, de tests Windows/Linux, d'un
parcours graphique réel et d'une distribution installée hors de l'arbre source.

**La perfection et une supériorité cognitive sur tous les modèles ne sont pas
établies.** Après les tests simulés, le proxy VSLLM fourni par l'utilisateur a
permis des essais fournisseurs réels, décrits ci-dessous. Ils couvrent un scénario
synthétique reproductible ; aucune comparaison statistique avec une baseline n'a
été exécutée. Les limites de validation et d'architecture font partie du résultat.

Les détails de l'état initial figurent dans [AUDIT.md](AUDIT.md), les comparaisons
et sources dans [RECHERCHE.md](RECHERCHE.md), et les décisions dans
[DESIGN.md](DESIGN.md). `MISSION.md` conserve la progression et les corrections
trouvées pendant les vérifications.

## Audit : fondations conservées

Le journal d'événements, la séparation moteur/interface, l'édition avec préconditions,
les snapshots et l'index compact de capacités étaient de bonnes fondations. Les
intentions avant effets et la lecture ciblée pouvaient servir un agent fiable.
En revanche, les garanties annoncées étaient parfois contredites par le code :
IDs réutilisés, comportements bloquants, projections variables, Windows non pris
en charge, deux cycles de serveur et aucune application web.

Le seuil de compression réel était **40 000 tokens estimés**, et non 500 000.
Les problèmes majeurs venaient aussi de ce qui était perdu ou réinterprété : demandes
omises des résumés dédiés, passes profondes réutilisant un résumé périmé, succès
mal classés et notes transformées en « ground truth » par similarité de mots.

L'inventaire a couvert tous les chemins ; les modules de production ont été lus,
les scripts et rapports historiques inspectés. Les expériences `scratch/` existaient
avant la mission et sont préservées. Les anciens résultats de benchmarks ne sont
pas réutilisés comme preuve de la nouvelle version.

## Causes racines et corrections

| Audit | Cause racine | Correction livrée |
|---|---|---|
| C01, C02 | IDs réutilisés et appels sans résultat à la reprise | IDs internes par événement, reçus associés dans l'ordre, renumérotation de collisions historiques dans la projection, résultat synthétique explicitement incertain |
| C03 | Répétition détectée après effet | Blocage avant effet dans le tour pour doublon réussi, et entre tours pour effet incertain ; répétition volontaire justifiée explicitement |
| C04 | Outils synchrones sur la boucle asyncio | Exécution en thread supervisé ; annulation coopérative d'exec/REPL ; résultat journalisé avant clôture |
| C05 | Background contournant autorisation et devenant synchrone hors bwrap | Autorisation conservée, lancement réellement asynchrone, handle et journal de sortie |
| C06 | Heuristiques de commandes read-only trop permissives | Liste conservatrice ; refus des wrappers et options exécutant des programmes, ainsi que des métacaractères shell |
| C07, C08 | fcntl/bash/python3/killpg imposés | Verrou Windows/POSIX, PowerShell/Bash, interpréteur courant, snapshots hashés, arrêt des enfants vérifié sur les deux OS |
| C09, C10 | Append après ligne déchirée et mutation mémoire avant disque | Archivage de queue, réparation avant append, verrou, flush/fsync, état mis à jour après succès durable |
| C11, C12 | Mauvais ordre d'undo et suppression de fichiers tiers | Restaurer avant tronquer ; absences explicites ; snapshots inactifs après abandon ; jamais de nettoyage global des fichiers non suivis |
| C13 | Chemins non validés | Identifiants de session validés ; namespace mémoire protégé contre traversal Windows/POSIX et sorties par symlink |
| C14 | Encodage et remplacement de fichiers | UTF-8, fichier vide créé, CRLF et mode existant préservés ; mode du snapshot restauré ; sortie PowerShell en UTF-8 |
| M01–M03 | Compression naïve et couverture de résumé incorrecte | Épisodes incrémentaux, plages exactes, sources immuables, contrat JSON, index de repli ; plus de piggyback ni de « deepening » réutilisant un résumé |
| M04, M05 | Reçus faux/insuffisants et projection avec effets de bord | Statuts explicites, preuves indépendantes, projection répétable ; données non mutées par consultation |
| M06, M07 | Supersession floue et promotion automatique de récits | SQLite transactionnel, provenance, états ; supersession par clé explicite seulement ; anciennes notes non vérifiées |
| M08 | Objectif coupé, faux « continue », plan vide ignoré | Objectif complet, liste explicite des continuations, plan vide appliqué, états pending/active/done/blocked |
| L01 | RPC MCP séquentiel fragile | Lecteur central, futures par ID, pagination, timeout borné, isError remonté, arrêt attendu |
| L02 | Schémas récursifs et références mal résolues | Détection de cycles et résolution des références échappées ; schéma dégradé borné plutôt que récursion infinie |
| L03, L04 | Runtime perdu entre Engine et remontages fuyants | Runtime de session partagé ; montage temporaire nettoyé en finally ; nouvelle session/fork vide ; sous-agent sans tâche vivante signalé interrompu |
| P01, P02 | REPL thread inarrêtable et exemples exécutés comme outils | REPL dans un processus arrêtable ; fallback fenced/XML uniquement quand le protocole natif est désactivé |
| P03, P04 | Image probe non transmise, PNG corrompu, limites inventées | PNG valide, conversions multimodales, profils par endpoint, limites du catalogue ou configuration explicite, sortie bornée à la place restante |
| I01, I02 | Plusieurs Workers et repli local après envoi incertain | Exclusion de tours concurrents, registre sérialisé, bail de session interprocessus, aucun renvoi local après livraison incertaine |
| I03 | État TUI périmé et mutation pendant exécution | Garde de tour, reprise de cwd/queue, filtrage des événements de session, suppression de doublon de file d'attente |
| I04, I05 | Pilotage navigateur tiers et secrets imbriqués | Serveur loopback, contrôle Host/Origin, CSP ; redaction récursive des champs du journal |
| I06 | Tests trompeurs et dépendances incohérentes | Suite pytest isolée, intégrations, CI Windows/Linux, versions Textual/websockets cohérentes, extra test et lockfile |

Autres défauts trouvés pendant la refonte :

- **Contrats auxiliaires trop étroits** : MiniMax et Haiku renvoyaient des listes
  textuelles pour les épisodes et `next_step: null` pour une revue complète. Ces
  formes équivalentes sont normalisées sans accepter d'objets arbitraires ; les
  incertitudes restent conservées. Une revue needs_work exige une étape concrète.
- **Adresses mémoire ambiguës** : le schéma ne décrivait que les fichiers Markdown,
  alors que SQLite renvoyait des identifiants. Schéma, index et résultats exposent
  `note:<id>` comme adresse de lecture ; les identifiants sont aussi recherchables.
- **Sortie CLI Windows redirigée** : un symbole Unicode du modèle provoquait
  UnicodeEncodeError sous un encodage ANSI. Les flux CLI sont configurés en UTF-8,
  avec une régression exécutée dans un sous-processus au flux initial ASCII.
- **Artefacts périmés après undo** : le numéro d'événement réutilisé pointait vers
  un ancien fichier. Sorties et épisodes sont désormais adressés par contenu.
- **Snapshot avant verrou** : une autre écriture Kern pouvait s'intercaler entre
  sauvegarde et mutation. Le snapshot est pris sous le verrou de l'écriture réelle.
- **Copie TUI** : Textual renvoyait une représentation interne du rendu ; le texte
  Markdown est maintenant extrait de son contenu.
- **Fork multi-client** : changer le Worker d'origine déplaçait les autres clients.
  Seul le client demandeur rejoint désormais le nouveau Worker.
- **Erreur RPC** : une entrée invalide fermait la connexion sans req_id. Les erreurs
  de requête restent corrélées et la connexion demeure utilisable.
- **SSE incomplet** : fermeture réseau assimilée à une fin normale. Les appels
  incomplets ne sont plus dispatchés ; les arguments invalides restent explicites.
- **Usage Anthropic** : les cumuls pouvaient être additionnés. Un total de message
  est construit ; les appels mémoire/revue sont aussi comptabilisés.
- **Fin de tour trompeuse** : limite de pas, sortie tronquée, blocage et revue
  indisponible sont distincts de done. Le CLI transmet un code de sortie non-zéro.
- **Approbation web réutilisée** : la valeur précédente d'un dialogue pouvait
  survivre à sa fermeture. Chaque demande repart sur le refus par défaut.
- **Schémas MCP avec noms invalides** : noms compatibles et route explicite,
  sans déduction ambiguë à partir d'un séparateur.
- **Base64 pris pour du texte** : l'estimation sépare l'image de son encodage de
  transport ; l'allocation image reste une estimation configurable.
- **Fausse capacité audio** : la charge était produite mais ignorée par les
  adaptateurs. La lecture annonce maintenant ses limites et conserve les métadonnées.
- **Distribution** : le sdist embarquait les expériences privées non suivies.
  Les inclusions sont maintenant explicites ; le contenu a été inspecté.

## Mémoire et montage d'outils : architecture finale

Le journal UTF-8 reste la source primaire. Une intention dit « dispatché », un
reçu dit ce qui a été observé, et un résumé ne peut pas transformer une hypothèse
en preuve. Les gros contenus restent sur disque avec une référence stable. Undo
archive la branche annulée ; les sauvegardes d'avant restauration restent disponibles.

`ContextManager` assemble la requête complète et réserve sa sortie. La maintenance
avance par groupes d'échanges, y compris dans un très long tour. Chaque épisode
couvre une plage identifiée et possède son original immuable. L'appel auxiliaire
reçoit des lots bornés, incluant les fragments des longues demandes utilisateur.
Un JSON invalide donne un index de sources avec erreur, jamais un résumé inventé.

La projection garde objectif, plan, reçus récents/incertains, échanges récents et
épisodes sélectionnés lexicalement. L'index complet et `memory(action="history")`
permettent de récupérer les sources. Les notes intersessions sont des assertions
attribuées dans SQLite, avec transactions, clés, supersession explicite et tombstones.
Les anciennes notes Markdown restent accessibles sans conversion en faits fiables.

Le design combine les intérêts de [MemGPT](https://arxiv.org/abs/2310.08560)
(gestion explicite de plusieurs niveaux), de
[LCM](https://arxiv.org/abs/2605.04050) (sources récupérables et organisation du contexte),
de [ReAct](https://arxiv.org/abs/2210.03629) (boucle action/observation) et de
[Reflexion](https://arxiv.org/abs/2303.11366) (critique ciblée). Il ne prétend pas
réimplémenter leurs méthodes complètes ni reproduire leurs résultats publiés.
La persistance et les preuves indépendantes suivent aussi les principes de
[LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence) et des
[évaluations d'agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents).
Les forces et faiblesses de ces approches, A-MEM, MemGate et des autres sources
figurent dans RECHERCHE.md.

Après des effets, une revue de fin au même modèle confronte le rapport proposé à
l'objectif, au plan et aux reçus. Elle peut demander un contrôle manquant ; elle ne
peut pas exécuter d'outil ni créer de preuve. Deux revues au maximum évitent une
boucle de critiques. Le nombre total de requêtes n'est pas optimisé au détriment de
la qualité ; cette borne limite un comportement sans progrès, pas le travail utile.

Pour MCP, l'index seul est visible initialement. Monter charge les schémas et un
client dans le runtime de session ; monter temporairement limite la durée au tour.
Aucun montage global n'est écrit. Une nouvelle session ou un fork démarre vide ;
une reprise explicite de la même session peut restaurer son état. Le contrat stdio
est basé sur la [spécification MCP](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).

## Architecture des interfaces

`engine` orchestre ; `client` adapte les protocoles ; `storage` et `journal` rendent
les effets traçables ; `context`/`pager` assemblent le contexte ; `memory` conserve
les notes ; `linker` gère les capacités ; `syscalls` et `repl_worker` exécutent.
`daemon` possède les runtimes. `web` sert HTTP et WebSocket sur le même port local.

La TUI conserve clavier, sélection de modèle, approbations et reprise, avec un panneau
permanent sur grand terminal. Le web fournit une conversation complète, historique,
sessions, modèle, outils/diffs, approbations, interruption, plan, reçus, duplication,
undo et reprise. Aucun framework frontend ni service externe n'est requis. Les
assets sont dans le wheel. L'interface Qt reste optionnelle et préservée.

## Ajouts, retraits et compatibilité

Ajoutés : tests isolés et CI, primitives de stockage portables, épisodes attribués,
notes SQLite, revue de fin, protection contre les répétitions, web complet, panneau
TUI, navigation historique, contrôle Origin/Host, distribution explicite et CLI aidé.

Retirés : piggyback et passes profondes de compression, supersession floue, promotion
regex des résumés en faits, suppression globale de fichiers non suivis lors du rewind,
rejeu local après envoi incertain, chargement audio inopérant. Leur suppression
traite une cause d'erreur ; aucun montage dynamique MCP n'a été retiré.

Compatibilité : anciens journaux et anciennes notes restent lisibles ; les profils
health non isolés par endpoint sont remplacés par de nouvelles mesures. Les scripts
historiques de benchmarks sont conservés, mais ceux ciblant l'ancienne compression
ne décrivent plus le contrat courant. Le serveur `serve` lance le même runtime que web.

## Lancement et tests

Voir [README.md](README.md) et [TESTING.md](TESTING.md) pour les commandes complètes
Windows et Linux, configuration fournisseur/MCP, installation du wheel et test
visuel isolé. `python -m kern` lance la TUI ; `python -m kern web` sert
`http://127.0.0.1:8766`. Un fournisseur configuré et joignable est nécessaire pour
les conversations réelles.

## Validation exécutée le 14 septembre 2026

| Environnement réel | Exécution | Résultat |
| --- | --- | --- |
| Windows, Python 3.12.14, source éditable | Suite pytest | 58 réussis, 11,39 s |
| Ubuntu 24.04 sous WSL, Python 3.12.3 | Suite pytest | 58 réussis, 13,44 s |
| Windows, Python 3.11.15, wheel installé, répertoire hors source | Suite pytest en mode importlib | 58 réussis, 11,53 s |

L'import du troisième environnement pointe bien dans `site-packages`. Ce contrôle
vérifie aussi la distribution, au lieu de tester accidentellement le code source.
Les scénarios comprennent les arbres de processus réellement arrêtés, interruption,
REPL isolé, undo, écritures Unicode/CRLF, reçus et répétitions, mémoire et contexte,
MCP stdio dans un processus réel, adaptateurs de protocole simulés, RPC HTTP/WS et TUI.

`compileall`, l'aide CLI, `node --check kern/static/app.js`, `uv lock --check`
et `uv pip check` sur l'installation isolée réussissent. La distribution 0.3.0
contient 25 fichiers dans le wheel, dont les trois assets web ; le sdist contient
39 fichiers et les rapports, sans `scratch/`, environnement virtuel ni données de test.
La CI Windows/Linux et Python 3.11–3.13 est préparée ; elle n'a pas été exécutée ici.

Le parcours web a été exécuté dans le navigateur à 1042 px puis à 390 × 844 px :
conversation, approbation avec diff, écriture UTF-8, Markdown, plan, reçus et
rechargement. Le contenu disque a été contrôlé : une seule écriture après reprise.
Le panneau d'état est accessible sur petit écran et du HTML issu du modèle reste
inerte. Le modèle de ce parcours est un scénario contrôlé, pas un fournisseur réel.

Ces résultats établissent des comportements reproductibles du logiciel. Ils sont
complétés par les essais VSLLM ci-dessous ; ils ne démontrent pas une supériorité
statistique du harness ni une fiabilité universelle.

## Essais réels avec VSLLM

L'adresse fournie par l'utilisateur a rendu le catalogue et les conversations
accessibles. Les identifiants ci-dessous sont ceux du proxy ; l'identité du modèle
sous-jacent et les fenêtres annoncées ne sont pas authentifiées indépendamment.
Chaque essai utilise un projet et un KERN_HOME temporaires, des données synthétiques
et le script opt-in `tests/live_eval.py`. Aucune donnée du projet utilisateur n'est
nécessaire au scénario.

Les 12 critères portent sur le contenu binaire exact, la source d'épisode et son
contrat, une écriture unique après réouverture, l'absence de modification ultérieure,
le rappel après épisode, le plan terminé, la note durable, son rappel dans une
nouvelle session, l'absence de montage hérité, les fins de tour et les reçus d'outils.
L'épisode est explicitement produit par le chemin mémoire réel ; cet essai ne mesure
pas une conversation de centaines de milliers de tokens.

| Modèle exposé | Système / protocole | Résultat final | Requêtes / durée |
| --- | --- | --- | --- |
| MiniMax-M2.7 | Windows / OpenAI | 12/12 critères, trois tours done | 18 / 66,70 s |
| MiniMax-M2.7 | Ubuntu/WSL / OpenAI | 12/12 critères, trois tours done | 17 / 60,14 s |
| claude-haiku-4-5-20251001 | Windows / Anthropic | 10/12 : reprise interrompue par timeout puis 503 ; premier et dernier tours done | 16 / 272,27 s |
| claude-sonnet-4-6 | Windows / Anthropic | Dernier essai arrêté à la sonde : 503, aucun tour exécuté | 1 / 89,25 s |

Les requêtes incluent les sondes de capacités et les appels auxiliaires de chaque
exécution indiquée ; elles ne représentent pas le total de toutes les tentatives
de développement. Un premier parcours Sonnet avait réussi 11/11 critères définis
à ce moment, en 16 requêtes et 171,94 s, avant les derniers ajustements de contrat.
Il n'est pas présenté comme une validation complète de la dernière révision.

Les essais diagnostiques Haiku et MiniMax ont exposé `next_step:null`, les listes
JSON d'épisode et les adresses SQLite ambiguës : ces défauts ont été corrigés puis
couverts par les tests automatisés. Le dernier essai Haiku produit bien un épisode
valide et conserve une seule écriture ; son rappel après épisode n'a pas abouti à
cause du fournisseur. Le rappel dans une nouvelle session réussit ensuite.
Le message fournisseur observé est `503: no upstream keys available` ; aucune
correction locale ne peut garantir la disponibilité de ces clés amont.

Les métriques et les empreintes des résultats bruts figurent dans
[tests/results/vsllm-2026-09-14.json](tests/results/vsllm-2026-09-14.json).
Les journaux détaillés et réponses auxiliaires restent dans les projets temporaires
indiqués par les résultats locaux `.test-runs/live-*.json`. Les premières tentatives
avec approbation mal configurée dans le script, puis sortie ANSI du script, sont
exclues des tableaux ; elles sont consignées dans MISSION.md.
Aucune amélioration statistique sur une baseline n'est déduite de ces essais.

## Limites et suite du travail

1. Essais réels limités à un scénario synthétique par modèle, sans baseline ni
   répétitions statistiques : aucune preuve d'amélioration quantitative générale
   des petits ou grands modèles, ni d'absence universelle de répétitions.
   La protection est exacte sur les arguments ; des commandes différentes mais
   sémantiquement équivalentes nécessitent encore le jugement du modèle.
2. Linux exécuté via Ubuntu 24.04/WSL, pas une installation Arch/CachyOS physique.
   bwrap, Wayland, Qt et les terminaux natifs variés n'ont pas une validation complète.
3. Recherche mémoire lexicale, pas d'embeddings ni de graphe sémantique appris.
   Les résumés restent faillibles et la sélection peut manquer une paraphrase ; les
   sources sont récupérables, ce qui ne garantit pas que le modèle ira les consulter.
4. Estimation de tokens portable, pas comptage exact du fournisseur. Images, schémas
   volumineux et très longues consignes peuvent demander une configuration de fenêtre
   vérifiée. Un dépassement non résolu arrête le tour explicitement.
5. Les effets externes ne sont pas transactionnels : un timeout MCP peut suivre un
   effet réussi. Kern signale l'incertitude ; il ne garantit pas « exactement une fois »
   sur un service tiers. Undo ne peut annuler une commande arbitraire ou un envoi externe.
6. Les verrous sérialisent les écritures Kern ; un éditeur externe non coopérant
   peut concurrencer un fichier. La restauration multi-fichier n'est pas une
   transaction atomique du système de fichiers ; les sauvegardes permettent l'inspection.
7. L'arrêt d'arbres a été testé avec des enfants contrôlés. Les processus volontairement
   détachés et la mort brutale de l'hôte demandent une supervision OS plus forte.
8. MCP stdio livré ; transport distant HTTP et transcription audio restent à construire
   avec des tests dédiés. Les très gros skills sont signalés avec un chemin pour lecture
   de la suite ; le préfixe seul n'est pas présenté comme le document complet.
9. Redaction ciblée, pas classification universelle de secrets. Les logs de processus,
   snapshots et sources sur disque restent des données locales potentiellement sensibles.
10. La CI est configurée, mais aucune exécution GitHub n'a été déclenchée dans cette
    mission. Les résultats locaux et les limites sont les preuves effectivement disponibles.

Il serait donc inexact de livrer l'étiquette « zéro bug dans tous les cas ».
Le résultat est une refonte substantielle vérifiée sur des scénarios définis,
avec des garanties plus nettes et des inconnues rendues visibles.
