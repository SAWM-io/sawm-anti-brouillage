# Résilience applicative sur HTTP local

Ce programme compare deux façons d’envoyer de petits messages civils à un serveur témoin : un émetteur sans file persistante et un émetteur doté d’une file SQLite, de nouvelles tentatives bornées et d’une validation des accusés de réception. Il utilise de vraies connexions HTTP sur `127.0.0.1`, dans un banc isolé. Il ne mesure ni la qualité d’un réseau physique ni son débit maximal.

Le serveur, les messages et les processus du banc appartiennent à cette exécution. Le banc local ne modifie aucune configuration réseau système et n’accède à aucun Cloud, véhicule ou équipement radio. L’étape Cloud optionnelle décrite plus bas se lance séparément, avant ou après les essais locaux.

## Lancer le banc

Depuis ce répertoire, avec Python 3.10 ou une version ultérieure :

```sh
python3 lab.py --output-dir output/demo
```

Le répertoire de sortie doit être nouveau afin de préserver les preuves des essais précédents. Le scénario par défaut utilise dix messages dans chaque groupe : deux avant la coupure, cinq pendant la coupure et trois après le retour du serveur. Le nombre de messages peut être adapté, de trois à vingt :

```sh
python3 lab.py --output-dir output/autre-essai --messages 10 --policy examples/policy.json
```

Le banc applique un budget global de trente secondes. La coupure dure deux secondes et consiste à arrêter son propre serveur HTTP, puis à le redémarrer sur le même port local. Ce budget logiciel n’est pas une garantie de temps réel fournie par le système d’exploitation.

## Ce que le scénario vérifie

Les deux groupes utilisent le même serveur, le même port, les mêmes identifiants et contenus de messages ainsi que la même coupure. Ils disposent de canaux distincts dans le registre du témoin : l’unicité est vérifiée par le couple `(canal, identifiant du message)`.

Pour le premier message de chaque groupe, le témoin valide l’effet en SQLite puis perd volontairement son premier accusé de réception. Cette faute permet de distinguer « réponse non reçue » et « message non appliqué ». Le worker de la file durable est également arrêté puis relancé pendant la coupure, avant le retour du serveur.

Le destinataire ne confirme l’effet qu’après validation de sa transaction. Une nouvelle tentative portant le même identifiant et le même contenu doit retrouver cet effet sans en créer un second. Un identifiant réutilisé avec un contenu différent doit être refusé. L’émetteur durable vérifie le schéma de l’ACK, l’identifiant, le digest du contenu et les champs de confirmation avant de considérer le message comme acquitté.

Ces mécanismes visent un effet applicatif unique dans ce témoin contrôlé. Ils ne garantissent pas que toute requête HTTP sera exécutée une seule fois sur un service arbitraire.

## Lire les résultats sans confondre les compteurs

- **Effet unique confirmé** : message enregistré par le témoin après commit SQLite.
- **ACK inconnu ou non reçu** : absence de confirmation reçue par l’émetteur ; l’effet peut pourtant avoir été enregistré.
- **Message sans effet à la clôture** : aucun effet unique correspondant dans le registre final du témoin. Si un compteur est nommé `lost`, il décrit cette observation à la fin du banc, pas la preuve d’une perte irréversible.
- **Message en attente dans la file** : message conservé localement et non acquitté ; il ne doit pas être assimilé automatiquement à un message perdu.
- **Doublon reçu** : tentative supplémentaire reçue pour un identifiant déjà traité. Ce compteur ne signifie pas qu’un effet supplémentaire a été appliqué.

Les tentatives, les ACK et les effets sont donc des faits différents. Le rapport doit les conserver séparément, y compris lorsqu’un essai se termine avec un budget épuisé ou une récupération incomplète.

`end_to_end_ms` mesure, avec la même horloge monotone de l’hôte, le temps entre la création du message et son horodatage d’acceptation dans la première transaction ensuite committée par le destinataire. Ce n’est ni l’heure exacte de fin du commit, ni le délai jusqu’à réception de l’ACK. Dans l’essai conservé, la moyenne volatile ne concerne que cinq messages arrivés ; la moyenne durable inclut les dix messages, dont ceux ayant attendu la fin de la coupure. Comparer ces moyennes comme un gain de vitesse réseau serait incorrect : la file accepte davantage de délai pour livrer davantage de messages, sans optimiser le débit réseau.

