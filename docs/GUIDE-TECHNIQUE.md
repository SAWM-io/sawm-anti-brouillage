# SAWM Anti-Brouillage — guide technique

[Présentation en français](../README.md) · [English overview](../README.en.md) · [Grille de validation](validation.md) · [Protocole radio futur](RADIO-VALIDATION.md)

Ce guide détaille le démonstrateur de résilience applicative HTTP, ses commandes, les preuves conservées et le parcours Cloud optionnel. Le nom SAWM Anti-Brouillage désigne ici la continuité d'envoi face aux coupures du banc ; le programme n'agit pas sur une liaison radio.

## Banc et scénario de fautes

Le banc utilise de vraies connexions HTTP sur `127.0.0.1`. Son serveur, ses messages synthétiques, ses fichiers et ses processus lui appartiennent. Il ne modifie aucune configuration réseau système. `lab.py` n'appelle aucun Cloud ; `cloud_control.py` s'exécute séparément avant ou après les essais locaux.

Depuis la racine du dépôt, dans un environnement Python 3.10+ :

```sh
python3 -m venv .venv
. .venv/bin/activate
python lab.py --output-dir output/demo --messages 20 --policy examples/policy.json
```

Le répertoire de sortie doit être nouveau. Le nombre de messages est compris entre 3 et 20 par canal, avec 10 par défaut. Dans le scénario par défaut à 10 messages, deux sont émis avant la coupure, cinq pendant et trois après. À 20 messages, quinze sont émis pendant la coupure, entre les mêmes deux messages initiaux et trois messages finaux.

Les deux groupes utilisent le même serveur, le même port, les mêmes identifiants et contenus ainsi que la même coupure. Le registre du témoin comporte deux canaux distincts : l'unicité porte sur le couple `(canal, identifiant du message)`.

Le banc exécute successivement les opérations suivantes :

1. Créer le témoin HTTP, sa base SQLite et la file durable ; lancer le worker.
2. Émettre les premiers messages. Pour le premier message de chaque canal, committer l'effet puis supprimer volontairement le premier ACK.
3. Arrêter le serveur et émettre les messages prévus pendant cette indisponibilité.
4. Arrêter de force puis relancer le worker en conservant la même file SQLite.
5. Après environ deux secondes de coupure, restaurer le serveur sur le même port et émettre les derniers messages.
6. Attendre le worker dans les limites du budget, comparer ses ACK au registre destinataire et fermer les ressources créées par le banc.

Le budget global est de 30 secondes ; la politique par défaut alloue 25 secondes à la file. Ces limites logicielles ne constituent pas une garantie de temps réel du système d'exploitation.

## Livraison, persistance et idempotence

La file utilise SQLite avec `synchronous=FULL` et conserve les messages, leurs digests, les tentatives et les démarrages du worker. Un verrou SQLite distinct empêche deux workers de traiter simultanément la même file.

Les états d'un message sont `pending`, `inflight`, `acked` et `exhausted`. Une tentative interrompue devient `interrupted_unknown` : la reprise ne suppose pas que l'effet a échoué. Elle conserve le compteur de tentatives et réessaie le même identifiant et le même digest. Le budget temporel initial n'est pas réinitialisé au redémarrage du processus.

Le destinataire confirme l'effet après commit de sa transaction. Une tentative ultérieure avec le même identifiant et le même contenu retrouve cet effet ; elle n'en crée pas un autre. Un identifiant réutilisé avec un contenu différent est refusé. L'émetteur ne passe à `acked` qu'après validation du schéma de l'ACK, de l'identifiant, du digest, de `committed=true` et du booléen `duplicate`.

L'effet unique démontré est une ligne du registre de ce destinataire contrôlé. Il ne constitue pas une garantie d'exécution unique pour tout service HTTP externe.

## Compteurs et délais

