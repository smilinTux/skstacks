#!/usr/bin/env python3
"""skfetch post-deploy wiring: connect Sonarr/Radarr/Lidarr/Prowlarr through
their HTTP APIs. Idempotent: every object is looked up by name (or path) first
and only created when missing, so a rerun changes nothing.

Usage: wire.py <wire.json>   (rendered by the deploy, mode 0600)

Exit 0 when everything is in place. A Plex notification that the app refuses
(Lidarr refuses one until Plex has a Music library) is a warning only. An
API that never answers, or any other refused create, exits 1.
"""
import json
import sys
import time
import urllib.error
import urllib.request

PLEX_EVENTS = ("onDownload", "onUpgrade", "onRename", "onReleaseImport", "onImportComplete")


class ApiError(Exception):
    pass


class Api:
    def __init__(self, name, url, key, ver):
        self.name, self.base, self.key, self.ver = name, url.rstrip("/"), key, ver

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"{self.base}/api/{self.ver}/{path}", data=data, method=method,
                                     headers={"X-Api-Key": self.key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise ApiError(f"{self.name}: {method} {path}: HTTP {exc.code}: {exc.read()[:300]!r}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise ApiError(f"{self.name}: {method} {path}: {exc}") from None
        return json.loads(raw) if raw else None

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body):
        return self.call("POST", path + "?forceSave=true", body)

    def wait(self, tries, delay):
        for i in range(tries):
            try:
                self.get("system/status")
                return
            except ApiError as exc:
                last = exc
                if i + 1 < tries:
                    time.sleep(delay)
        raise ApiError(f"{self.name} API never answered: {last}")


def set_fields(item, values):
    for f in item.get("fields", []):
        if f["name"] in values:
            f["value"] = values[f["name"]]
    return item


def from_schema(api, kind, impl):
    for item in api.get(f"{kind}/schema"):
        if item.get("implementation") == impl:
            return item
    raise ApiError(f"{api.name}: no {impl} in {kind}/schema")


class Wirer:
    def __init__(self):
        self.errors = 0

    def ensure(self, api, kind, name, build, match=None, warn_only=False):
        match = match or (lambda e: e.get("name") == name)
        try:
            if any(match(e) for e in api.get(kind)):
                print(f"exists  {api.name} {kind} {name}")
                return None
            obj = api.post(kind, build())
            print(f"created {api.name} {kind} {name}")
            return obj
        except ApiError as exc:
            if warn_only:
                print(f"WARN    {exc}")
            else:
                self.errors += 1
                print(f"ERROR   {exc}")
            return None

    def app(self, name, cfg, top):
        api = Api(name, cfg["url"], cfg["key"], cfg["api"])

        def root():
            body = {"path": cfg["root"]}
            if name == "lidarr":
                body.update(name="Music", defaultQualityProfileId=api.get("qualityprofile")[0]["id"],
                            defaultMetadataProfileId=api.get("metadataprofile")[0]["id"],
                            defaultMonitorOption="all", defaultNewItemMonitorOption="all", defaultTags=[])
            return body

        self.ensure(api, "rootfolder", cfg["root"], root, match=lambda e: e.get("path", "").rstrip("/") == cfg["root"])

        def client():
            item = from_schema(api, "downloadclient", "QBittorrent")
            set_fields(item, {"host": top["qbittorrent"]["host"], "port": top["qbittorrent"]["port"],
                              cfg["category_field"]: cfg["category"]})
            item.update(name="qBittorrent", enable=True, removeCompletedDownloads=True, removeFailedDownloads=True)
            return item

        self.ensure(api, "downloadclient", "qBittorrent", client)

        plex = top.get("plex")
        if plex:
            def notification():
                item = from_schema(api, "notification", "PlexServer")
                set_fields(item, {"host": plex["host"], "port": plex["port"], "authToken": plex["token"],
                                  "updateLibrary": True, "mapFrom": plex["map_from"], "mapTo": plex["map_to"]})
                for ev in PLEX_EVENTS:
                    if item.get("supports" + ev[0].upper() + ev[1:], ev in item):
                        item[ev] = True
                item["name"] = "Plex"
                return item

            self.ensure(api, "notification", "Plex", notification, warn_only=True)

    def prowlarr(self, top):
        cfg = top["prowlarr"]
        api = Api("prowlarr", cfg["url"], cfg["key"], "v1")
        for name, app in top["apps"].items():
            impl = name.capitalize()

            def application(impl=impl, app=app):
                item = from_schema(api, "applications", impl)
                set_fields(item, {"prowlarrUrl": cfg["bridge_url"], "baseUrl": app["bridge_url"], "apiKey": app["key"]})
                item.update(name=impl, syncLevel="fullSync")
                return item

            self.ensure(api, "applications", impl, application)

        tag_id = None
        if top.get("flaresolverr"):
            try:
                tags = [t for t in api.get("tag") if t.get("label") == "flaresolverr"]
                if tags:
                    tag_id = tags[0]["id"]
                else:
                    tag_id = api.post("tag", {"label": "flaresolverr"})["id"]
                    print("created prowlarr tag flaresolverr")
            except ApiError as exc:
                self.errors += 1
                print(f"ERROR   {exc}")

            def proxy():
                item = from_schema(api, "indexerProxy", "FlareSolverr")
                set_fields(item, {"host": top["flaresolverr"]["url"]})
                item.update(name="FlareSolverr", tags=[tag_id])
                return item

            if tag_id is not None:
                self.ensure(api, "indexerProxy", "FlareSolverr", proxy)

        wanted = [(n, []) for n in top.get("indexers", [])]
        wanted += [(n, [tag_id] if tag_id is not None else []) for n in top.get("indexers_flaresolverr", [])]
        if not wanted:
            return
        schema = api.get("indexer/schema")
        profile = api.get("appprofile")[0]["id"]
        for n, tags in wanted:
            low = n.lower()
            hit = [s for s in schema if low in (str(s.get("definitionName", "")).lower(), str(s.get("name", "")).lower())]
            if not hit:
                print(f"WARN    prowlarr has no indexer definition {n!r}; skipped")
                continue
            item = hit[0]

            def indexer(item=item, tags=tags):
                item.update(enable=True, appProfileId=profile, tags=tags)
                return item

            key = str(item.get("definitionName") or item["name"]).lower()
            self.ensure(api, "indexer", item["name"], indexer,
                        match=lambda e, key=key: str(e.get("definitionName") or e.get("name")).lower() == key)


def main(argv):
    top = json.load(open(argv[1]))
    tries, delay = top["wait"]["tries"], top["wait"]["delay"]
    apis = [Api(n, a["url"], a["key"], a["api"]) for n, a in top["apps"].items()]
    apis.append(Api("prowlarr", top["prowlarr"]["url"], top["prowlarr"]["key"], "v1"))
    try:
        for api in apis:
            api.wait(tries, delay)
    except ApiError as exc:
        print(f"ERROR   {exc}")
        return 1
    w = Wirer()
    for name, cfg in top["apps"].items():
        w.app(name, cfg, top)
    w.prowlarr(top)
    return 1 if w.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
