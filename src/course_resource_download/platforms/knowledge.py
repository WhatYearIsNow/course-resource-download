"""Read knowledge-theme metadata without visiting learning/player routes."""

from urllib.parse import urlsplit

from ..common import safe_error

INIT_BRIDGE = r"""async () => {
    // Discover the versioned public frontend module rather than pinning its hash.
    const scripts = [...document.querySelectorAll('script[src]')]
        .map(s => new URL(s.src, location.href))
        .filter(u => u.origin === location.origin && /\/assets\/index-.*\.js$/.test(u.pathname));
    let path;
    for (const script of scripts) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 30000);
        let response, text;
        try {
            response = await fetch(script.href, {signal:controller.signal});
            if (response.ok) text = await response.text();
        } finally { clearTimeout(timer); }
        if (!response.ok) continue;
        const match = text.match(/(?:assets\/)?(courseUrl-[A-Za-z0-9_-]+\.js)/);
        if (match) { path = '/assets/' + match[1]; break; }
    }
    if (!path) throw new Error('Knowledge metadata module not found');
    const module = await import(path);
    const api = Object.values(module).find(v => v &&
        typeof v.courseKnowledgeDic === 'function' &&
        typeof v.getListNodeResourcesWithStatus === 'function');
    if (!api) throw new Error('Knowledge metadata methods unavailable');
    window.__courseResourceDownloadMetadata = async (method, params) => {
        let timer;
        try {
            return await Promise.race([
                api[method](params),
                new Promise((_, reject) => { timer = setTimeout(() =>
                    reject(new Error('Knowledge metadata request timed out')), 30000); })
            ]);
        } finally { clearTimeout(timer); }
    };
    return true;
}"""

QUERY_CATALOG = """async params => {
    const response = await window.__courseResourceDownloadMetadata('courseKnowledgeDic', params);
    if (![0, 200].includes(Number(response?.code))) throw new Error('Knowledge catalog request failed');
    // Return identifiers/names only; never persist raw course responses or progress.
    function group(items) {
        if (!Array.isArray(items)) throw new Error('Malformed knowledge theme list');
        return items.map(t => ({name:t.themeName, children:group(t.subThemeList || []),
            nodes:(t.knowledgeList || []).map(n => ({knowledgeId:n.knowledgeId,
                knowledgeName:n.knowledgeName}))}));
    }
    return group(response.data?.themeList);
}"""

QUERY_RESOURCES = """async params => {
    const response = await window.__courseResourceDownloadMetadata('getListNodeResourcesWithStatus', params);
    if (![0, 200].includes(Number(response?.code))) throw new Error('Knowledge resources request failed');
    if (!Array.isArray(response.data?.resourceList)) throw new Error('Malformed knowledge resource list');
    return response.data.resourceList.map(row => {
        const r = row?.resourcesDetail;
        if (!r || typeof r !== 'object') return {malformed:true};
        return {name:r.resourcesName, url:r.resourcesUrl, type:r.resourcesDataType,
            resourceId:r.resourcesUid, canDownload:r.canDownload,
            downloadAllowed:r.downloadAllowed, isVideo:!!row.resourcesVideoDetail};
    });
}"""


def validate_cdp_endpoint(value):
    """Attaching to a browser grants access to that session; restrict to loopback."""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https", "ws", "wss"}
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("CDP endpoint must be a loopback HTTP(S) or WS(S) URL")
    return value


def flatten_catalog(groups, prefix=""):
    if not isinstance(groups, list):
        raise ValueError("Malformed knowledge catalog")
    nodes = []
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("Malformed knowledge theme")
        chapter = "/".join(x for x in [prefix, str(group.get("name") or "")] if x)
        records = group.get("nodes", [])
        if not isinstance(records, list):
            raise ValueError("Malformed knowledge nodes")
        for node in records:
            if not isinstance(node, dict) or not node.get("knowledgeId"):
                raise ValueError("Knowledge node identity is missing")
            nodes.append({**node, "chapter": chapter})
        nodes.extend(flatten_catalog(group.get("children", []), chapter))
    seen = set()
    for node in nodes:
        key = str(node["knowledgeId"])
        if key in seen:
            raise ValueError("Duplicate knowledge node identity")
        seen.add(key)
    return nodes


def harvest_knowledge(page, args, resources, records, checkpoint=None):
    """Each node gets a terminal traversal record, including empty/error nodes."""
    page.evaluate(INIT_BRIDGE)
    params = {"courseId": args.course_id, "classId": args.class_id}
    catalog = page.evaluate(QUERY_CATALOG, params)
    nodes = flatten_catalog(catalog)
    for node in nodes:
        record = {
            "task": node.get("knowledgeName"),
            "knowledgeId": node["knowledgeId"],
            "chapter": node["chapter"],
            "expectedActivities": 1,
            "visitedActivities": 0,
            "status": "pending",
            "reason": None,
        }
        records.append(record)
    for node, record in zip(nodes, records[-len(nodes) :] if nodes else []):
        try:
            rows = page.evaluate(QUERY_RESOURCES, {**params, "knowledgeId": node["knowledgeId"]})
            if not isinstance(rows, list):
                raise ValueError("Malformed knowledge resources")
            for row in rows:
                if not isinstance(row, dict) or row.get("malformed"):
                    resources.append(
                        {
                            "source": "knowledge",
                            "knowledge": record["task"],
                            "type": None,
                            "url": None,
                            "error": "Malformed resource",
                        }
                    )
                    continue
                row = dict(row)
                is_video = row.pop("isVideo", False)
                if str(row.get("type")) == "22" and is_video:
                    # This URL is a shared cover image, not a downloadable video.
                    row["url"] = None
                denied = row.get("canDownload") is False or row.get("downloadAllowed") is False
                resources.append(
                    {
                        **row,
                        "source": "knowledge",
                        "knowledge": record["task"],
                        "knowledgeId": node["knowledgeId"],
                        "chapter": node["chapter"],
                        "permissionDenied": denied,
                    }
                )
            record.update(status="ok", visitedActivities=1, resourceCount=len(rows))
        except Exception as exc:
            record.update(status="failed", reason=safe_error(exc))
        finally:
            if checkpoint:
                checkpoint()
    print(f"Knowledge nodes visited: {sum(r['status'] == 'ok' for r in records)}/{len(nodes)}")