| Observation | Interprétation |
| --- | --- |
| Effet unique confirmé | Message enregistré dans le registre du témoin après commit SQLite |
| ACK inconnu ou non reçu | Aucune confirmation reçue ; l'effet peut pourtant avoir été appliqué |
| `not_delivered_at_end` ou `lost_at_end` | Aucun effet correspondant dans le registre final ; pas une preuve de perte irréversible |
| Message encore en file | Message conservé localement, non acquitté ; pas automatiquement perdu |
| `duplicate_requests` | Tentatives reçues en plus pour un identifiant déjà traité |
| `duplicate_effects` | Effets appliqués en plus ; à distinguer des requêtes répétées |

Conserver séparément les tentatives, les ACK et les effets, y compris en cas de budget épuisé ou de récupération incomplète.

`end_to_end_ms` va de la création du message à l'horodatage d'acceptation dans la première transaction ensuite committée par le destinataire, avec la même horloge monotone de l'hôte. Cet horodatage précède la fin du commit ; il ne mesure ni sa fin exacte ni la réception de l'ACK.

Les moyennes des deux canaux ne portent pas sur les mêmes populations : dans la campagne à 20 messages, la moyenne volatile ne couvre que cinq messages arrivés, tandis que la moyenne durable couvre les vingt, dont ceux ayant attendu le retour du service. La différence ne démontre donc pas un gain de vitesse réseau. Le mécanisme accepte du délai pour conserver puis livrer davantage de messages.

## Politique de reprise

La politique initiale est un JSON fermé, vérifié avant le démarrage du worker. Elle définit les plafonds du mode adaptatif optionnel ; le mode statique conserve ses paramètres pendant toute l’exécution. La [politique fournie](../examples/policy.json) et celle de la campagne du 27 octobre ont les mêmes valeurs :

```json
{
  "schema": "resilience-retry-policy/v1",
  "max_attempts": 12,
  "base_delay_ms": 100,
  "max_delay_ms": 1000,
  "request_timeout_ms": 500,
  "total_budget_ms": 25000
}
```

| Paramètre | Bornes acceptées |
| --- | --- |
| `max_attempts` | Entier de 1 à 20 |
| `base_delay_ms` | Entier de 25 à 1000 ms |
| `max_delay_ms` | Entier de 25 à 2000 ms, au moins égal à `base_delay_ms` |
| `request_timeout_ms` | Entier de 50 à 2000 ms |
| `total_budget_ms` | Entier de 1000 à 25000 ms |

Les clés inconnues, doublons de clés JSON, mauvais types et valeurs hors bornes sont refusés. En mode statique, après une tentative non acquittée, le délai est `min(max_delay_ms, base_delay_ms × 2^(tentative − 1))`.

La destination est fixe : l'hôte `127.0.0.1`, le port provisionné par le banc et le chemin `/deliver/durable`. Une politique ne peut fournir ni destination libre ni commande à exécuter.

Le worker peut être appelé directement dans un scénario où le témoin correspondant reste actif :

```sh
python delivery.py --queue output/demo/queue.sqlite
```

Après fermeture de `lab.py`, cette commande ne recrée pas le serveur et ne prolonge pas le budget. Le banc prend lui-même en charge le redémarrage nécessaire à la démonstration.

## Calibration continue locale

Le mode adaptatif est activé explicitement lors de la création d'une file avec `delivery.create_queue(..., adaptive=True)`. Le paramètre vaut `False` par défaut ; `lab.py` conserve son scénario statique durable contre volatil. `delivery.py --queue` reprend automatiquement le mode inscrit dans les métadonnées de la file.

Le module `adaptive_policy.py` est déterministe. Il reçoit uniquement le résultat d'une tentative terminée : ACK déjà vérifié, durée et catégorie d'erreur. Il ne consulte ni le Cloud, ni une horloge, ni le programme des prochaines fautes. Il ajuste le timeout de requête et le délai des reprises suivantes. Il s'agit d'une calibration de livraison HTTP, pas d'une estimation de conditions RF.

