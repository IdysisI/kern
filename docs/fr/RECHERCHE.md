# Recherche comparative pour Kern

## Conclusion de conception

Un résumé LLM ne peut pas être la source d'autorité sur les actions exécutées.
La combinaison retenue est un journal durable, des reçus structurés, un plan
explicite, des épisodes référençant leurs sources, une recherche à la demande et
un contexte construit avant chaque requête. C'est une synthèse d'ingénierie,
pas une technique scientifique nouvelle ni une supériorité mesurée de Kern.

## Comparaison

| Approche/source primaire | Force | Faiblesse ou limite pour Kern | Décision |
|---|---|---|---|
| [MemGPT, Packer et al., 2023](https://arxiv.org/abs/2310.08560) | Hiérarchie mémoire et accès externe au-delà de la fenêtre | Le modèle doit gérer correctement ses mouvements mémoire ; petits modèles fragiles | Déportage géré par moteur, récupération explicite |
| [A-MEM, Xu et al., 2025](https://arxiv.org/abs/2502.12110) | Notes structurées, liens et évolution de la mémoire | Liens et mises à jour générés ne prouvent pas les faits ; complexité de réconciliation | Provenance/version explicites ; pas de supersession floue |
| [MemGate, Zhang et al., 2026](https://arxiv.org/abs/2606.06054) | Admission conditionnée par requête et fiabilité, pas seulement similarité | Porte neuronale nécessitant données/évaluation ; un index de fichiers n'est pas MemGate | Souvenirs comme données attribuées, jamais instructions ; ne pas revendiquer implémentation MemGate |
| [LCM, Ehrlich et Blackman, 2026](https://arxiv.org/abs/2605.04050) | DAG de résumés et pointeurs vers originaux ; orchestration déterministe | Récupérabilité sans perte ne signifie pas résumé sans perte ; résultats OOLONG propres au dispositif étudié | Épisodes immuables avec plage source, aucun effacement du journal |
| [Lost in the Middle, Liu et al., 2023](https://arxiv.org/abs/2307.03172) | Montre les limites de récupération dans de grands contextes | Étude de modèles et tâches spécifiques ; ne fixe pas un seuil universel pour 2026 | Contexte centré sur tâche, pas remplissage maximal de la fenêtre |
| [ReAct, Yao et al., 2022/2023](https://arxiv.org/abs/2210.03629) | Boucle action/observation révisant les décisions avec l'environnement | Pas de garantie de durabilité, de non-répétition ni de vérité | Garder boucle simple avec reçus fiables |
| [Reflexion, Shinn et al., 2023](https://arxiv.org/abs/2303.11366) | Retour sur échecs conservé comme mémoire épisodique, sans changer les poids | Auto-évaluation susceptible de renforcer une fausse explication | Réflexion seulement sur retours concrets ; pas d'appel rituel à chaque étape |
| [Context engineering, Anthropic, 2025](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | Outils peu nombreux, chargement à la demande, notes et contexte ciblé | Notes/résumés peuvent omettre détails ; aucun nombre magique de tokens | Réserver système/outils/sortie ; pin du plan et preuves |
| [Long-running harnesses, Anthropic, 2025](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) | Fichier de progression, fonctionnalités vérifiées, passage de relais explicite | Dépend de bons tests ; « done » déclaré peut être faux | Séparer tâche déclarée finie et preuve de validation |
| [Harness design, Anthropic, 2026](https://www.anthropic.com/engineering/harness-design-long-running-apps) | Séparation planification/production/évaluation sur tâches longues | Coût/latence de coordination, résultats non universels | Architecture extensible, validation observable en premier |
| [Persistance LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence) | Checkpoints et écritures intermédiaires pour reprise | Un checkpoint ne rend pas un effet externe exactement-une-fois | Intentions/reçus et état « incertain », pas de répétition automatique |
| [MCP outils](https://modelcontextprotocol.io/specification/2025-11-25/server/tools), [cycle de vie](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) | Schémas explicites, pagination, erreurs métier distinctes du protocole | Compatibilité serveur/version variable, outils non fiables par défaut | Lecteur RPC unique, timeout global, pagination, isError, nettoyage |
| [SQLite isolation](https://www.sqlite.org/isolation.html), [WAL](https://www.sqlite.org/wal.html) | Transactions et lecteurs concurrents, bibliothèque standard | Un écrivain à la fois ; contraintes FS/réseau et fichiers auxiliaires WAL | SQLite pour index mémoire local, journal source conservé |
| [Évaluations agents, Anthropic, 2026](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) | Évaluer résultat et trajectoire, distinguer infrastructure et comportement | Un juge LLM seul ou tests saturés masquent les régressions | Fautes injectées, invariants, intégrations réelles, évals modèle séparées |
| [Python subprocess asyncio](https://docs.python.org/3/library/asyncio-subprocess.html) | Exécution non bloquante avec gestion timeout | Pipes et processus enfants exigent drainage et arrêt explicites | Wrapper portable et supervision des processus |

## Conséquences pratiques et limites

Le choix d'un petit noyau est compatible avec une mémoire sophistiquée : conserver
plus de données sur disque ne signifie pas charger plus d'outils ou de texte dans
chaque requête. Un index lexical inspectable privilégie d'abord chemins, IDs et
termes exacts ; il ne remplace pas la recherche sémantique pour les paraphrases.
Une future couche d'embeddings devra démontrer son gain sur un jeu de rappel.

La mémoire de projet doit distinguer source utilisateur, observation d'outil et
note du modèle. Une note du modèle reste une assertion. Une réussite d'écriture
prouve les octets écrits, pas le bon fonctionnement de l'application. Une commande
avec exit=0 prouve sa terminaison, pas la complétude d'une mission. Un timeout
ne prouve jamais l'absence d'effet externe.

Les chiffres publiés des articles ne sont pas transposés à Kern. Les gains sur
petits/grands modèles devront être mesurés ici avec mêmes tâches, même environnement
et plusieurs répétitions. Les sources récentes peuvent évoluer ; les versions MCP
ci-dessus sont précisément datées. La lecture de MemGate confirme que l'ancien
commentaire « admission map = MemGate » était une attribution excessive.

## Appels auxiliaires

Ils sont justifiés pour produire un passage de relais borné à partir d'un épisode
nouveau, résoudre une contradiction ou vérifier une sortie complexe. Leur nombre
n'est pas un objectif d'optimisation. On conserve le même modèle choisi par
l'utilisateur. Un résumé échoué ne doit pas effacer ses entrées. Les garanties
de stockage et d'exécution restent indépendantes de la qualité de cet appel.

## Vérification des adaptateurs pendant l’implémentation

La documentation officielle [Anthropic Streaming](https://platform.claude.com/docs/en/build-with-claude/streaming) décrit les blocs indexés, la fin `message_stop` et le caractère cumulatif de l’usage. Kern attend une fin complète avant de libérer les appels et ne somme pas les cumuls. Les formats image sont comparés à [Anthropic Vision](https://platform.claude.com/docs/en/build-with-claude/vision) et à la [référence Chat Completions](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create). Les images issues d’outils sont transmises comme observations utilisateur après les reçus textuels dans l’adaptateur OpenAI. Ces contrats sont testés avec des réponses HTTP simulées ; la conformité d’un proxy tiers reste à vérifier contre ce proxy.