## Politique de nouvelles tentatives

La politique est un document JSON fermé, validé avant le démarrage du worker. La configuration prévue est :

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
| `max_attempts` | 1 à 20 |
| `base_delay_ms` | 25 à 1000 ms |
| `max_delay_ms` | Au moins `base_delay_ms`, au plus 2000 ms |
| `request_timeout_ms` | 50 à 2000 ms |
| `total_budget_ms` | 1000 à 25000 ms |

La file n’autorise ni destination libre ni commande à exécuter. Le worker lit uniquement la cible locale provisionnée par le banc et utilise le chemin `/deliver/durable` :

```sh
python3 delivery.py --queue output/demo/queue.sqlite
```

Cette commande est destinée au scénario où le témoin correspondant est encore actif. Relancer le worker après la fermeture du banc ne recrée pas ce serveur et ne prolonge pas son budget.

## Fichiers conservés

Le répertoire de sortie contient la file `queue.sqlite`, son fichier de verrouillage `queue.worker.sqlite`, le registre du témoin `witness.sqlite`, les messages `messages.json`, les tentatives volatiles `volatile-attempts.json`, les journaux `worker-1.log` et `worker-2.log`, la politique `policy.json`, ainsi que `report.json` et `measurements.json`. Les tentatives durables sont enregistrées dans la file SQLite. Les éventuels fichiers auxiliaires SQLite font partie de l’état local tant que les connexions ne sont pas fermées.

`measurements.json` prépare des mesures structurées pour une exploitation ultérieure. Sa présence ne signifie pas qu’un dépôt Cloud a eu lieu. Un futur dépôt pourrait fournir une mémoire de ces essais et faciliter leur audit. À politique de livraison identique, ce dépôt ne modifie ni les tentatives, ni les ACK, ni les effets produits ; il n’améliore pas à lui seul la livraison. Aucune preuve Cloud n’est présente dans cet essai. Les journaux et les empreintes locales ne constituent pas, à eux seuls, une attestation externe inviolable.

La [grille de validation](docs/validation.md) décrit les preuves attendues et les limites. Les résultats effectifs doivent être lus dans le rapport de l’exécution concernée ; ce guide ne suppose pas leur réussite.

## Préparer puis auditer un essai avec le Cloud, sur demande explicite

`cloud_control.py` permet de déposer une petite calibration synthétique, de la rappeler avec TSST, de faire classer des options par ODVL, puis de demander à SAWM une politique de nouvelles tentatives. Le JSON proposé doit respecter le schéma et les bornes locales avant d’être enregistré dans `policy.json`. Aucun code ou commande produit par SAWM n’est exécuté. Ces appels précèdent le banc ; le Cloud n’intervient pas dans la boucle de livraison des messages.

Cette étape nécessite les dépendances de `requirements.txt` :

```sh
python3 -m pip install -r requirements.txt
```

Préparer, hors du dépôt, un fichier privé contenant `BASIRA_SPACE=mem_…` et `BASIRA_KEY=…`, puis lui donner les permissions `600`. Ne pas placer la clé dans une commande, un résumé ou un journal. Le fichier peut aussi contenir `BASIRA_BASE=https://basira-api.sawm.io/api/v1/basira` ; cette URL publique est fixe et toute autre valeur est refusée. La CLI lit directement le fichier, sans avoir besoin de le sourcer dans le shell.

```sh
chmod 600 /chemin/credentials-privees.env
```

`BASIRA_ENTITY` désigne ici une variable shell contenant l’identifiant non secret de l’entité cible, au format `ent_` suivi de 32 caractères hexadécimaux minuscules. Cette entité doit appartenir à l’espace concerné et être autorisée par les droits effectifs de la clé ; connaître son identifiant ne donne aucun droit d’accès.

Préparer `calibration.json` avec quelques données synthétiques de calibration, distinctes des résultats du prochain essai. Le fichier JSON doit faire moins de 4096 octets. Le code impose aussi une enveloppe canonique complète de 1300 caractères au maximum pour le rappel littéral : garder le résumé très court. N’envoyer ni clés, ni données privées, ni bases SQLite, ni rapports volumineux.

```sh
python3 cloud_control.py prepare --kind delivery \
  --credentials /chemin/credentials-privees.env \
  --entity "$BASIRA_ENTITY" \
  --summary calibration.json \
  --output output/cloud-prepare --run
```