### Règles et bornes

| Observation terminée | Mise à jour locale |
| --- | --- |
| ACK vérifié | Mettre à jour la moyenne lissée de latence et sa variation ; réduire de moitié le délai de reprise, avec un minimum de 25 ms |
| Timeout | Doubler le timeout et le délai de reprise dans leurs plafonds ; traiter l'observation comme censurée, sans ajouter d'échantillon RTT |
| Refus et autres échecs de transport ou de protocole | Doubler le délai de reprise, au moins jusqu'à la base initiale et au plus jusqu'au maximum initial |
| Connexion fermée sans ACK | Conserver le plus grand du délai courant et de la base initiale, sans doublement ; laisser le timeout inchangé et ne créer ni ACK vérifié, ni échantillon RTT |
| ACK invalide | Espacer les reprises ; ne pas modifier l'estimation RTT ni réduire le timeout |
| Tentative interrompue avant enregistrement de sa fin | Conserver une tentative de résultat inconnu ; ne pas inventer d'observation pour le contrôleur |

La fermeture d’une connexion sans ACK (`connection_closed_without_ack`) laisse la livraison ambiguë. Elle reste un échec pour les compteurs, mais ne suffit pas à conclure à une congestion ; sa trace porte la raison `ambiguous_ack_no_rtt_sample`. Un délai plus élevé déjà acquis après un timeout ou un refus est conservé.

Le timeout après un ACK vérifié est `ceil(SRTT + 4 × RTTVAR + 25 ms)`, borné entre 50 ms et `request_timeout_ms` de la politique initiale. SRTT utilise un poids de `1/8` pour le nouvel échantillon ; la variation utilise `1/4`. Le premier échantillon initialise SRTT à sa valeur et RTTVAR à sa moitié. L'entrée de l'estimateur est plafonnée à trois fois le timeout initial pour limiter l'effet d'une suspension du processus ; la trace conserve la durée observée.

Le nombre maximal de tentatives, l'échéance monotone et le budget global ne sont jamais étendus. La borne du timeout reste celle de la politique initiale, même si le service devient plus lent. La calibration peut donc échouer ou coûter davantage de tentatives ; le rapport doit le montrer.

### Persistance et reprise

Les nouvelles files portent `adaptive_algorithm: "bounded-rto/v2"` dans leurs métadonnées ; l’identifiant figure aussi dans les limites et les événements du contrôleur. Les anciennes files sans identifiant utilisent `bounded-rto/v1`, avec leur comportement et leur historique préservés.

Les tables `adaptive_state` et `calibration_events` conservent l'état et chaque transition dans la même transaction que la fin de tentative et l'état du message. `queue_summary()` expose ces données sous `adaptive.state` et `adaptive.calibration_trace`.

Au redémarrage, le worker rejoue les observations terminées et compare le résultat à l'état et aux événements persistés. Les tentatives `started` interrompues deviennent `interrupted_unknown` et n'alimentent pas l'estimateur. Cette vérification détecte les incohérences entre état, traces et tentatives ; elle n'authentifie pas une base entière réécrite de façon cohérente par un acteur contrôlant le fichier.

### Comparer une politique fixe et la calibration continue

Exécution courte, avec un répertoire de sortie nouveau :

```sh
python adaptive_lab.py --output-dir output/adaptive-demo --repetitions 1 --messages 20
```

Campagne de trois répétitions par profil et par mode, avec la politique Cloud historique déjà enregistrée :

```sh
python adaptive_lab.py --output-dir output/adaptive-campaign --repetitions 3 --messages 20 \
  --policy docs/measured-2026-10-27/cloud/policy.json
```

La lecture de ce fichier de politique est locale et ne relance aucun appel API. Les deux modes reçoivent les mêmes limites initiales. Le contrôleur local ajuste ses paramètres à l'intérieur de ces limites ; SAWM n'intervient pas entre les tentatives.

