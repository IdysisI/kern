# Conception Kern

## Architecture visée

Conserver le moteur Python et les adaptateurs. Un Worker conserve un runtime par
session (montages, sous-agents, caches) partagé entre les Engine de chaque tour ;
TUI et web passent par le même daemon, le point d'entrée serveur historique en est
un alias. La classe de connexion historique reste disponible pour compatibilité. Le
journal reste la source récupérable ; la projection est déterministe. Les choix
ci-dessous synthétisent les sources de RECHERCHE.md et traitent AUDIT.md.

## Mémoire

1. Journal UTF-8 durable, verrouillé, append seulement après validation ; réparation
   de queue déchirée avant append, archive du fragment. IDs internes uniques par
   événement assistant. Reçus typés : succeeded, failed, denied, uncertain.
2. Conserver objectif, plan, contraintes utilisateur et reçus séparés de tout récit.
   Une action sans reçu reste incertaine. Les tool_calls interrompus reçoivent un
   résultat synthétique explicitement non exécuté/incertain pour un protocole valide.
3. Remplacer le piggyback et les passes profondes par des épisodes incrémentaux :
   plier uniquement des groupes complets, indexer leurs plages, archiver l'original.
   Résumé dédié avec tous les messages de sa plage et contrat JSON validé ; fallback
   déterministe récupérable. Aucune plage supprimée non couverte.
4. Avant chaque appel : budget incluant système, schémas, marge de sortie et vue.
   Garder état de travail et dernières observations ; déporter résultats volumineux
   avec chemins exacts, rechercher épisodes pertinents ; ne jamais muter les événements
   pendant la projection. Arrêt explicite si les instructions seules ne tiennent pas.
5. Mémoire de projet SQLite transactionnelle : IDs, texte, sujet, clé explicite,
   source complète, dates, états. Aucune supersession inférée par mots communs.
   Compatibilité lecture des anciens Markdown ; import attribué « legacy ».
   Les résumés de session ne deviennent pas automatiquement des faits durables.

## Exécution et organisation

Les appels synchrones quittent la boucle UI ; les effets en cours restent supervisés
jusqu'à leur reçu, y compris lors d'une interruption. Les sous-agents vivants restent
dans le runtime de session, les sous-agents sans runtime sont déclarés interrompus.
Un appel répété n'est jamais rendu idempotent par un cache aveugle : avertissement
avant dispatch et blocage des duplications mutantes injustifiées dans le même tour.
Plan validé avec états explicites ; clear de liste pris en compte. Fin de boucle
ne signifie pas objectif vérifié : la preuve doit venir des outils.

Revue de fin : après des opérations à effets, une requête auxiliaire au même modèle
compare réponse proposée, objectif, plan et reçus. Contrat JSON `verdict`, `reason`,
`next_step`; verdicts complete/needs_work/blocked. Deux revues au maximum par tour
pour éviter une boucle de critiques sans progrès. `needs_work` réinjecte un point
précis à résoudre ; `blocked` doit rester explicite. En cas de JSON invalide ou
d'indisponibilité, contrôle déterministe du plan et des effets incertains, statut
de revue non vérifié. Cette critique n'est jamais présentée comme un test réussi.

## MCP

Index noms/descriptions au démarrage, zéro serveur lancé ni schéma monté. Montage
de session conserve clients et schémas entre tours de la même session. Reprendre
explicitement une ancienne session peut restaurer son montage ; créer ou forker
une nouvelle session repart sans MCP. Montage temporaire limité au tour puis
démontage dans finally. Aucune configuration utilisateur réécrite par le montage.
Un lecteur RPC distribue réponses par ID ; listes paginées, erreurs explicites,
fermeture attendue et nettoyage sur initialisation échouée.

## Stockage et portabilité

Verrou de fichier portable (fcntl POSIX, msvcrt Windows), remplacement atomique
unique dans le même répertoire, UTF-8 explicite. Snapshots nommés par hash, absence
restaurable, jamais suppression large de fichiers non suivis. Shell Bash sous Linux,
PowerShell sous Windows ; sys.executable pour Python. REPL dans sous-processus
arrêtable, pas de thread fuyant après timeout.

## Interfaces et validation

TUI : espace conversation et panneau permanent état/plan/capacités ; navigation
clavier, historique inspectable, résultat/erreur cohérents, déconnexion sans replay.
Web : assets locaux, panneau sessions, sélection modèle, flux, outils/diffs,
approbations, interruption, fork/undo, contexte et état ; reconnect sans nouvel envoi.
Serveur loopback et contrôles Origin/Host pour éviter pilotage depuis un site tiers.
Tests de cœur avant refonte visuelle ; tests intégration UI/transport ensuite.

## Critère de livraison

Rapporter exactement le périmètre implémenté, les vérifications exécutées et les
limites. La perfection universelle et une amélioration de tous les modèles ne sont
pas déductibles d'une suite verte. Aucun benchmark historique recyclé comme résultat.

## Contrats confrontés aux fournisseurs

Les essais VSLLM ont confirmé que des champs de résumé demandés comme chaînes
peuvent revenir comme listes de chaînes. Le parseur normalise ces formes et null
vers une représentation textuelle, conserve les incertitudes et rejette les
objets/nombres imprévus. Pour une revue complète ou bloquée, next_step:null signifie
absence d'étape ; needs_work exige toujours une étape textuelle non vide.
Ce traitement ne transforme jamais la conclusion du modèle en reçu d'exécution.

Les notes SQLite ont une adresse note:<id> explicite dans le contrat de l'outil,
l'index et les résultats. Elles ne sont pas des fichiers Markdown. Ce point évite
que le modèle tente de reconstruire un chemin de stockage imaginaire.
