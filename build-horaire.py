#!/usr/bin/env python3
"""
Filtre le GTFS Static du TEC -> horaire.json (petit fichier lu par le Worker du site).

Usage :   py build-horaire.py gtfs-tec.zip [horaire.json]     (Windows)
          python3 build-horaire.py gtfs-tec.zip [horaire.json] (Linux / GitHub Actions)

On ne garde que les trajets qui :
  - passent par un de nos 3 arrêts de départ (HELHA, Ferme, Chemin d'Obourg),
  - ET passent ENSUITE par un arrêt de la gare de Mons (identifiant commençant par H1ms401).

Les trip_id changent d'une version du GTFS Static à l'autre : le fichier doit être régénéré
à chaque nouvelle version (le workflow GitHub le fait chaque nuit).
"""
import csv, io, json, os, re, sys, zipfile
from collections import defaultdict

# Base de l'identifiant SANS la lettre du quai : un même arrêt a plusieurs quais (a, b, c...) selon la direction / la ligne.
# Le sens "vers la gare" est de toute façon vérifié plus bas (un arrêt de la gare doit venir APRÈS dans le trajet).
STOPS = [("Mons HELHA", "H1ms367"), ("Mons Ferme", "H1ms279"), ("Mons Chemin d'Obourg", "H1ms266")]
GARE = ["H1ms401"]          # préfixe : couvre les quais a à f


def rows(z, name):
    try:
        f = z.open(name)
    except KeyError:
        return iter(())
    return csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))


def secs(t):
    """'08:27:00' (peut dépasser 24:00:00) -> secondes ; None si vide."""
    t = (t or "").strip()
    if not t:
        return None
    h, m, *s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + (int(s[0]) if s else 0)


def norm(x):
    """'gs:tec:H1ms401a' -> 'h1ms401a' (le GTFS-RT préfixe les identifiants)."""
    return x.strip().rsplit(":", 1)[-1].lower()


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src, out = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "horaire.json")
    z = zipfile.ZipFile(src)

    # 1) arrêts concernés (par identifiant, sans préfixe)
    own, gare = {}, set()
    for r in rows(z, "stops.txt"):
        sid = norm(r["stop_id"])
        for i, (_, code) in enumerate(STOPS):
            if re.fullmatch(re.escape(code.lower()) + r"[a-z]?", sid):      # tous les quais de l'arrêt
                own[r["stop_id"]] = i
        if any(sid.startswith(g.lower()) for g in GARE):
            gare.add(r["stop_id"])
    print(f"quais de départ trouvés : {len(own)}   quais gare : {len(gare)}")
    for sid, i in sorted(own.items(), key=lambda x: (x[1], x[0])):
        print(f"   {STOPS[i][0]:24s} {sid}")
    found = {i for i in own.values()}
    if len(found) < 3 or not gare:
        print("ATTENTION : arrêt introuvable dans stops.txt, vérifie les identifiants.")

    # 2) stop_times (plusieurs centaines de Mo) : on ne retient que nos arrêts + le premier arrêt de chaque trajet
    mine = defaultdict(list)       # trip -> [(seq, kind, idx, arr, dep)]
    first = {}                     # trip -> (seq, dep)
    rd = csv.reader(io.TextIOWrapper(z.open("stop_times.txt"), encoding="utf-8-sig", newline=""))
    ix = {k: n for n, k in enumerate(next(rd))}
    iT, iS, iQ = ix["trip_id"], ix["stop_id"], ix["stop_sequence"]
    iA, iD = ix.get("arrival_time"), ix.get("departure_time")
    for r in rd:
        sid = r[iS]
        t, seq = r[iT], int(r[iQ])
        arr = secs(r[iA]) if iA is not None else None
        dep = secs(r[iD]) if iD is not None else None
        if dep is None:
            dep = arr
        if t not in first or seq < first[t][0]:
            first[t] = (seq, dep)
        if sid in own:
            mine[t].append((seq, "o", own[sid], arr, dep))
        elif sid in gare:
            mine[t].append((seq, "g", 0, arr, dep))

    # 3) garder les trajets qui vont VERS la gare
    keep = {}
    for t, lst in mine.items():
        lst.sort()
        p, g = [], None
        for k, (seq, kind, idx, arr, dep) in enumerate(lst):
            if kind != "o" or dep is None:
                continue
            nxt = next((x for x in lst[k + 1:] if x[1] == "g"), None)
            if nxt:
                p.append([idx, dep])
                g = g if g is not None else (nxt[3] if nxt[3] is not None else nxt[4])
        if p:
            seen, uniq = set(), []
            for a in p:                       # un trajet qui touche 2 quais du même arrêt : on garde le premier passage
                if a[0] not in seen:
                    seen.add(a[0]); uniq.append(a)
            keep[t] = (uniq, g)
    print(f"trajets vers la gare : {len(keep)}")

    # 4) trips + routes
    routes = {r["route_id"]: (r.get("route_short_name") or r["route_id"]) for r in rows(z, "routes.txt")}
    trips, used_svc = [], set()
    for r in rows(z, "trips.txt"):
        t = r["trip_id"]
        if t not in keep:
            continue
        p, g = keep[t]
        b = first[t][1]
        trips.append({"i": t.rsplit(":", 1)[-1], "l": routes.get(r["route_id"], "?"),
                      "s": r["service_id"], "b": b if b is not None else p[0][1], "g": g, "p": p})
        used_svc.add(r["service_id"])

    # 5) calendrier
    svc = {}
    for r in rows(z, "calendar.txt"):
        if r["service_id"] in used_svc:
            svc[r["service_id"]] = {"d": [int(r[d]) for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")],
                                    "a": r["start_date"], "z": r["end_date"]}
    for r in rows(z, "calendar_dates.txt"):
        s = r["service_id"]
        if s not in used_svc:
            continue
        e = svc.setdefault(s, {})
        e.setdefault("+" if r["exception_type"] == "1" else "-", []).append(r["date"])

    ends = [v["z"] for v in svc.values() if "z" in v] + [d for v in svc.values() for d in v.get("+", [])]
    starts = [v["a"] for v in svc.values() if "a" in v]
    ids = defaultdict(set)
    for sid, i in own.items():
        ids[STOPS[i][0]].add(norm(sid))
    lignes = defaultdict(set)
    for t in trips:
        for idx, _ in t["p"]:
            lignes[STOPS[idx][0]].add(t["l"])
    for n, _ in STOPS:
        print(f"lignes vers la gare à {n} : {sorted(lignes[n]) or 'AUCUNE'}")
    doc = {"v": 1, "ids": {k: sorted(v) for k, v in ids.items()}, "debut": min(starts) if starts else None, "fin": max(ends) if ends else None,
           "stops": [n for n, _ in STOPS], "svc": svc, "trips": trips}
    if not trips:
        sys.exit("ERREUR : aucun trajet vers la gare trouvé, horaire.json NON écrit (identifiants à vérifier).")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{out} : {len(trips)} trajets, {len(svc)} services, valable {doc['debut']} -> {doc['fin']}")


if __name__ == "__main__":
    main()