La CLI accepte une à trois répétitions et fixe la charge à 20 messages par passage. Trois répétitions produisent 24 passages ; le plafond global de la campagne est de 180 secondes.

La campagne comprend quatre profils : service normal, hausse temporaire de latence, ACK perdus après commit et indisponibilité suivie d'un redémarrage du worker. Les charges de chaque paire fixe/adaptatif partagent les mêmes contenus et les mêmes arrivées prévues ; l'ordre d'exécution des modes est alterné. Le banc connaît le scénario de faute pour l'injecter ; le contrôleur connaît seulement les observations terminées. Les durées réellement obtenues dépendent de l'ordonnancement de l'hôte.

| Profil injecté | Paramètres du scénario |
| --- | --- |
| `normal` | Délai de réponse synthétique de 8 à 11 ms |
| `latency_jump_recovery` | Délai de 180 à 183 ms entre 300 et 1000 ms, puis retour au délai normal |
| `ack_loss_after_commit` | Premier ACK supprimé après commit pour les messages 7 à 14 |
| `blackout_return` | Les requêtes reçues entre 300 et 1000 ms ne produisent ni commit ni ACK ; worker arrêté à environ 600 ms et repris à environ 700 ms dans les deux modes |

Ces valeurs sont les paramètres de faute injectés, pas des délais de livraison mesurés. Les vingt arrivées sont prévues à intervalles de 50 ms ; leurs retards réels sont conservés.

Les fichiers `report.json`, `aggregate.json`, `manifest.json`, la figure `ack-p95.svg` et les traces de chaque passage documentent les résultats. Dans ce nouveau banc, `success: true` signifie que la campagne et ses contrôles d’intégrité ont abouti ; la livraison complète est vérifiée séparément par `metrics.all_messages_delivered_and_acked` dans chaque rapport de passage. Les messages non acquittés ou épuisés restent explicitement comptés.

Dans ce banc, les délais partent du début réel de la mise en file : `delivery_p95_ms` va jusqu’à l’horodatage d’acceptation du destinataire, `ack_p95_ms` jusqu’à la fin de la tentative dont l’ACK est vérifié. Le p95 est le rang supérieur correspondant à 95 % des observations dans chaque passage ; l’agrégat publie ensuite la moyenne de ces p95, ainsi que leur minimum, leur maximum et leurs valeurs individuelles. Ce n’est pas un p95 calculé sur toutes les répétitions fusionnées.

Lire séparément les délais jusqu’à effet, les délais jusqu’à ACK, les nombres de livraisons et les tentatives. Une baisse de latence sur un profil ne constitue pas un avantage universel ; publier aussi les profils sans gain ou avec régression.

### Correction documentée après la première comparaison

La [première campagne adaptative](measured-adaptive-2026-10-27/initial/) utilisait `bounded-rto/v1`. Sur le profil ACK perdu après commit, la moyenne des p95 de confirmation atteignait **1048,8 ms en adaptatif contre 150,9 ms en fixe**, avec 28 tentatives dans chaque mode. Un doublement global du délai après chaque connexion fermée sans ACK ralentissait la confirmation des messages suivants.

La version `bounded-rto/v2` distingue cette observation ambiguë des timeouts et des refus : elle ne double plus le délai sur ce seul signal. Les preuves de la première campagne, avec un instantané des sources, sont conservées séparément de la [campagne finale](measured-adaptive-2026-10-27/). Les douze files adaptatives initiales ont aussi été relues sans écriture : état et trace restent identiques aux rapports originaux. Cette correction et les résultats ultérieurs ne constituent pas une garantie d’amélioration sur tous les profils.

### Résultats finaux mesurés

