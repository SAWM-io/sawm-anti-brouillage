# Démonstration transport en direct — VM Linux → Mac

**Périmètre : transport HTTP réel et adaptation locale.** Le passage complet du 27 octobre 2026 a livré et confirmé 100 messages, sans effet doublé. Les [preuves et les essais préparatoires](measured-live-2026-10-27/README.md) sont conservés. Cette procédure permet de reproduire le banc.

Le programme envoie 100 messages synthétiques, espacés de 400 ms, soit environ 40 secondes d'émission. Deux chemins virtuels A/B subissent des profils `netem`, puis rejoignent **le même tunnel SSH vers le Mac**. Ce ne sont pas deux radios ni deux liaisons physiques indépendantes. Aucun appel SAWM, Basira, TSST ou ODVL n'intervient dans cette boucle locale.

Le [rejeu du rapport d'origine à 16 canaux avec le moteur ODVL](reference-report-2026-10-27/README.md) est une expérience distincte. Les livraisons de cette démonstration ne prouvent pas le choix de canaux du simulateur original.

## Préparer les deux machines

- **Mac** : Python 3.10+, client SSH et copie de ce dépôt.
- **VM Linux contrôlée par l'utilisateur** : Python 3.10+, `iproute2` (`ip`, `tc`), prise en charge de `veth`, des namespaces réseau et de `netem`, ainsi que l'accès root pour les seules ressources du banc.
- **SSH** : utiliser un alias personnel `vm-lab`, déjà configuré pour joindre la VM. Les exemples ne contiennent ni adresse personnelle ni clé privée SSH. Le serveur SSH de la VM doit autoriser la redirection distante sur son interface loopback.

Dans les commandes suivantes, le dépôt est placé dans `~/sawm-anti-brouillage` sur chaque machine. Adapter ce chemin si nécessaire. Depuis le compte disposant de l'accès au dépôt :

```sh
git clone https://github.com/SAWM-io/sawm-anti-brouillage.git "$HOME/sawm-anti-brouillage"
```

Les composants live utilisent la bibliothèque standard Python. Le Mac conserve le port **8898** : les clients VM envoient explicitement l'en-tête HTTP `Host: 127.0.0.1:8898`, même lorsqu'ils passent par le port distant 18901.

## Créer le jeton de démonstration

L'utilisateur crée un jeton dédié, hors du dépôt, puis transmet le même fichier à la VM par SSH. Ce jeton est propre à la démonstration ; ce n'est pas une clé Basira. Ces commandes sont à exécuter sur le Mac. Le fichier existant n'est pas écrasé et le secret n'est pas affiché :

```sh
umask 077
mkdir -p "$HOME/.config/sawm"
python3 - "$HOME/.config/sawm/live-demo.token" <<'PY'
from pathlib import Path
import secrets
import sys
path = Path(sys.argv[1])
with path.open("x", encoding="ascii") as stream:
    stream.write(secrets.token_hex(32) + "\n")
path.chmod(0o600)
PY
ssh vm-lab 'umask 077; mkdir -p "$HOME/.config/sawm"'
scp "$HOME/.config/sawm/live-demo.token" vm-lab:.config/sawm/live-demo.token
ssh vm-lab 'chmod 600 "$HOME/.config/sawm/live-demo.token"'
```

Les deux programmes exigent un fichier régulier de permissions `600`. Ne pas placer le jeton dans une commande d'API, un journal, le HTML ou un fichier suivi par Git.

## Démarrer le récepteur sur le Mac

Dans un premier terminal Mac, depuis le dépôt :

```sh
cd "$HOME/sawm-anti-brouillage"
python3 live_receiver.py \
  --state-dir "$HOME/.local/state/sawm-live" \
  --token-file "$HOME/.config/sawm/live-demo.token" \
  --port 8898
```

Ouvrir [la démonstration locale](http://127.0.0.1:8898/). Le répertoire d'état peut être neuf ou réutilisé : SQLite conserve les reçus et les campagnes précédentes. Un message est identifié par le couple `(run_id, message_id)` ; le récepteur vérifie l'empreinte UTF-8 du contenu, committe l'effet puis renvoie son ACK. Une requête identique répétée reçoit une confirmation de doublon ; un contenu divergent sous le même identifiant est refusé avec le statut `409`.

Sans configuration de lancement, le bouton « Lancer / rejouer la démonstration » reste désactivé. Le lancement manuel dans la VM décrit ci-dessous fonctionne indépendamment de ce bouton.

## Ouvrir le tunnel SSH

Dans un deuxième terminal Mac, laisser cette commande active :

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -R 127.0.0.1:18901:127.0.0.1:8898 vm-lab
```

La redirection est **VM `127.0.0.1:18901` → Mac `127.0.0.1:8898`**. Elle est établie depuis le Mac ; le serveur HTTP du Mac reste lié à loopback. Le programme VM ne crée pas ce tunnel lui-même. Le port 18901 doit être disponible avant son ouverture.

## Lancer le scénario dans la VM

Ouvrir un terminal dans la VM, puis exécuter :

```sh
cd "$HOME/sawm-anti-brouillage"
sudo python3 live_vm_lab.py \
  --output-dir "$HOME/sawm-live-demo-01" \
  --token-file "$HOME/.config/sawm/live-demo.token" \
  --scripted
```

Choisir un **nouveau répertoire de sortie** pour chaque passage. Le scénario automatique demande les profils suivants ; les traces conservent leurs instants réels d'application :

| Temps prévu | Profil | Chemin A, par sens | Chemin B, par sens |
| --- | --- | --- | --- |
| 0–5 s | `healthy` | 3 ms, sans perte injectée | 15 ms, sans perte injectée |
| 5–12 s | `degraded_a` | 80 ± 20 ms, 10 % de pertes | 15 ms, sans perte injectée |
| 12–20 s | `cut_a` | 100 % de pertes | 15 ms, sans perte injectée |
| 20–25 s | `cut_both` | 100 % de pertes | 100 % de pertes |
| Dès 25 s | `recovery` | 3 ms, sans perte injectée | 15 ms, sans perte injectée |

Le budget global est de 120 secondes et les tentatives sont bornées. Les 40 secondes décrivent la période d'émission, pas une garantie de livraison complète. Le sélecteur de chemin utilise les observations terminées et ses sondes de reprise ; il ne reçoit pas le calendrier des fautes comme entrée de décision.

Sans `--scripted`, le programme démarre en mode manuel. Les boutons « Dégrader A », « Couper A », « Couper A + B » et « Rétablir » publient une commande que la VM applique lors d'un échange de statut. Une commande demandée et un profil rapporté restent affichés séparément.

## Lire le dashboard

Le bandeau « Démonstration transport · adaptation locale » rappelle le périmètre. Les reçus uniques proviennent du registre SQLite du Mac ; les messages générés, en attente et les compteurs de tentatives sont déclarés par la VM. Les requêtes répétées et les effets dupliqués sont deux compteurs distincts.

La télémétrie devient périmée après trois secondes sans nouvel état VM. Des données historiques restent consultables, mais ne sont pas présentées comme une connexion active. Le canal de contrôle peut continuer à communiquer pendant une coupure A+B : cela ne prouve pas que les messages franchissent les chemins coupés. À la fin du processus ou sur un statut terminal, la vue conserve le résultat, les courbes et la chronologie.

Les délais affichés sont calculés côté VM ; aucune soustraction entre horloges VM et Mac ne mesure un RTT. La chronologie montre des événements et des raisons publiées par le programme, pas des pensées cachées d'un modèle. Le sélecteur de campagne permet de consulter les passages précédents.

## Activer le bouton de lancement, si souhaité

Le récepteur accepte `--launcher-config` pointant vers un JSON privé de permissions `600`. Le document contient exactement `argv`, `cwd` et `log_dir`, avec un exécutable et des chemins locaux absolus. Il est lu au démarrage ; aucun argument venant du navigateur n'est ajouté à la commande.

Exemple de configuration **à adapter aux chemins du Mac** :

```json
{
  "argv": ["/usr/bin/ssh", "-o", "BatchMode=yes", "vm-lab", ".local/bin/sawm-live-demo"],
  "cwd": "/chemin/absolu/sawm-anti-brouillage",
  "log_dir": "/chemin/absolu/prive/sawm-live-logs"
}
```

Dans cet exemple, l'utilisateur prépare sur la VM le script fixe `~/.local/bin/sawm-live-demo`, exécutable, et configure l'autorisation nécessaire pour que **cette commande** fonctionne sans invite interactive. Le script choisit un nouveau dossier pour chaque passage :

```sh
#!/bin/sh
set -eu
umask 077
live_run_root=$(mktemp -d "$HOME/sawm-live-run.XXXXXX")
exec sudo -n /usr/bin/python3 "$HOME/sawm-anti-brouillage/live_vm_lab.py" \
  --output-dir "$live_run_root/evidence" \
  --token-file "$HOME/.config/sawm/live-demo.token" --scripted
```

Sur le Mac, donner les permissions `600` au JSON, arrêter le récepteur puis le relancer en conservant le même état et en ajoutant :

```sh
--launcher-config "$HOME/.config/sawm/live-launcher.json"
```

Le bouton lance uniquement cet `argv` fixe, avec `shell=False` côté Mac et un journal privé. Un second lancement est refusé tant que le processus est actif. « Scénario auto · nouvel essai » démarre une nouvelle campagne ; il ne remet pas silencieusement en mode automatique une campagne manuelle déjà en cours. Le tunnel SSH doit rester ouvert.

Les modifications depuis le navigateur exigent l'origine exacte `http://127.0.0.1:8898`, du JSON et un nonce de page transmis dans `X-Demo-Token`. Le jeton Bearer utilisé par la VM n'est pas incorporé au HTML.

## Relier le rapport d'origine

Le lien « Rapport d’origine » ouvre `/report-reference`. Pour le renseigner, placer un petit fichier `report-reference.json` dans le répertoire d'état du récepteur :

```json
{
  "title": "Le Réseau qui se souvient — référence distincte",
  "summary": "La démonstration live transporte des messages par deux chemins virtuels. Le rejeu original à 16 canaux utilise le moteur ODVL privé et conserve ses propres métriques et traces.",
  "source_label": "Rejeu documenté du modèle original",
  "source_url": "https://github.com/SAWM-io/sawm-anti-brouillage/blob/main/docs/reference-report-2026-10-27/README.md"
}
```

La page lit ce fichier fixe et échappe son texte. Le [dossier de référence](reference-report-2026-10-27/README.md) précise les sources privées nécessaires, le raccordement direct à `ODVLNetwork` et l'absence d'appel Cloud ou TSST dans ce rejeu. Aucun gain du modèle radio numérique ne doit être attribué automatiquement au transport HTTP live.

## Conserver les preuves et arrêter

Lire le `report.json` du passage VM, ses compteurs, son état final, les phases, les tentatives et les vérifications de nettoyage. Une télémétrie visible ou un serveur répondant ne remplace pas ces vérifications. Conserver aussi l'état SQLite du récepteur Mac pour rapprocher les effets réellement enregistrés des ACK vus par la VM.

Après la fin du passage, arrêter le tunnel SSH et le récepteur avec leur terminal. Les ressources réseau créées pour l'essai sont nettoyées par le programme VM ; les rapports indiquent si cette étape a abouti. La file et les reçus démontrent une persistance applicative ; cette procédure ne certifie ni une reprise après redémarrage de machine, ni une résistance RF.
