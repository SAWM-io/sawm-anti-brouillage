# Grille de validation du banc local

Cette grille décrit les assertions à vérifier à partir des traces et des bases SQLite d’un essai. Les attentes de la grille sont distinctes du relevé réel présenté à la fin.

## Scénarios et preuves attendues

| Scénario | Faits à conserver | Vérification |
| --- | --- | --- |
| Livraison avant la coupure | Identifiant, digest, tentative, ACK éventuel et ligne du registre destinataire | Chaque effet confirmé correspond au contenu envoyé ; distinguer effet enregistré et ACK reçu. |
| ACK perdu après commit | Commit du premier message de chaque canal, réponse supprimée, observations de l’émetteur | Une absence d’ACK ne doit pas être comptée comme une absence d’effet. |
| Arrêt du serveur témoin | Horodatages de fermeture et de reprise, même adresse et même port, erreurs de connexion expurgées | La coupure réelle concerne uniquement le serveur créé par le banc ; les deux groupes rencontrent la même fenêtre de faute. |
| Messages émis pendant la coupure | Messages prévus, tentatives et état de la file | Les messages sans effet, les messages en attente et les erreurs de transport restent des catégories distinctes. |
| Arrêt puis reprise du worker | Identité des processus, arrêt constaté, redémarrage, même base de file | La récupération lit l’état persisté ; aucune réinjection silencieuse des messages manquants depuis une autre liste ne remplace cette reprise. |
| Retour du serveur | Nouvelles tentatives, ACK validés et registre final du témoin | Les messages acquittés ont un effet unique confirmé ; les éventuels messages restants sont signalés. |
| Nouvelle tentative après ACK perdu | Même canal, identifiant et digest ; tentative reçue en plus | Le registre conserve un seul effet pour le message, même si plusieurs tentatives ont été reçues. |
| Identifiant réutilisé avec un autre contenu | Réponse de refus et registre avant/après | Aucun remplacement du contenu ni nouvel effet ne doit résulter du conflit. |
| ACK invalide ou non corrélé | Réponse incorrecte contrôlée par un test local | L’émetteur ne marque pas le message comme acquitté. |
| Politique invalide | Paramètre fautif, refus avant démarrage du worker | Les champs inconnus, types invalides et dépassements de bornes sont refusés. |
| Limite de tentatives ou de temps | Compteurs, échéance et état terminal | Le worker s’arrête selon sa politique ; aucun succès n’est déduit d’un simple épuisement du budget. |
| Nettoyage | Arrêt constaté des processus et fermeture du témoin | Seuls les processus et ressources créés par l’essai sont arrêtés ; les preuves restent disponibles. |

Les tests unitaires peuvent contrôler les refus, les ACK et les transactions sans produire de preuve de coupure réseau réelle. Le banc en loopback complète ces tests avec de vraies connexions locales et des arrêts de processus. Les deux types de preuve doivent être identifiés séparément dans le compte rendu.

## Comparaison entre les deux groupes

Comparer les mêmes messages et contenus, sous la même faute et avec les mêmes limites, puis publier séparément :

- le nombre de messages prévus et de tentatives émises ;
- le nombre de tentatives reçues et de doublons reçus ;
- les effets uniques confirmés par le registre du témoin ;
- les ACK reçus et validés par chaque émetteur ;
- les messages sans effet à la clôture ;
- les messages encore présents ou en attente dans la file durable ;
- les temps mesurés et les limites atteintes.

Un message peut avoir un effet confirmé côté témoin tout en restant non acquitté côté émetteur. Il peut également rester en file sans avoir encore produit d’effet. Le rapport ne doit pas fusionner ces deux situations sous un taux de perte unique.

Le terme `lost`, s’il apparaît dans les mesures, signifie seulement « absence d’effet unique correspondant dans le registre final du banc ». Le terme `duplicates` désigne des tentatives reçues en plus ; l’absence d’effets supplémentaires doit être démontrée séparément par la contrainte d’unicité et les lignes effectivement enregistrées.

## Temps, transport et conservation