La [campagne finale](measured-adaptive-2026-10-27/report.json) a exécuté 24 passages en 32,101 secondes avec les sources inchangées pendant les mesures. Les cinq contrôles de campagne sont vrais. Les 24 passages ont chacun 20 effets uniques et 20 ACK vérifiés, sans effet dupliqué ni conflit de contenu. Les deux modes partagent la politique initiale enregistrée lors de la préparation SAWM historique ; aucun appel Cloud n’est effectué pendant cette campagne.

Le tableau reprend les moyennes des p95 par passage, sur trois répétitions. Les délais jusqu’à effet et jusqu’à ACK restent distincts :

| Profil | Effet p95 fixe | Effet p95 adaptatif | ACK p95 fixe | ACK p95 adaptatif | Tentatives fixe / adaptatif |
| --- | ---: | ---: | ---: | ---: | ---: |
| Normal | 33,99 ms | 33,23 ms | 34,90 ms | 34,12 ms | 20 / 20 |
| Hausse de latence | 586,99 ms | 575,56 ms | 588,35 ms | 609,36 ms | 20 / 22 |
| ACK perdu après commit | 35,32 ms | 39,70 ms | 149,49 ms | 153,89 ms | 28 / 28 |
| Coupure et reprise | 972,87 ms | 1111,21 ms | 974,77 ms | 1113,07 ms | 22 / 25 |

Le mode adaptatif utilise 285 tentatives contre 270 pour le mode fixe, pour 240 messages par mode. Sur le profil de hausse de latence, il ajoute deux timeouts par passage ; sur le profil de coupure, il en observe quatre contre un en fixe. La recalibration est effective, mais les confirmations sont plus lentes en moyenne sur trois profils. La livraison complète des deux modes ne démontre donc pas de gain général du mode adaptatif.

Les valeurs individuelles et les écarts appariés sont dans [aggregate.json](measured-adaptive-2026-10-27/aggregate.json). Par exemple, le p95 ACK lors de la coupure varie de 939,65 à 1024,48 ms en fixe, et de 1108,20 à 1117,26 ms en adaptatif. Avec trois répétitions et un seul hôte, il faut conserver cette dispersion et limiter toute conclusion aux conditions mesurées. Les résultats de la première version et ceux de la version finale ne doivent pas être fusionnés.

Voir aussi le [graphique des délais](measured-adaptive-2026-10-27/ack-p95.svg), les [traces de recalibration illustrées](measured-adaptive-2026-10-27/figures/calibration-examples.svg) et le [résumé des mesures](measured-adaptive-2026-10-27/README.md).

## Fichiers produits

| Fichier de sortie | Contenu |
| --- | --- |
| `queue.sqlite` | Messages, états, tentatives durables, métadonnées et exécutions du worker ; état et trace de calibration lorsque le mode adaptatif est activé |
| `queue.worker.sqlite` | Verrou du worker |
| `witness.sqlite` | Registre des effets et observations du destinataire |
| `messages.json` | Messages synthétiques source |
| `volatile-attempts.json` | Tentatives du canal volatil |
| `worker-1.log`, `worker-2.log` | Sorties des deux processus worker |
| `policy.json` | Politique utilisée |
| `report.json` | Scénario, contrôles, compteurs, délais et empreintes |
| `measurements.json` | Mesures structurées de l'exécution locale |

Les fichiers auxiliaires SQLite éventuels font partie de l'état local tant que les connexions ne sont pas fermées. `measurements.json` et son champ `cloud_sent: false` décrivent la production locale ; ils n'interdisent pas un dépôt séparé de résumé ensuite. Une empreinte vérifie la correspondance avec une valeur connue, sans fournir à elle seule une attestation externe inviolable.

## Parcours Cloud

Ces appels sont optionnels pour exécuter le banc. Ils déposent une calibration synthétique, vérifient son rappel TSST, enregistrent un classement ODVL, demandent une politique à SAWM puis, après un nouvel essai local, déposent le bilan et demandent une lecture des résultats. Ces commandes Cloud ne modifient pas la boucle de livraison en cours. La calibration continue optionnelle décrite plus haut est réalisée par le contrôleur local, à partir des seules observations terminées et dans les limites de la politique initiale.

