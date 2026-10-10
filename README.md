# AutoPost Reels — publication automatique sur Instagram (100 % gratuit)

Ce bot publie tes Reels tout seul sur tes 30 comptes Instagram :

- **10 Reels par jour et par compte**, en deux sessions (heure du Bénin) :
  - session 1 : 16:00, 17:12, 18:24, 19:36, 20:48
  - session 2 : 00:00, 01:12, 02:24, 03:36, 04:48
- Les vidéos sont prises dans **Google Drive**, un dossier par compte.
- Il tourne sur **GitHub Actions**, sans serveur et sans abonnement.
- Il passe par l'**API officielle d'Instagram**. Tes mots de passe ne sont jamais utilisés.
- Une fois publiée, une vidéo est rangée dans `publies`. Après 3 échecs, elle va dans `echecs`.
- En cas de problème, GitHub t'envoie un **e-mail**.

> Pour la mise en place, prévois **1 à 2 heures, sur un ordinateur**. Ensuite, tout se gère depuis le téléphone : tu déposes simplement des vidéos dans Drive.

---

## Étape 1 — Préparer les comptes Instagram

Chaque compte doit être un **compte professionnel** (Créateur ou Entreprise).
Dans l'app : *Paramètres → Type de compte et outils → Passer à un compte professionnel*.

## Étape 2 — Préparer Google Drive

1. Dans Google Drive, crée un dossier **`AutoPost`**.
2. Dans ce dossier, crée **un sous-dossier par compte**, nommé exactement comme dans `config.yaml` : `compte01`, `compte02`… `compte30`. Tu peux renommer les comptes, mais il faut alors changer le nom aux deux endroits.
3. Dépose les vidéos dans le sous-dossier du compte :
   - Elles sont publiées dans **l'ordre alphabétique**. Numérote-les : `001.mp4`, `002.mp4`…
   - **Légende** : crée un fichier texte du même nom (`001.txt`) contenant la légende et les hashtags. Sans fichier texte, la légende prévue dans `config.yaml` est utilisée.
   - Format conseillé : MP4, vertical 9:16, entre 3 s et 15 min, moins de 300 Mo.
4. Ouvre le dossier `AutoPost` et copie son **identifiant** : c'est la fin de l'adresse, après `folders/`.
   Exemple : `https://drive.google.com/drive/folders/`**`1AbCdEfGhIjKlMnOp`** → `1AbCdEfGhIjKlMnOp`.

Les sous-dossiers `publies` et `echecs` sont créés automatiquement. Vide `publies` de temps en temps : Drive gratuit est limité à 15 Go.

## Étape 3 — Donner au bot l'accès à Drive (compte de service Google)

1. Va sur **https://console.cloud.google.com** et crée un projet, par exemple `autopost`.
2. Ouvre *API et services → Bibliothèque*, cherche **Google Drive API** et clique sur **Activer**.
3. Ouvre *IAM et administration → Comptes de service → Créer un compte de service*. Donne-lui un nom (`autopost-bot`), puis clique sur **OK** sans ajouter de rôle.
4. Clique sur le compte créé, puis *Clés → Ajouter une clé → Créer une clé → JSON*. Un fichier `.json` est téléchargé. **Garde-le secret.**
5. Copie l'adresse e-mail du compte de service (`autopost-bot@….iam.gserviceaccount.com`).
6. Dans Drive, fais un clic droit sur `AutoPost` → **Partager**, colle cette adresse et donne le rôle **Éditeur**.

## Étape 4 — Obtenir les tokens Instagram (application Meta)

1. Va sur **https://developers.facebook.com**, connecte-toi, puis *Mes apps → Créer une app*.
2. Choisis le cas d'usage **« Gérer les messages et le contenu sur Instagram »**, ou le type **Business**, puis termine la création.
3. Dans l'app, ouvre **Instagram → Configuration de l'API avec la connexion Instagram**.
4. Dans la section **« Générer des tokens d'accès »**, clique sur **Ajouter un compte** et connecte-toi avec le premier compte Instagram.
   - Si Instagram demande d'accepter une invitation de testeur, va dans *Paramètres → Apps et sites web → Invitations de testeur* et accepte.
5. Clique sur **Générer le token** à côté du compte, puis copie le token (il commence par `IGAA…`).
6. Répète pour les 30 comptes. Pour chaque compte, déconnecte-toi d'Instagram dans le navigateur avant d'ajouter le suivant.

L'app peut rester en **mode développement** : comme tu publies seulement sur tes propres comptes, aucune validation de Meta n'est nécessaire.

Prépare ensuite ce texte (modèle dans `exemple-IG_TOKENS.json`), avec un token par compte :

```json
{
  "compte01": "IGAA…token du compte 1…",
  "compte02": "IGAA…token du compte 2…"
}
```

## Étape 5 — Créer le dépôt GitHub

