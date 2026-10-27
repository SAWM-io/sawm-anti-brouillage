# SAWM Anti-Brouillage

**Résilience applicative aux coupures — conserver les messages, reprendre leur envoi et vérifier chaque effet dans un démonstrateur HTTP.**

[English](README.en.md) · [Démarrage](#démarrer-en-local) · [Guide technique](docs/GUIDE-TECHNIQUE.md) · [Preuves mesurées](docs/measured-2026-10-27/) · [Validation radio envisagée](docs/RADIO-VALIDATION.md)

SAWM Anti-Brouillage est un démonstrateur exécutable de livraison HTTP résiliente : une file SQLite conserve les messages, un worker reprend les tentatives après son redémarrage et un destinataire idempotent évite de répéter l'effet d'un message. Il donne aux équipes produit et intégration un cas concret pour évaluer la continuité d'un échange applicatif avant d'adapter ce mécanisme à leur service.

Le parcours Cloud ajoute une mémoire des essais avec **Basira**, un rappel vérifié avec **TSST**, un classement consultatif avec **ODVL** et une proposition de politique bornée par **SAWM**. Pendant l'essai, un mode optionnel de **calibration continue locale** ajuste le timeout HTTP et le délai entre reprises à partir des seules tentatives terminées. La livraison reste locale et autonome ; SAWM prépare la politique initiale avant l'exécution.

## Ce que vous pouvez évaluer

| Besoin | Mécanisme démontré |
| --- | --- |
| Conserver les messages lorsque le service est indisponible | File SQLite persistante et nouvelles tentatives bornées |
| Reprendre après l'arrêt du processus émetteur | Récupération de la même file et conservation du budget de tentatives |
| Traiter une réponse perdue sans doubler l'effet | Identifiant stable, digest du contenu, transaction du destinataire et ACK vérifié |
| Ajuster la reprise aux observations HTTP | Calibration locale après chaque tentative terminée, avec état et limites persistés |
| Expliquer les résultats | Registre destinataire, traces de tentatives, rapport JSON et contrôles explicites |
| Retrouver une calibration et un bilan | Dépôt Basira, rappel littéral TSST et lecture SAWM avant/après l'essai |

## Un résultat mesuré, dans un périmètre précis

La [campagne initiale du 27 octobre 2026](docs/measured-2026-10-27/benchmark.json) compare les mêmes 20 messages sur deux canaux du même serveur HTTP local. Elle provoque une coupure de **2,009 s**, un arrêt puis une reprise du worker et la perte volontaire d'un ACK après commit dans chaque canal.

| Observation à la clôture | File durable | Envoi volatil, une tentative |
| --- | ---: | ---: |
| Effets uniques enregistrés | **20 / 20** | **5 / 20** |
| ACK reçus et vérifiés | 20 | 4 |
| Messages sans effet | 0 | 15 |
| Requêtes reçues en double | 1 | 0 |
| Effets applicatifs dupliqués | **0** | **0** |

Les 14 contrôles du rapport sont vrais. Une requête répétée n'a donc pas créé un second effet. L'absence d'ACK ne signifie pas nécessairement l'absence d'effet : c'est précisément ce que teste la réponse volontairement perdue.

**La calibration locale sans Cloud obtenait aussi 20/20, avec exactement la même politique.** Ces mesures montrent le bénéfice de la file, des reprises et du destinataire idempotent face aux fautes du banc ; elles ne démontrent aucun gain de livraison causé par le Cloud. Les deux exécutions et leurs limites sont [documentées séparément](docs/GUIDE-TECHNIQUE.md#preuves-conservées).

## Démarrer en local

Prérequis : **Python 3.10+**, un environnement permettant d'ouvrir un port sur `127.0.0.1` et de lancer des processus enfants. Le banc local utilise uniquement la bibliothèque standard Python.

```sh
git clone https://github.com/ulookme/sawm-anti-brouillage.git
cd sawm-anti-brouillage
python3 -m venv .venv
. .venv/bin/activate
python lab.py --output-dir output/demo --messages 20 --policy examples/policy.json
```

Le dépôt nécessite les droits GitHub appropriés. Utilisez un nouveau répertoire de sortie à chaque essai. Le scénario dure normalement quelques secondes, avec un budget global de 30 secondes. Il crée son propre serveur HTTP, le coupe pendant deux secondes, redémarre le worker puis restaure le serveur.

Consultez `output/demo/report.json` pour les résultats et `output/demo/measurements.json` pour les mesures structurées. Les bases SQLite et les journaux restent disponibles pour l'analyse. Sans `--messages`, le banc utilise 10 messages ; il accepte de 3 à 20 messages par canal.

```sh
# Tests locaux, y compris les validateurs Cloud ; aucun appel API réel.
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## Calibration continue optionnelle

Le mode adaptatif ajuste ses paramètres après chaque tentative terminée. Un ACK vérifié met à jour l'estimation de latence et sa variation ; un timeout fait augmenter le délai d'attente sans être assimilé à une mesure RTT complète. Les timeouts et refus espacent les reprises ; une connexion fermée sans ACK conserve une attente bornée, sans supposer une congestion. Les succès rapprochent les reprises. L'état et les changements sont enregistrés dans SQLite et vérifiés lors de la reprise du worker.

Les plafonds de la politique initiale, le nombre maximal de tentatives et le budget global restent fixes. La calibration locale n'utilise ni les prochaines fautes du scénario, ni une estimation de conditions radio. Le mode statique reste le comportement par défaut de `lab.py`.

Pour comparer les deux modes sur les mêmes charges synthétiques :

```sh
python adaptive_lab.py --output-dir output/adaptive-demo --repetitions 3 --messages 20
```

La [méthode et les limites de la comparaison](docs/GUIDE-TECHNIQUE.md#calibration-continue-locale) distinguent les observations utilisées par le contrôleur des hypothèses de politique préparées avant l'essai. Un meilleur résultat n'est pas supposé : le rapport conserve aussi les régressions et les coûts en tentatives.

### Résultat de la comparaison finale

La [campagne finale du 27 octobre](docs/measured-adaptive-2026-10-27/) comprend 24 passages : quatre profils, deux modes, trois répétitions. **Chaque passage livre et acquitte 20/20 messages, sans effet dupliqué.** Les valeurs ci-dessous sont les moyennes des p95 de délai jusqu'à ACK, calculés séparément dans les trois passages de chaque mode.

| Profil HTTP | Fixe : p95 ACK moyen | Adaptatif : p95 ACK moyen | Tentatives par passage, fixe / adaptatif |
| --- | ---: | ---: | ---: |
| Service normal | 34,90 ms | 34,12 ms | 20 / 20 |
| Hausse de latence puis retour | 588,35 ms | 609,36 ms | 20 / 22 |
| ACK perdu après commit | 149,49 ms | 153,89 ms | 28 / 28 |
| Coupure puis reprise du worker | 974,77 ms | 1113,07 ms | 22 / 25 |

**La calibration fonctionne, mais cette campagne ne montre pas de gain général de performance.** Le mode adaptatif demande 285 tentatives contre 270 en fixe pour 240 messages par mode ; le p95 ACK moyen est inférieur en mode fixe sur trois des quatre profils mesurés. Trois répétitions ne permettent pas d'établir une supériorité générale. Le mode statique reste le défaut, et le mode adaptatif permet d'étudier ces compromis avec des traces complètes.

Les [mesures individuelles](docs/measured-adaptive-2026-10-27/aggregate.json), le [graphique des délais](docs/measured-adaptive-2026-10-27/ack-p95.svg) et les [exemples de recalibration](docs/measured-adaptive-2026-10-27/figures/calibration-examples.svg) sont conservés. La première version et sa régression sur les ACK perdus restent [documentées dans le guide](docs/GUIDE-TECHNIQUE.md#correction-documentée-après-la-première-comparaison).

## Architecture et rôle du Cloud

```mermaid
flowchart TB
    subgraph Before[Avant le banc — Cloud optionnel]
        C[Résumé de calibration] --> B[Basira : dépôt et job terminé]
        B --> T[TSST : rappel littéral vérifié]
        T --> S[SAWM : proposition de politique]
        S --> V[Validation locale du JSON et des bornes]
        O[ODVL : classement des options] --> R[Rapport de préparation]
    end
    subgraph Local[Essai local — HTTP sur 127.0.0.1]
        M[Messages synthétiques] --> Q[File SQLite]
        Q --> W[Worker et reprises bornées]
        W --> H[Témoin HTTP et registre idempotent]
        H --> A[ACK corrélé et vérifié]
        A --> Q
        W --> F[Observations de tentatives terminées]
        F --> L[Calibration locale optionnelle]
        L --> W
        M --> E[Émetteur volatil]
        E --> H
        H --> J[Rapport de mesures]
    end
    V --> Q
    subgraph After[Après le banc — Cloud optionnel]
        J --> U[Résumé des résultats]
        U --> BT[Basira et rappel TSST]
        BT --> SR[SAWM : lecture du bilan]
    end
```

- **Basira** conserve les petits résumés fournis par le laboratoire ; les reçus et la fin des jobs sont contrôlés.
- **TSST** rappelle ces résumés. Le validateur exige le texte canonique attendu dans une preuve marquée intègre, avec le bon espace et la bonne entité.
- **ODVL** classe trois options de livraison avant l'essai. L'ordre est validé et conservé à titre consultatif ; le code ne l'applique pas au worker et ne le transmet pas au prompt de politique SAWM.
- **SAWM** propose avant l'essai un JSON de politique initiale et produit ensuite une lecture des mesures. Le code accepte uniquement les champs et bornes prévus ; il n'exécute aucune commande générée.

Aucun de ces appels ne se trouve dans la boucle de livraison. La campagne archivée contient de vrais appels Cloud ; le champ local `cloud_access: false` décrit uniquement l'exécution de `lab.py`.

## Utiliser Basira et SAWM

Le [parcours Cloud complet](docs/GUIDE-TECHNIQUE.md#parcours-cloud) fournit les commandes de calibration, préparation, essai et enregistrement du bilan. Il nécessite `httpx` via `requirements.txt`, un espace Basira, une entité explicitement autorisée et une clé existante avec les scopes **`events:write`, `jobs:read`, `tsst:recall`, `odvl:rank`, `sawm:run`** pour le parcours complet. Les droits effectifs sur l'entité restent nécessaires.

La clé se lit depuis un fichier privé hors du dépôt, de permissions `600`. L'URL API acceptée est `https://basira-api.sawm.io/api/v1/basira`. Les commandes Cloud exigent `--run` et peuvent être facturées. Envoyez uniquement les petites mesures synthétiques prévues dans le guide.

## Périmètre et suite du parcours

Le programme est un banc de démonstration sur un seul hôte. Il teste un redémarrage de processus avec conservation de SQLite pendant le même démarrage de l'hôte. Il ne valide ni un redémarrage de machine, ni une panne électrique, ni la corruption de stockage, ni l'idempotence d'une API tierce. Les délais incluent l'attente des messages conservés ; ils ne mesurent pas un gain de débit ou de vitesse réseau. Le Mac et les VM permettent la validation logicielle ; aucun équipement radio, véhicule ou réseau physique n'est testé. Le [protocole radio futur](docs/RADIO-VALIDATION.md) décrit une liaison isolée entre deux radios, une atténuation contrôlée et les mesures nécessaires, sans brouilleur déployé.

Pour approfondir : [guide technique](docs/GUIDE-TECHNIQUE.md), [grille de validation](docs/validation.md) et [preuves du 27 octobre](docs/measured-2026-10-27/). Pour explorer l'ordonnancement de messages HTTP et les échéances, voir le programme complémentaire [SAWM Smart Réseau](https://github.com/ulookme/sawm-smart-reseau).