### Accès et configuration

Installer la dépendance dans l'environnement virtuel :

```sh
python -m pip install -r requirements.txt
```

Prévoir un espace Basira et une entité dédiée aux essais, accessible au principal de la clé avec les droits effectifs de lecture et d'écriture. Le parcours complet utilise les scopes suivants :

| Scope | Usage par le programme |
| --- | --- |
| `events:write` | Déposer les petits résumés |
| `jobs:read` | Attendre et vérifier la fin des jobs de dépôt |
| `tsst:recall` | Rappeler les résumés déposés |
| `odvl:rank` | Classer les options lors de `prepare` |
| `sawm:run` | Demander la politique puis la lecture du bilan |

Les scopes ne remplacent pas les droits sur l'entité. Le programme ne crée ni espace, ni clé, ni entité et n'a pas besoin de purge ou d'administration pour ce parcours.

Dans un fichier hors du dépôt, par exemple `~/.config/sawm/anti-brouillage.env`, renseigner les valeurs de votre accès existant :

```dotenv
BASIRA_SPACE=mem_VOTRE_ESPACE
BASIRA_KEY=VOTRE_CLE_EXISTANTE
BASIRA_BASE=https://basira-api.sawm.io/api/v1/basira
```

Ce bloc est un modèle à compléter, pas un accès utilisable. La CLI lit le fichier directement : ne pas le sourcer. La base API est fixe ; toute autre URL est refusée. Le fichier doit être régulier, non symbolique et inaccessible au groupe et aux autres utilisateurs :

```sh
chmod 600 "$HOME/.config/sawm/anti-brouillage.env"
```

Définir `BASIRA_ENTITY` avec l'identifiant non secret de l'entité autorisée : `ent_` suivi de 32 caractères hexadécimaux minuscules. On peut le saisir sans l'inscrire dans les exemples :

```sh
read -r BASIRA_ENTITY
```

### 1. Mesurer une calibration locale

Utiliser un nouveau répertoire, puis produire un résumé à partir des mesures réelles :

```sh
python lab.py --output-dir output/calibration --messages 20 --policy examples/policy.json
python - output/calibration/report.json output/calibration-summary.json <<'PY'
import json
import sys
from pathlib import Path
report = json.loads(Path(sys.argv[1]).read_text())
summary = {
    "kind": "delivery_calibration",
    "success": report["success"],
    "messages": report["planned_messages_per_channel"],
    "outage_seconds": round(report["outage_measured_seconds"], 3),
    "sender_restarted": report["checks"]["sender_process_restarted"],
    "durable_unique": report["comparison"]["durable"]["delivered_unique"],
    "volatile_unique": report["comparison"]["volatile"]["delivered_unique"],
    "duplicate_effects": report["comparison"]["durable"]["duplicate_effects"],
    "failed_checks": [key for key, passed in report["checks"].items() if not passed],
}
Path(sys.argv[2]).write_text(json.dumps(summary, indent=2) + "\n")
PY
```

Le résumé est une sélection de faits de calibration, distincts des résultats du prochain essai. Si le banc ne produit pas les champs attendus, inspecter son rapport avant de préparer un résumé fidèle ; ne pas remplacer une mesure manquante par un succès supposé.

Le fichier de résumé doit faire au plus 4096 octets et son enveloppe canonique complète au plus 1300 caractères. Garder le résumé court, y compris lorsque des contrôles échouent. N'y inclure ni clé, ni donnée privée, ni base SQLite, ni rapport volumineux.

### 2. Préparer la politique avec le Cloud

```sh
python cloud_control.py prepare --kind delivery \
  --credentials "$HOME/.config/sawm/anti-brouillage.env" \
  --entity "$BASIRA_ENTITY" \
  --summary output/calibration-summary.json \
  --output output/cloud-prepare --run
```

