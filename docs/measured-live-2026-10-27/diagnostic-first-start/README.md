# Premier passage — diagnostic conservé

100 effets uniques, 100 ACK, aucun effet doublé. Le démarrage HTTP résolvait les adresses virtuelles en DNS : il a consommé 16 secondes et fait sauter `degraded_a`. Le champ `success` de cette version ne contrôlait pas la couverture des phases. Ce passage **ne valide pas le scénario complet**.

La version suivante évite la résolution DNS, démarre l’horloge après la préparation et exige les cinq phases ainsi que des confirmations durant la dégradation, la voie B pendant la coupure de A et zéro ACK durant la coupure totale stabilisée. Les anciennes preuves restent intactes ici.
