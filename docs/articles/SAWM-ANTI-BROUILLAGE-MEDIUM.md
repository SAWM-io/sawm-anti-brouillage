![SAWM Anti-Brouillage : livraison persistante, calibration locale et essais de coupure](../images/sawm-anti-brouillage-overview.png)

# SAWM Anti-Brouillage : conserver les messages, traverser la panne, prouver la reprise

Une commande à transmettre, un mouvement de stock, un compte rendu de production : un message peut avoir une conséquence métier bien après son départ. Quand le service destinataire devient indisponible, l'équipe doit savoir ce qui a été conservé, ce qui a été appliqué et ce qui attend encore une confirmation. Une réponse perdue rend cette lecture particulièrement délicate.

**SAWM Anti-Brouillage donne un cadre exécutable pour examiner ces situations.** Le programme associe une file persistante, des tentatives de reprise et un destinataire capable de reconnaître un message déjà traité. Il fournit des traces vérifiables pour évaluer la continuité d'un échange applicatif. Son périmètre actuel est HTTP, avec des essais locaux et une émulation réseau dans une machine virtuelle Linux.

## Ce qui se passe pendant une panne

Le premier résultat est concret. Après une coupure de service de **2,009 secondes** et un redémarrage du processus émetteur, la file durable produit **20 effets uniques sur 20 messages**, contre **5 sur 20** pour l'envoi volatil à tentative unique. Une requête durable répétée après la perte d'une confirmation ne crée aucun effet supplémentaire. Le scénario, les compteurs et les contrôles figurent dans le [rapport initial](https://github.com/SAWM-io/sawm-anti-brouillage/blob/main/docs/measured-2026-10-27/benchmark.json).

Pour une entreprise, ce résultat invite à poser des questions utiles avant l'intégration : une tâche survit-elle au redémarrage de son processus ? Une opération déjà appliquée peut-elle être reconnue lors d'une nouvelle tentative ? Le support dispose-t-il d'éléments pour expliquer un message en attente ? Ce sont des critères d'évaluation pour les échanges entre services, la collecte d'événements ou les synchronisations différées. Les exemples métier restent des pistes d'adaptation ; ils ne constituent pas des déploiements validés.

Le mécanisme est simple à suivre. SQLite conserve les messages et l'historique des tentatives. Chaque message porte un identifiant stable et une empreinte de son contenu. Le destinataire enregistre l'effet avant de confirmer ; une nouvelle tentative avec le même contenu retrouve cet effet. L'émetteur vérifie la confirmation reçue avant de clôturer le message. Cette coopération est essentielle : le comportement d'une API tierce devra être vérifié séparément. Le [guide technique](https://github.com/SAWM-io/sawm-anti-brouillage/blob/main/docs/GUIDE-TECHNIQUE.md) décrit le protocole et ses limites.

## La mémoire Cloud prépare ; le contrôleur local adapte

SAWM et Basira ajoutent une préparation et une mémoire des expériences. Avant l'essai, Basira conserve un résumé de calibration ; TSST permet de vérifier son rappel. ODVL enregistre un classement consultatif des options. SAWM propose une politique initiale, acceptée uniquement si elle respecte les bornes locales. Après l'essai, un résumé des résultats peut être déposé puis relu. Ces opérations sont documentées dans les [preuves Cloud](https://github.com/SAWM-io/sawm-anti-brouillage/tree/main/docs/measured-2026-10-27/cloud).

Pendant la livraison, le mode adaptatif travaille localement. Il ajuste l'attente des réponses et l'espacement des reprises à partir des tentatives terminées, sans connaître les prochaines pannes. Le Cloud n'est pas appelé entre les messages. La calibration historique sans Cloud obtenait déjà **20/20**, avec la même politique : les mesures ne démontrent donc aucun gain de livraison causé par le Cloud. Cette référence est conservée dans le [rapport de calibration](https://github.com/SAWM-io/sawm-anti-brouillage/blob/main/docs/measured-2026-10-27/calibration.json).

## 120 messages confirmés dans la VM

L'étape suivante a fait circuler de vrais paquets HTTP dans une VM Linux. Deux espaces réseau isolés sont reliés uniquement entre eux. Le banc impose, dans chaque sens, **60 ± 20 ms de délai et 3 % de pertes**, puis **une seconde de perte totale**, avant le retour du lien. Les six passages, répartis entre politiques fixe et adaptative, livrent finalement **120 messages sur 120**, tous confirmés, sans effet dupliqué. Le [rapport VM](https://github.com/SAWM-io/sawm-anti-brouillage/blob/main/docs/measured-vm-2026-10-27/report.json) conserve les paramètres et résultats.

Le déroulement compte autant que le total final. Des confirmations continuent pendant la dégradation partielle. Dans la fenêtre de coupure stabilisée, aucun nouvel effet ni aucune confirmation ne sont observés. Après le retour, le premier accusé de réception apparaît entre **31,97 et 124,44 ms** dans les trois passages adaptatifs, soit environ **32 à 124 ms**. Ce délai concerne la première confirmation, pas le vidage complet de la file. Un accusé en frontière de coupure, lié à un message accepté auparavant, reste explicitement documenté dans le [bilan par phase](https://github.com/SAWM-io/sawm-anti-brouillage/blob/main/docs/measured-vm-2026-10-27/README.md).

L'adaptation n'apporte pas de supériorité générale dans cette campagne : elle utilise **73 tentatives contre 69** en mode fixe. Les pertes aléatoires diffèrent entre passages, et les répétitions restent limitées. Le programme permet précisément d'examiner ces compromis, avec les résultats défavorables conservés dans les [agrégats](https://github.com/SAWM-io/sawm-anti-brouillage/blob/main/docs/measured-vm-2026-10-27/aggregate.json).

## Essayer le programme

Pour essayer le banc local, il faut Python **3.10 ou ultérieur** et les droits d'accès au dépôt. Depuis un terminal :

```sh
git clone https://github.com/SAWM-io/sawm-anti-brouillage.git
cd sawm-anti-brouillage
python3 -m venv .venv
. .venv/bin/activate
python lab.py --output-dir output/demo --messages 20 --policy examples/policy.json
```

Le [démarrage documenté](https://github.com/SAWM-io/sawm-anti-brouillage#démarrer-en-local) utilise la bibliothèque standard Python. Chaque essai exige un nouveau répertoire de sortie. Pour les étapes Cloud optionnelles, installer `requirements.txt`, puis suivre le [parcours prévu](https://github.com/SAWM-io/sawm-anti-brouillage/blob/main/docs/GUIDE-TECHNIQUE.md#parcours-cloud) : clé existante dans un fichier privé hors du dépôt, entité autorisée et scopes appropriés. Aucun secret n'est nécessaire pour le banc local.

La reproduction réseau requiert une VM Linux équipée d'`iproute2`, de namespaces et de `netem`. La commande utilise root pour créer et nettoyer les ressources isolées ; elle lit la politique archivée sans nouvel appel Cloud :

```sh
sudo python3 vm_netem_lab.py --output-dir output/vm-demo --repetitions 3 \
  --policy docs/measured-2026-10-27/cloud/policy.json
```

Le nom du programme ne transforme pas cette preuve en protection radio. Aucun RSSI, SNR ou brouilleur physique n'est mesuré. La reprise d'un processus ne valide pas non plus une reprise après redémarrage de la machine. Ces frontières sont exposées dans la [portée réseau et radio](https://github.com/SAWM-io/sawm-anti-brouillage/blob/main/docs/RADIO-VALIDATION.md).

Pour une équipe qui prépare un pilote, la prochaine étape est concrète : reproduire les essais, choisir un échange métier, puis vérifier son destinataire et ses contraintes. [SAWM Anti-Brouillage](https://github.com/SAWM-io/sawm-anti-brouillage) fournit le code, les mesures et les éléments nécessaires pour commencer cette évaluation.