`--run` autorise de vrais appels API, potentiellement facturés. Son absence provoque un refus ; elle ne lance pas une simulation. Le répertoire Cloud doit être nouveau.

Le programme vérifie le reçu de dépôt, le job terminé dans la bonne entité, puis la présence littérale du résumé canonique dans le rappel TSST. Les éléments rappelés sans rapport avec ce résumé ne sont pas recopiés dans le prompt.

ODVL reçoit les options `bounded_retry`, `volatile_once`, `manual_resume`, avec les scores locaux respectifs `0.8`, `0.3`, `0.4`. La réponse doit être une permutation exacte avec les bons identifiants, scores, rangs, espace et entité. Ce classement reste dans le rapport : il ne détermine pas les tentatives et n'est pas injecté dans le prompt SAWM.

SAWM reçoit le résumé rappelé et le contrat de politique. Sa réponse doit être un JSON respectant exactement les champs et bornes du validateur local. Aucun code ou commande produit par SAWM n'est exécuté.

En cas de succès, `output/cloud-prepare/policy.json` contient la politique validée et `cloud-report.json` conserve les reçus, preuves et réponses. Avec la version actuelle du contrôleur, vérifier `completed: true`, `policy_validated: true` et les opérations réussies. Un fichier présent ou un appel commencé ne prouve pas le succès.

### 3. Exécuter avec la politique validée

```sh
python lab.py --output-dir output/with-cloud-policy --messages 20 \
  --policy output/cloud-prepare/policy.json
```

Le banc réalise toutes les livraisons en local. Pour une comparaison équitable, conserver le même nombre de messages, les mêmes valeurs synthétiques, la même politique et le même scénario de fautes. Les identifiants et horodatages sont propres à chaque passage ; la charge de l'hôte peut faire varier les durées. Si les politiques diffèrent, l'écart observé n'isole pas l'effet de la mémoire Cloud.

### 4. Enregistrer les résultats réellement obtenus

```sh
python - output/with-cloud-policy/report.json output/results-summary.json <<'PY'
import json
import sys
from pathlib import Path
report = json.loads(Path(sys.argv[1]).read_text())
summary = {
    "kind": "delivery_measured_results",
    "success": report["success"],
    "messages": report["planned_messages_per_channel"],
    "outage_seconds": round(report["outage_measured_seconds"], 3),
    "sender_restarted": report["checks"]["sender_process_restarted"],
    "durable_unique": report["comparison"]["durable"]["delivered_unique"],
    "volatile_unique": report["comparison"]["volatile"]["delivered_unique"],
    "duplicate_effects": report["comparison"]["durable"]["duplicate_effects"],
    "failed_checks": [key for key, passed in report["checks"].items() if not passed],
}
Path(sys.argv[2]).write_text(json.dumps(summary, indent=2) + "\n")
PY
python cloud_control.py record --kind delivery \
  --credentials "$HOME/.config/sawm/anti-brouillage.env" \
  --entity "$BASIRA_ENTITY" \
  --summary output/results-summary.json \
  --output output/cloud-record --run
```

Cette commande dépose le bilan, vérifie son rappel TSST et demande une lecture à SAWM. Elle intervient après les mesures et ne change pas les livraisons déjà réalisées. En cas de résultat incomplet, publier les échecs et les limites du rapport ; le résumé ne remplace pas les preuves locales.

## Preuves conservées

Les jeux de preuves sont distincts ; les passages avec et sans Cloud ne doivent pas être fusionnés.

