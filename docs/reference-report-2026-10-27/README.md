# Rejeu du rapport « Le Réseau qui se souvient » — lien à 16 canaux

Le [relevé du 27 octobre 2026](reproduction.json) rejoue le **simulateur numérique original** avec le moteur Python privé `ODVLNetwork`. Il est distinct du laboratoire HTTP et de la démonstration VM → Mac : les messages effectivement transportés par ces derniers ne prouvent pas le choix de canaux radio décrit dans le rapport.

## Protocole et résultats reproduits

Huit graines, 1 200 pas de 100 ms, 16 canaux, puissance simulée de 22 dB. La perturbation débute au pas 150 et dure 950 pas. Un changement de canal coûte deux pas ; le poursuivant a un retard de quatre pas et le balayage couvre trois canaux. Les six conduites A–F du banc original sont conservées dans le JSON, y compris les résultats sans gain.

| Perturbation simulée | B, conduite réactive | F, moteur ODVL | Comparaison appariée |
| --- | ---: | ---: | --- |
| Barrage sur toute la bande | 18,50 % | 18,50 % | Égalité sur 8 graines |
| Canal ciblé (« spot ») | 88,90 % | 88,90 % | Égalité sur 8 graines |
| Balayage | 81,54 % | 84,76 % | ODVL devant sur 8/8 graines |
| Poursuivant | 53,59 % | 55,41 % | ODVL devant sur 8/8 graines |

La mesure est le débit cumulé du modèle, rapporté à son débit nominal. Ce n'est ni un taux de messages HTTP livrés, ni une mesure sur un émetteur radio. Le relevé retrouve les gains arrondis du rapport de référence : environ **+0,032** face au balayage et **+0,018** face au poursuivant. Les moyennes et écarts appariés sont arrondis séparément dans le programme d'origine ; leur dernière décimale peut différer d'une soustraction de moyennes déjà arrondies. Les graines individuelles restent disponibles.

Ce rejeu ne refait pas les autres parties du rapport : équivalence en décibels d'antenne, localisation, drones ou couverture de bande. Il ne constitue pas une comparaison exhaustive aux produits du marché.

## Reproduire avec les sources privées autorisées

Depuis la racine de ce dépôt, avec Python 3.10 ou ultérieur :

```sh
python3 reference_replay.py \
  --private-source-root /chemin/vers/le/depot-prive \
  --output-dir output/reference-radio-nouvelle
```

L'arborescence privée doit contenir `experiments/lien-adaptatif/` et `backend/services/basira_odvl.py`. Aucun fichier du moteur n'est copié dans ce dépôt. L'absence du chemin ou des sources fait échouer explicitement l'exécution ; aucun remplacement heuristique n'est utilisé.

Le script produit `replay.json`, un manifeste SHA-256 et huit traces graine 0 (quatre perturbations × deux conduites B/F). Chaque trace conserve les **16 observations SNR simulées**, le canal avant/après choix et le débit à chaque pas, en instrumentant la boucle originale sans appel aléatoire supplémentaire. `--trace-only` permet de produire uniquement ces traces sans répéter la campagne à huit graines ; ce mode est indiqué dans le résultat.

## Raccordement technologique exact

Le moteur ODVL privé est appelé directement et conserve son état entre pas. **Aucun nouvel appel Cloud, aucun modèle de langage SAWM et aucun bras TSST ne sont exécutés dans ce rejeu.** Un classement public `/odvl/rank` sans cet état évolutif n'est pas équivalent à `ODVLNetwork`. Le futur raccordement Cloud de cette boucle reste à réaliser et à comparer ; les appels Cloud du laboratoire HTTP sont une preuve différente.

`reproduction.json` est la copie intacte du relevé mesuré avant l'ajout de cet adaptateur. Il contient les empreintes des sources effectivement rejouées. Les futures exécutions écrivent dans un répertoire neuf et ajoutent leurs propres empreintes et traces ; elles ne remplacent pas ce relevé historique.

Les [8 traces graine 0 archivées](traces-seed0/replay.json) ont été rejouées par cet adaptateur ; leurs résultats correspondent exactement à la graine 0 du relevé initial. Leur [manifeste](traces-seed0/manifest.json) permet de contrôler les fichiers. La campagne complète à huit graines a ensuite été exécutée par l’adaptateur : [relevé complet](validated-full-replay.json), toutes les sources inchangées et traces cohérentes avec les résultats de référence.

[Ouvrir la visualisation autonome](index.html) — télécharger le fichier HTML puis l’ouvrir dans un navigateur ; les données sont intégrées.