1. Sur **https://github.com/new**, crée un dépôt nommé `autopost-reels`.
   - Choisis **Public** : GitHub Actions est alors illimité et gratuit. Un dépôt privé ne donne que 2 000 minutes/mois, insuffisant pour 30 comptes.
   - Aucun risque : tes tokens et tes clés sont dans les **Secrets**, jamais visibles, et le code ne contient aucune donnée personnelle.
2. Clique sur *uploading an existing file* et dépose **tous les fichiers de ce dossier**, y compris le dossier `.github`.
   - Si le dossier `.github` n'apparaît pas, crée les fichiers à la main : *Add file → Create new file*, tape `.github/workflows/publier.yml` comme nom, puis colle le contenu. Fais de même pour `rafraichir-tokens.yml`.

## Étape 6 — Ajouter les secrets

Dans le dépôt : *Settings → Secrets and variables → Actions → New repository secret*. Crée ces 4 secrets :

| Nom | Contenu |
|---|---|
| `IG_TOKENS` | Le texte JSON de l'étape 4 |
| `GOOGLE_SERVICE_ACCOUNT` | Tout le contenu du fichier `.json` de l'étape 3 (ouvre-le et copie-colle) |
| `DRIVE_DOSSIER_ID` | L'identifiant du dossier `AutoPost` (étape 2) |
| `GH_PAT` | Un token GitHub qui permet au bot de renouveler les tokens Instagram (voir ci-dessous) |

**Créer `GH_PAT`** : *Photo de profil → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token*.
- *Repository access* : **Only select repositories** → `autopost-reels`
- *Permissions → Repository permissions* : **Secrets → Read and write**
- *Expiration* : la plus longue possible. Note la date et pense à le recréer avant l'échéance.

Les tokens Instagram expirent après 60 jours : le bot les renouvelle **chaque lundi** grâce à ce secret.

## Étape 7 — Tester

Dans l'onglet **Actions** du dépôt (active les workflows si GitHub le demande) :

1. **Publier les Reels → Run workflow → `verifier`**
   Pour chaque compte, tu dois voir ✅, le pseudo Instagram et le nombre de vidéos en stock. Les ❌ indiquent le problème à corriger.
2. **Run workflow → `simulation`** (pendant une session : 16 h–22 h ou 00 h–6 h)
   Affiche les vidéos qui seraient publiées, sans rien publier.
3. **Run workflow → `publier`** : vraie publication pour les créneaux échus.
4. **Renouveler les tokens Instagram → Run workflow** : vérifie que le renouvellement fonctionne.

Si tout est vert, c'est terminé : le bot tourne tout seul.

---

## Au quotidien

- **Approvisionne le stock** : 10 vidéos par jour et par compte, soit 300 par jour pour 30 comptes. Le mode `verifier` indique combien de jours de stock il reste par compte.
- **Mettre un compte en pause** : dans `config.yaml`, ajoute `actif: false` sous le compte.
- **Changer les horaires ou le rythme** : modifie `sessions` et `posts_par_session` dans `config.yaml`. Si les heures changent, ajuste aussi les lignes `cron` de `.github/workflows/publier.yml` (elles sont en heure UTC, soit l'heure du Bénin moins 1 h).
- **Historique** : onglet *Actions*. Chaque passage affiche un tableau des publications ✅ et des erreurs ❌.
- **E-mail d'erreur** : ouvre le passage concerné dans *Actions* pour voir le détail. Les vidéos refusées 3 fois sont dans `echecs`, avec la raison dans leurs propriétés.

## Bon à savoir

- **Retards de GitHub** : les tâches planifiées peuvent partir avec 5 à 20 minutes de retard aux heures chargées. Le bot rattrape automatiquement les créneaux manqués (2 posts maximum par passage).
- **Limite d'Instagram** : l'API autorise au maximum 100 publications par compte et par 24 h. Avec 10 par jour, tu en es loin.
- **Anti-spam** : 10 Reels par jour sur 30 comptes, c'est un rythme élevé. Varie les vidéos et les légendes d'un compte à l'autre (évite de publier la même vidéo partout) pour ne pas déclencher les protections anti-spam d'Instagram.
- **Changement de mot de passe Instagram** : génère un nouveau token pour ce compte (étape 4) et mets à jour `IG_TOKENS`.

## Contenu du dossier

| Fichier | Rôle |
|---|---|
| `autopost.py` | Le bot (modes `verifier`, `simulation`, `publier`, `rafraichir`) |
| `config.yaml` | Horaires, comptes, légendes (aucun secret) |
| `.github/workflows/publier.yml` | Lancement automatique toutes les 15 min pendant les sessions |
| `.github/workflows/rafraichir-tokens.yml` | Renouvellement hebdomadaire des tokens |
| `exemple-IG_TOKENS.json` | Modèle du secret `IG_TOKENS` |
| `requirements.txt` | Dépendances Python |