| Jeu de preuves | Résultat et portée |
| --- | --- |
| [Essai local initial](lab-report.json), `8f8b860e672f4f8691121db727e15342` | 10 messages par canal ; 10 effets durables, 5 volatils ; 10 ACK durables, 4 volatils ; coupure de 2,008543 s ; durée de 2,779408 s ; aucun dépôt Cloud dans cet essai |
| [Calibration du 27 octobre](measured-2026-10-27/calibration.json), `d12b381e78c94dee8c297f136c9c8a2d` | 20 messages ; 20 effets durables, 5 volatils ; coupure de 2,008468 s ; durée de 2,881150 s ; exécution locale sans Cloud |
| [Préparation Cloud](measured-2026-10-27/cloud/cloud-report.json) | `real_cloud: true`, cinq opérations réussies, dépôt terminé, rappel TSST littéral, classement ODVL et proposition SAWM validée |
| [Essai avec politique préparée](measured-2026-10-27/benchmark.json), `b3adc1750d904e5e9dd9964b9ea5a31b` | 20 effets durables, 5 volatils ; 20 ACK durables, 4 volatils ; coupure de 2,008630 s ; durée de 2,850368 s |
| [Bilan Cloud](measured-2026-10-27/results-cloud/cloud-report.json) | Quatre opérations réussies, dépôt puis rappel littéral TSST du bilan et lecture SAWM ; `completed: true` |
| [Relecture par le validateur final](measured-2026-10-27/final-validator-check.json) | `validated: true`, `real_api_rerun: false` ; contrôle local des preuves archivées, pas un nouvel appel API |

La préparation Cloud archivée utilise un format historique sans champ global `completed`. Sa réussite s'apprécie à partir des cinq opérations terminées, du job, de la preuve de rappel et de `policy_validated: true`. Le contrôleur actuel ajoute le champ `completed` ; cette différence de format ne doit pas être masquée en modifiant les preuves historiques.

Les deux essais du 27 octobre utilisent la même politique et présentent 14 contrôles vrais, une requête durable reçue en double et zéro effet dupliqué. Le résultat 20/20 était déjà obtenu sans Cloud. La campagne démontre le raccordement effectif à Basira, TSST, ODVL et SAWM ; elle ne démontre pas un gain de livraison causé par ce raccordement.

Le rapport PDF existant dans le dossier de campagne et les JSON sont conservés comme artefacts historiques. La lecture SAWM est une interprétation des résumés fournis par le laboratoire, pas une mesure indépendante du destinataire.

## Validation et limites

Exécuter les tests après installation de `requirements.txt` :

```sh
python -m unittest discover -s tests -v
```

La vérification du 27 octobre 2026 après l’ajout de la calibration continue et du banc comparatif a passé **41 tests sur 41**. La suite initiale, avant cette extension, comportait 20 tests. Les tests du contrôleur Cloud utilisent des réponses locales simulées ; ils ne relancent pas les API réelles. Les tests couvrent notamment les ACK invalides, les conflits d'identité/contenu, les budgets de tentatives, l'interruption après commit avant réception de l'ACK et les validations des reçus, rappels et classements Cloud. Les tests adaptatifs ajoutent la causalité des mises à jour, les timeouts censurés, les limites immuables, l’atomicité SQLite et la reprise de l’état. La [grille de validation](validation.md) explicite les preuves attendues.

La reprise concerne un processus arrêté puis relancé sur le même hôte resté démarré, avec SQLite conservé. Les échéances monotones ne constituent pas un protocole de reprise après redémarrage de la machine. Le banc ne certifie ni une coupure électrique, ni une corruption du disque, ni la résistance à un administrateur malveillant. Il ne mesure aucune liaison radio, bande passante physique ou performance opérateur. Le [protocole de validation radio futur](RADIO-VALIDATION.md) distingue le Mac/VM disponible d’une future liaison isolée et instrumentée entre deux radios. Le résultat doit être reproduit et étendu au protocole du destinataire avant toute adaptation à un service réel.

Pour le programme complémentaire consacré à l'ordonnancement HTTP et aux échéances : [SAWM Smart Réseau](https://github.com/ulookme/sawm-smart-reseau).
