#!/usr/bin/env python3
"""
AutoPost Reels — publication automatique de Reels Instagram, 100 % GitHub Actions.

Les vidéos sont prises dans Google Drive (un sous-dossier par compte), publiées
via l'API officielle d'Instagram (upload direct, aucun hébergement public),
puis rangées dans le sous-dossier « publies » (ou « echecs »).

Modes :
  python autopost.py verifier     -> contrôle tokens, dossiers Drive, vidéos en stock
  python autopost.py simulation   -> montre ce qui serait publié maintenant, sans publier
  python autopost.py publier      -> publie les Reels dus pour la session en cours
  python autopost.py rafraichir --sortie FICHIER -> renouvelle les tokens (60 jours)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

log = logging.getLogger("autopost")

FOLDER_MIME = "application/vnd.google-apps.folder"
DOSSIER_PUBLIES = "publies"
DOSSIER_ECHECS = "echecs"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def charger_config(chemin: str = "config.yaml") -> dict:
    with open(chemin, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("fuseau_horaire", "Africa/Porto-Novo")
    cfg.setdefault("posts_par_session", 5)
    cfg.setdefault("rattrapage_max", 2)
    cfg.setdefault("marge_minutes", 45)
    cfg.setdefault("tentatives_max", 3)
    cfg.setdefault("publications_en_parallele", 5)
    cfg.setdefault("legende_par_defaut", "")
    cfg.setdefault("api", {})
    cfg["api"].setdefault("hote", "graph.instagram.com")
    cfg["api"].setdefault("version", "v23.0")
    comptes = []
    for c in cfg.get("comptes") or []:
        if isinstance(c, str):
            c = {"nom": c}
        c.setdefault("dossier", c["nom"])
        c.setdefault("legende", cfg["legende_par_defaut"])
        c.setdefault("actif", True)
        if c["actif"]:
            comptes.append(c)
    cfg["comptes"] = comptes
    return cfg


def charger_tokens() -> dict:
    brut = os.environ.get("IG_TOKENS", "").strip()
    if not brut:
        raise SystemExit("Secret IG_TOKENS manquant.")
    try:
        data = json.loads(brut)
    except json.JSONDecodeError as e:
        raise SystemExit(f"IG_TOKENS n'est pas un JSON valide : {e}")
    # Accepte {"compte": "TOKEN"} ou {"compte": {"token": "...", "user_id": "..."}}
    return {k: ({"token": v} if isinstance(v, str) else v) for k, v in data.items()}


# --------------------------------------------------------------------------- #
# Planning des créneaux
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    id: str
    debut: datetime
    fin: datetime
    creneaux: list = field(default_factory=list)

    def dus(self, maintenant: datetime) -> int:
        return sum(1 for c in self.creneaux if c <= maintenant)


def _hm(texte: str) -> tuple[int, int]:
    h, m = str(texte).split(":")
    return int(h), int(m)


def session_courante(maintenant: datetime, sessions: list, posts: int, marge: int) -> Session | None:
    """Retourne la session active (créneaux répartis régulièrement), ou None."""
    for idx, s in enumerate(sessions, start=1):
        dh, dm = _hm(s["debut"])
        fh, fm = _hm(s["fin"])
        for decalage in (0, -1):  # session commencée aujourd'hui, ou hier si elle passe minuit
            jour = (maintenant + timedelta(days=decalage)).date()
            debut = datetime(jour.year, jour.month, jour.day, dh, dm, tzinfo=maintenant.tzinfo)
            fin = datetime(jour.year, jour.month, jour.day, fh, fm, tzinfo=maintenant.tzinfo)
            if fin <= debut:
                fin += timedelta(days=1)
            if debut <= maintenant <= fin + timedelta(minutes=marge):
                pas = (fin - debut) / posts
                creneaux = [debut + pas * k for k in range(posts)]
                return Session(f"{debut:%Y-%m-%d}-S{idx}", debut, fin, creneaux)
    return None


# --------------------------------------------------------------------------- #
# Google Drive
# --------------------------------------------------------------------------- #
class Drive:
    def __init__(self, identifiants_json: str):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        info = json.loads(identifiants_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive"]
        )
        self.email = info.get("client_email", "?")
        self.svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    @staticmethod
    def _q(texte: str) -> str:
        return texte.replace("\\", "\\\\").replace("'", "\\'")

    def lister(self, q: str, champs: str = "files(id,name,mimeType,size,appProperties)") -> list:
        resultats, page = [], None
        while True:
            r = self.svc.files().list(
                q=q, fields=f"nextPageToken,{champs}", pageSize=1000, orderBy="name",
                pageToken=page, supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            resultats += r.get("files", [])
            page = r.get("nextPageToken")
            if not page:
                return resultats

    def sous_dossier(self, parent: str, nom: str, creer: bool = False) -> str | None:
        q = (f"'{parent}' in parents and name = '{self._q(nom)}' "
             f"and mimeType = '{FOLDER_MIME}' and trashed = false")
        trouves = self.lister(q, "files(id,name)")
        if trouves:
            return trouves[0]["id"]
        if not creer:
            return None
        f = self.svc.files().create(
            body={"name": nom, "mimeType": FOLDER_MIME, "parents": [parent]},
            fields="id", supportsAllDrives=True,
        ).execute()
        return f["id"]

    def contenu(self, dossier: str) -> list:
        return self.lister(f"'{dossier}' in parents and trashed = false and mimeType != '{FOLDER_MIME}'")

    def telecharger(self, file_id: str, chemin: str) -> None:
        from googleapiclient.http import MediaIoBaseDownload

        req = self.svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        with open(chemin, "wb") as f:
            dl = MediaIoBaseDownload(f, req, chunksize=50 * 1024 * 1024)
            fini = False
            while not fini:
                _, fini = dl.next_chunk()

    def lire_texte(self, file_id: str) -> str:
        brut = self.svc.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
        return brut.decode("utf-8", errors="replace").strip()

    def deplacer(self, file_id: str, de: str, vers: str, props: dict) -> None:
        self.svc.files().update(
            fileId=file_id, addParents=vers, removeParents=de,
            body={"appProperties": props}, supportsAllDrives=True, fields="id",
        ).execute()

    def maj_props(self, file_id: str, props: dict) -> None:
        self.svc.files().update(
            fileId=file_id, body={"appProperties": props}, supportsAllDrives=True, fields="id",
        ).execute()

    def compter_session(self, dossier_publies: str, session_id: str) -> int:
        q = (f"'{dossier_publies}' in parents and trashed = false and "
             f"appProperties has {{ key='session' and value='{self._q(session_id)}' }}")
        return len(self.lister(q, "files(id)"))


# --------------------------------------------------------------------------- #
# Instagram (API officielle)
# --------------------------------------------------------------------------- #
class InstagramErreur(Exception):
    pass


class Instagram:
    def __init__(self, token: str, hote: str, version: str, user_id: str | None = None):
        self.token = token
        self.version = version
        self.base = f"https://{hote}/{version}"
        self._user_id = user_id
        self._username = None

    def _req(self, methode: str, url: str, **kw) -> dict:
        r = requests.request(methode, url, timeout=kw.pop("timeout", 120), **kw)
        try:
            data = r.json()
        except ValueError:
            data = {"reponse": r.text[:300]}
        if r.status_code >= 400 or (isinstance(data, dict) and "error" in data):
            err = data.get("error", data) if isinstance(data, dict) else data
            if isinstance(err, dict):
                err = err.get("message", err)
            raise InstagramErreur(f"HTTP {r.status_code} : {err}")
        return data

    def profil(self) -> tuple[str, str]:
        if not self._user_id or not self._username:
            champs = "user_id,username" if "instagram.com" in self.base else "id,username"
            cible = "me" if not self._user_id else self._user_id
            d = self._req("GET", f"{self.base}/{cible}", params={"fields": champs, "access_token": self.token})
            self._user_id = self._user_id or str(d.get("user_id") or d.get("id"))
            self._username = d.get("username", "?")
        return self._user_id, self._username

    def publier_reel(self, chemin: str, legende: str, attente_max: int = 900) -> str:
        user_id, _ = self.profil()
        taille = os.path.getsize(chemin)

        # 1. Conteneur Reel en mode upload direct
        c = self._req("POST", f"{self.base}/{user_id}/media", data={
            "media_type": "REELS", "upload_type": "resumable", "caption": legende,
            "share_to_feed": "true", "access_token": self.token,
        })
        cid = c["id"]
        uri = c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{self.version}/{cid}"

        # 2. Envoi du fichier vidéo
        with open(chemin, "rb") as f:
            self._req("POST", uri, timeout=600, data=f, headers={
                "Authorization": f"OAuth {self.token}", "offset": "0", "file_size": str(taille),
            })

        # 3. Attente du traitement par Instagram
        debut = time.time()
        while True:
            s = self._req("GET", f"{self.base}/{cid}",
                          params={"fields": "status_code,status", "access_token": self.token})
            code = s.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise InstagramErreur(f"traitement de la vidéo refusé ({code}) : {s.get('status', '')}")
            if time.time() - debut > attente_max:
                raise InstagramErreur("traitement de la vidéo trop long (> 15 min)")
            time.sleep(10)

        # 4. Publication
        p = self._req("POST", f"{self.base}/{user_id}/media_publish",
                      data={"creation_id": cid, "access_token": self.token})
        return str(p["id"])

    def rafraichir(self) -> str:
        d = self._req("GET", "https://graph.instagram.com/refresh_access_token",
                      params={"grant_type": "ig_refresh_token", "access_token": self.token})
        return d["access_token"]


# --------------------------------------------------------------------------- #
# Publication
# --------------------------------------------------------------------------- #
@dataclass
class Resultat:
    compte: str
    fichier: str
    statut: str  # publie | simule | echec | abandon
    detail: str = ""


def _est_video(f: dict) -> bool:
    return f.get("mimeType", "").startswith("video/") or f["name"].lower().endswith((".mp4", ".mov"))


def traiter_compte(compte: dict, cfg: dict, tokens: dict, drive: Drive, racine: str,
                   session: Session, maintenant: datetime, simulation: bool,
                   fabrique_ig=Instagram) -> list[Resultat]:
    nom = compte["nom"]
    res: list[Resultat] = []
    infos = tokens.get(nom)
    if not infos or not infos.get("token"):
        return [Resultat(nom, "-", "echec", "aucun token dans IG_TOKENS")]

    dossier = drive.sous_dossier(racine, compte["dossier"])
    if not dossier:
        return [Resultat(nom, "-", "echec", f"dossier Drive « {compte['dossier']} » introuvable")]
    publies = drive.sous_dossier(dossier, DOSSIER_PUBLIES, creer=True)

    deja = drive.compter_session(publies, session.id)
    a_publier = min(session.dus(maintenant) - deja, cfg["rattrapage_max"])
    if a_publier <= 0:
        return []

    fichiers = drive.contenu(dossier)
    textes = {Path(f["name"]).stem: f["id"] for f in fichiers if f["name"].lower().endswith(".txt")}
    videos = [f for f in fichiers if _est_video(f)][:a_publier]
    if not videos:
        return [Resultat(nom, "-", "echec", "plus aucune vidéo en stock")]

    ig = fabrique_ig(infos["token"], infos.get("hote", cfg["api"]["hote"]),
                     cfg["api"]["version"], infos.get("user_id"))

    for v in videos:
        stem = Path(v["name"]).stem
        legende = drive.lire_texte(textes[stem]) if stem in textes else compte["legende"]
        if simulation:
            res.append(Resultat(nom, v["name"], "simule", f"légende : {legende[:60]!r}"))
            continue
        props = dict(v.get("appProperties") or {})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                chemin = os.path.join(tmp, "video" + (Path(v["name"]).suffix or ".mp4"))
                drive.telecharger(v["id"], chemin)
                media_id = ig.publier_reel(chemin, legende)
            props.update({"session": session.id, "media_id": media_id,
                          "publie_le": maintenant.strftime("%Y-%m-%d %H:%M")})
            drive.deplacer(v["id"], dossier, publies, props)
            if stem in textes:
                drive.deplacer(textes[stem], dossier, publies, {"session_legende": session.id})
            res.append(Resultat(nom, v["name"], "publie", f"media {media_id}"))
        except Exception as e:  # noqa: BLE001 — on continue avec les autres comptes
            essais = int(props.get("tentatives", "0")) + 1
            props["tentatives"] = str(essais)
            props["derniere_erreur"] = str(e).encode("utf-8")[:90].decode("utf-8", "ignore")  # limite Drive : 124 octets
            try:
                if essais >= cfg["tentatives_max"]:
                    echecs = drive.sous_dossier(dossier, DOSSIER_ECHECS, creer=True)
                    drive.deplacer(v["id"], dossier, echecs, props)
                    res.append(Resultat(nom, v["name"], "abandon", f"{e} → déplacée dans « echecs »"))
                else:
                    drive.maj_props(v["id"], props)
                    res.append(Resultat(nom, v["name"], "echec", f"essai {essais}/{cfg['tentatives_max']} : {e}"))
            except Exception as e2:  # noqa: BLE001
                res.append(Resultat(nom, v["name"], "echec", f"{e} (+ Drive : {e2})"))
            break  # ne pas enchaîner sur la vidéo suivante si le compte pose problème
    return res


def ecrire_resume(titre: str, lignes: list[Resultat], entete: str = "") -> None:
    texte = [f"## {titre}", ""]
    if entete:
        texte += [entete, ""]
    if lignes:
        texte += ["| Compte | Fichier | Statut | Détail |", "|---|---|---|---|"]
        icones = {"publie": "✅", "simule": "🧪", "echec": "❌", "abandon": "⛔", "ok": "✅"}
        for r in lignes:
            detail = r.detail.replace("|", "/")
            texte.append(f"| {r.compte} | {r.fichier} | {icones.get(r.statut, '')} {r.statut} | {detail} |")
    else:
        texte.append("Rien à faire pour ce passage.")
    sortie = "\n".join(texte)
    print(sortie)
    chemin = os.environ.get("GITHUB_STEP_SUMMARY")
    if chemin:
        with open(chemin, "a", encoding="utf-8") as f:
            f.write(sortie + "\n")


def cmd_publier(cfg: dict, simulation: bool, maintenant: datetime | None = None,
                drive: Drive | None = None, fabrique_ig=Instagram) -> int:
    tz = ZoneInfo(cfg["fuseau_horaire"])
    maintenant = maintenant or datetime.now(tz)
    session = session_courante(maintenant, cfg["sessions"], cfg["posts_par_session"], cfg["marge_minutes"])
    if not session:
        print(f"{maintenant:%d/%m %H:%M} : hors session, rien à publier.")
        return 0

    manquants = [n for n in ("IG_TOKENS", "GOOGLE_SERVICE_ACCOUNT", "DRIVE_DOSSIER_ID")
                 if not os.environ.get(n, "").strip()]
    if manquants and drive is None:
        print(f"Bot pas encore configuré (secrets manquants : {', '.join(manquants)}). Rien publié.")
        return 0

    tokens = charger_tokens()
    drive = drive or Drive(os.environ["GOOGLE_SERVICE_ACCOUNT"])
    racine = os.environ["DRIVE_DOSSIER_ID"].strip()

    def un_compte(c):
        try:
            return traiter_compte(c, cfg, tokens, drive, racine, session, maintenant, simulation, fabrique_ig)
        except Exception as e:  # noqa: BLE001
            return [Resultat(c["nom"], "-", "echec", f"erreur inattendue : {e}")]

    with ThreadPoolExecutor(max_workers=cfg["publications_en_parallele"]) as pool:
        resultats = [r for lot in pool.map(un_compte, cfg["comptes"]) for r in lot]

    entete = (f"Session **{session.id}** — {maintenant:%d/%m/%Y %H:%M} "
              f"({session.dus(maintenant)}/{len(session.creneaux)} créneaux échus)")
    ecrire_resume("Simulation" if simulation else "Publication", resultats, entete)
    return 1 if any(r.statut in ("echec", "abandon") for r in resultats) else 0


def cmd_verifier(cfg: dict, drive: Drive | None = None, fabrique_ig=Instagram) -> int:
    tokens = charger_tokens()
    drive = drive or Drive(os.environ["GOOGLE_SERVICE_ACCOUNT"])
    racine = os.environ["DRIVE_DOSSIER_ID"].strip()
    lignes, besoin = [], cfg["posts_par_session"] * len(cfg["sessions"])

    for c in cfg["comptes"]:
        nom, problemes, infos = c["nom"], [], tokens.get(c["nom"])
        pseudo = "?"
        if not infos or not infos.get("token"):
            problemes.append("pas de token")
        else:
            try:
                _, pseudo = fabrique_ig(infos["token"], infos.get("hote", cfg["api"]["hote"]),
                                        cfg["api"]["version"], infos.get("user_id")).profil()
            except Exception as e:  # noqa: BLE001
                problemes.append(f"token refusé : {e}")
        stock = 0
        try:
            dossier = drive.sous_dossier(racine, c["dossier"])
            if dossier:
                stock = sum(1 for f in drive.contenu(dossier) if _est_video(f))
            else:
                problemes.append(f"dossier « {c['dossier']} » introuvable")
        except Exception as e:  # noqa: BLE001
            problemes.append(f"Drive : {e}")
        jours = stock / besoin if besoin else 0
        detail = f"@{pseudo} · {stock} vidéos (~{jours:.1f} j)"
        if problemes:
            detail += " · " + " ; ".join(problemes)
        elif stock < besoin:
            detail += " · ⚠️ stock faible"
        lignes.append(Resultat(nom, c["dossier"], "echec" if problemes else "ok", detail))

    ecrire_resume("Vérification", lignes, f"Drive partagé avec : `{drive.email}`")
    return 1 if any(r.statut == "echec" for r in lignes) else 0


def cmd_rafraichir(cfg: dict, sortie: str) -> int:
    tokens, erreurs = charger_tokens(), 0
    for nom, infos in tokens.items():
        if infos.get("hote", cfg["api"]["hote"]) != "graph.instagram.com":
            continue  # les tokens Facebook (System User) n'expirent pas
        try:
            infos["token"] = Instagram(infos["token"], "graph.instagram.com", cfg["api"]["version"]).rafraichir()
            print(f"✅ {nom} : token renouvelé")
        except Exception as e:  # noqa: BLE001
            erreurs += 1
            print(f"❌ {nom} : {e}")
    with open(sortie, "w", encoding="utf-8") as f:
        json.dump(tokens, f)
    return 1 if erreurs else 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="AutoPost Reels")
    p.add_argument("mode", choices=["publier", "simulation", "verifier", "rafraichir"])
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--sortie", help="fichier où écrire les tokens renouvelés (mode rafraichir)")
    a = p.parse_args()
    cfg = charger_config(a.config)
    if a.mode == "verifier":
        code = cmd_verifier(cfg)
    elif a.mode == "rafraichir":
        if not a.sortie:
            p.error("--sortie est obligatoire avec rafraichir")
        code = cmd_rafraichir(cfg, a.sortie)
    else:
        code = cmd_publier(cfg, simulation=(a.mode == "simulation"))
    sys.exit(code)


if __name__ == "__main__":
    main()
