# Passage complet — contrôle de nettoyage trop strict

Les cinq phases ont été exercées ; 100 effets uniques Mac et 100 ACK, aucun doublon. La sortie reste `success: false` car la destruction du namespace a supprimé un veth entre `ip link show` et `ip link delete`. La suppression signalait alors « Cannot find device », bien que l’interface ait disparu. Le réseau principal est inchangé et aucun namespace du banc ne demeure.

Le correctif vérifie explicitement la liste des interfaces après cet échec transitoire, en conservant un refus si l’interface demeure. Le test de régression couvre les deux cas. Cette preuve initiale est conservée intacte.