Les durées doivent être calculées avec une horloge monotone. Les heures civiles servent à situer les traces, sans remplacer cette horloge pour calculer une durée ou une échéance. Les fichiers de sortie doivent permettre de retrouver l’ordre des événements et de distinguer le premier worker du processus repris.

Dans ce code, `end_to_end_ms` va de la création du message à l’horodatage d’acceptation enregistré dans la première transaction ensuite committée par le destinataire, sur la même horloge monotone. L’horodatage est pris avant la fin du commit ; il ne mesure donc pas cette fin exacte ni la réception de l’ACK. La moyenne volatile de l’essai final porte sur cinq messages arrivés, tandis que la moyenne durable porte sur dix messages et inclut l’attente pendant la coupure. Ces populations différentes ne permettent pas de conclure à un gain de vitesse réseau. Le bénéfice observé concerne le nombre d’effets livrés, au prix d’un délai supplémentaire pour les messages conservés.

L’ACK n’est fiable dans ce protocole que s’il correspond au bon identifiant et au bon digest, et confirme un commit. Une réussite de connexion TCP ou un statut HTTP isolé ne suffisent pas. Les messages et réponses restent bornés, avec une cible locale fixe et sans redirection vers une destination externe.

Conserver les fichiers de l’essai, les paramètres, les versions de code et les empreintes utiles à la reproduction. Une empreinte permet de vérifier qu’un fichier n’a pas changé par rapport à une valeur connue ; elle ne prouve pas à elle seule son origine ni l’absence d’altération par un acteur disposant des mêmes droits.

## Limites à rappeler avec tout résultat

- Le serveur HTTP tourne sur l’interface loopback du même hôte. Aucun réseau physique, opérateur ou accès distant n’est évalué.
- L’effet applicatif est une écriture dans le témoin contrôlé. L’idempotence d’une API externe ne découle pas de ce résultat.
- L’arrêt/reprise du worker teste la persistance entre processus. Il ne certifie ni la tenue à une panne électrique ni celle du stockage matériel.
- Les trente secondes constituent le budget du banc, pas une exigence de temps réel certifiée par le système d’exploitation.
- Les délais et compteurs ne mesurent pas un gain de bande passante réseau.
- Aucun appel Cloud n’est réalisé par ce banc ; `measurements.json` est un artefact local.
- Un futur dépôt Cloud pourrait conserver une mémoire utile à l’audit. À politique de livraison identique, il ne change pas les tentatives ni les effets et n’apporte pas, à lui seul, de gain de livraison. Aucune validation Cloud n’est revendiquée ici.

## Résultat de l’essai

Le [rapport réel du code final](lab-report.json), pour l’essai `8f8b860e672f4f8691121db727e15342` sur Darwin 25.6.0 arm64 avec la politique par défaut, indique une durée de 2,779408 secondes, dont une coupure mesurée de 2,008543 secondes. Le worker interrompu sort avec le code `-9` ; son remplaçant termine avec le code `0` et dix messages acquittés. Le destinataire compte dix effets uniques côté durable, cinq côté volatile, une tentative durable reçue en double et aucun effet dupliqué. Deux pertes d’ACK après commit sont enregistrées, une par canal. Les quatorze contrôles du rapport sont vrais, y compris la correspondance des digests reçus avec les messages source. La fermeture finale des ressources est également confirmée par `owned_processes_and_server_stopped=true`. Le rapport conserve les SHA256 des trois sources exécutées.

La suite locale compte onze tests réussis. Un test distinct interrompt réellement un worker après commit du destinataire, avant l’ACK : l’état `inflight` est constaté, puis la reprise obtient trois effets uniques, une tentative reçue en double et aucun effet supplémentaire. Cette vérification complète l’arrêt pendant la coupure du banc principal ; elle ne doit pas être confondue avec ce dernier. Les tests couvrent aussi la conservation du budget de tentatives, les ACK invalides et les conflits d’identifiant et de contenu.

Ces faits montrent une livraison complète dans ce scénario précis, avec des délais supplémentaires pour les messages conservés pendant la coupure. Ils ne démontrent pas une livraison plus rapide sur réseau physique. Pour tout autre essai, conserver sa version de code, ses paramètres, son rapport et ses éventuels échecs ; une attente du scénario ne doit jamais être présentée comme une mesure.