`--run` déclenche de vrais appels API, potentiellement facturés. Son absence provoque un refus ; elle ne lance pas une simulation. Le répertoire de sortie doit être nouveau. La préparation conserve les reçus et réponses dans `cloud-report.json` et écrit une politique validée si les étapes aboutissent. Lire ce rapport avant d’utiliser la politique : un appel commencé ou un fichier présent ne constitue pas, à lui seul, une preuve de succès Cloud.

Exécuter ensuite le banc local avec cette politique :

```sh
python3 lab.py --output-dir output/essai-politique-cloud \
  --policy output/cloud-prepare/policy.json
```

Après l’essai, préparer `resultats-synthetiques.json` avec un petit résumé fidèle des mesures réellement obtenues, y compris les échecs éventuels, et les mêmes limites de taille et de confidentialité. Le résumé est une sélection de faits, pas une copie complète de `report.json` ou de `measurements.json`.

```sh
python3 cloud_control.py record --kind delivery \
  --credentials /chemin/credentials-privees.env \
  --entity "$BASIRA_ENTITY" \
  --summary resultats-synthetiques.json \
  --output output/cloud-record --run
```

Cette seconde commande dépose le bilan, vérifie son rappel TSST et demande une lecture à SAWM. Elle intervient après les mesures et ne change pas les livraisons déjà réalisées. Aucune réussite de cette étape Cloud n’est revendiquée par le relevé local fourni avec ce programme.

Pour comparer l’exécution locale seule à une exécution accompagnée de mémoire et d’audit Cloud, conserver exactement la même politique, le même nombre de messages, leurs valeurs fictives et le même scénario de fautes. Les identifiants et horodatages sont propres à chaque passage ; les temps réels peuvent varier avec la charge de l’hôte. La livraison et l’idempotence restent celles de la file et du destinataire. À politique identique, le Cloud ajoute de l’historique et des éléments d’audit, sans produire d’ACK ni d’effet applicatif supplémentaire. Si la politique change entre deux essais, la comparaison ne permet pas d’attribuer l’écart observé à la seule mémoire Cloud.

## Essai mesuré du code final

Le [rapport local `8f8b860e672f4f8691121db727e15342`](docs/lab-report.json) constate une coupure de 2,009 secondes et une exécution totale de 2,779 secondes sur macOS arm64. Après l’arrêt forcé et la reprise du worker, le groupe durable obtient dix effets uniques et dix ACK vérifiés sur dix messages ; une tentative reçue en double n’ajoute aucun effet. Le groupe volatile obtient cinq effets uniques et quatre ACK vérifiés : cinq messages n’ont pas d’effet à la clôture et un effet confirmé correspond à l’ACK volontairement perdu. Les quatorze contrôles du rapport sont vrais, notamment la correspondance des digests reçus avec les contenus source. Il s’agit d’un essai local précis, sans mesure de débit réseau ni garantie pour d’autres pannes.

## Portée de la démonstration

La reprise testée concerne l’arrêt puis le redémarrage d’un processus sur le même hôte resté démarré, avec son état SQLite conservé. Les échéances monotones de cette file ne sont pas un protocole de reprise après redémarrage de l’hôte. Le banc ne certifie pas la tenue à une coupure électrique, à une corruption du disque ou à un administrateur malveillant. Une file persistante et des tentatives supplémentaires peuvent améliorer la livraison de messages face aux fautes de ce banc ; elles ne rendent pas le réseau physique plus rapide et ne réparent pas sa liaison.

## Campagne avec les vraies API du 27 octobre 2026

Le dossier [measured-2026-10-27](docs/measured-2026-10-27/) ajoute le raccordement effectivement exécuté à Basira, TSST, ODVL et SAWM, puis le dépôt et le rappel du bilan. La calibration et les résultats utilisent une entité civile dédiée, autorisée dans l'espace client depuis Chrome. Le rapport PDF commun explique le rôle de chaque composant.

L'essai à 20 messages a livré **20/20** messages durables contre **5/20** en mode volatil, après une coupure de service d'environ 2 secondes et un redémarrage du processus émetteur. Une requête a été répétée après perte d'ACK ; aucun effet applicatif n'a été doublé. SAWM a proposé la politique de reprise bornée, validée avant le lancement. Cette politique est identique à celle du banc local sans Cloud, qui obtenait aussi 20/20 : le résultat ne démontre pas un gain de livraison causé par le Cloud.
