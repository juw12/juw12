"""Keep the "Building" section of the profile README live.

For every bullet between the BUILDING markers, look up the latest commit of the
project's source (a whole repo, or a folder inside a monorepo such as
juw12/projects/apps/haru) and append a one-line activity status.

The order is hand-ranked (grouped under type headings, most solid first inside each) and is
never changed here: only the status line (between <!--live--> markers) is managed. New bullets are picked up
automatically as long as they contain a "Source" badge linking to GitHub.

Every status line is plain text (no emoji) and starts with two ways to read about
the project: "Landing page" (a marketing page from .github/landing.json, a copy of
https://juw12.github.io/p/landing.json) and "README" (the source's README).

Standard library only. Run locally with:  python .github/scripts/update_building.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

README = Path(__file__).resolve().parents[2] / "README.md"
START = "<!-- BUILDING:START -->"
END = "<!-- BUILDING:END -->"
CAPTION = (
    "<sub><!--caption-->Grouped by type; inside each group the most solid projects "
    "(tests, releases, a working demo) come first. Activity refreshes automatically every 2 hours.</sub>"
)

STATUS_RE = re.compile(r"<br><sub><!--live-->.*?<!--/live--></sub>")
SOURCE_RE = re.compile(r"\[!\[Source\]\([^)]*\)\]\((https://github\.com/[^)\s]+)\)")
API_ROOT = "https://api.github.com"
LANDING_FILE = Path(__file__).resolve().parents[1] / "landing.json"


def load_landings() -> dict[str, str]:
    """Normalized source URL -> landing page URL (empty if the manifest is missing)."""
    try:
        manifest = json.loads(LANDING_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {normalize(source): entry["landing"] for source, entry in manifest.items() if entry.get("landing")}


def normalize(url: str) -> str:
    return url.lower().rstrip("/")


def view_links(source_url: str | None, landings: dict[str, str]) -> str:
    """'Landing page · README' for a bullet, or '' when it has no source."""
    if not source_url:
        return ""
    links = []
    landing = landings.get(normalize(source_url))
    if landing:
        links.append(f"[Landing page]({landing})")
    links.append(f"[README]({source_url}#readme)")
    return " · ".join(links)


def api(path: str):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "juw12-profile-building"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(API_ROOT + path, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def parse_source(url: str) -> tuple[str, str, str | None, str]:
    """github.com/owner/repo[/tree/branch/sub/path] -> (owner, repo, branch, path)."""
    parts = urllib.parse.urlparse(url).path.strip("/").split("/")
    owner, repo = parts[0], parts[1]
    if len(parts) > 3 and parts[2] == "tree":
        return owner, repo, parts[3], "/".join(parts[4:])
    return owner, repo, None, ""


def fetch_activity(source: tuple[str, str, str | None, str], now: dt.datetime) -> dict | None:
    owner, repo, branch, path = source
    params = {"per_page": 1}
    if branch:
        params["sha"] = branch
    if path:
        params["path"] = path
    latest = api(f"/repos/{owner}/{repo}/commits?{urllib.parse.urlencode(params)}")
    if not latest:
        return None
    week_params = dict(params, per_page=100, since=(now - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    recent = api(f"/repos/{owner}/{repo}/commits?{urllib.parse.urlencode(week_params)}")
    commit = latest[0]
    return {
        "date": dt.datetime.fromisoformat(commit["commit"]["committer"]["date"].replace("Z", "+00:00")),
        "message": commit["commit"]["message"].splitlines()[0].strip(),
        "url": commit["html_url"],
        "week": len(recent),
    }


def status_line(info: dict | None, now: dt.datetime, links: str = "") -> str:
    parts = [links] if links else []
    if info:
        # Plain text only (no emoji): the date links to the latest commit.
        date = f"{info['date']:%b} {info['date'].day}"
        week = info["week"]
        if week == 0:
            week_text = "quiet this week"
        else:
            week_text = f"{'100+' if week >= 100 else week} commit{'' if week == 1 else 's'} this week"
        parts.append(f"Updated [{date}]({info['url']}) · {week_text}")
    if not parts:
        return ""
    return f"<br><sub><!--live-->{' · '.join(parts)}<!--/live--></sub>"


def update(readme_text: str, now: dt.datetime) -> str:
    if START not in readme_text or END not in readme_text:
        raise SystemExit(f"Markers {START} / {END} not found in README.md")
    head, rest = readme_text.split(START, 1)
    body, tail = rest.split(END, 1)

    # Keep every line in place (tier headings, blank lines, bullets); drop only the old caption.
    lines = [line for line in body.strip("\n").splitlines() if "<!--caption-->" not in line]
    while lines and not lines[0].strip():
        lines.pop(0)

    landings = load_landings()
    bullet_rows = []
    for index, line in enumerate(lines):
        if line.startswith("- "):
            bullet = STATUS_RE.sub("", line).rstrip()
            match = SOURCE_RE.search(bullet)
            source = parse_source(match.group(1)) if match else None
            link = view_links(match.group(1) if match else None, landings)
            bullet_rows.append((index, bullet, source, link))

    unique_sources = sorted({source for _, _, source, _ in bullet_rows if source}, key=str)
    # Look up all sources in parallel; any failure propagates and aborts the whole update.
    with ThreadPoolExecutor(max_workers=8) as pool:
        activity = dict(zip(unique_sources, pool.map(lambda source: fetch_activity(source, now), unique_sources)))

    for index, bullet, source, link in bullet_rows:
        info = activity.get(source) if source else None
        lines[index] = bullet + status_line(info, now, link)

    section = "\n".join([START, CAPTION, "", *lines, END])
    return head + section + tail


def main() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    original = README.read_text(encoding="utf-8")
    try:
        updated = update(original, now)
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as error:
        # All-or-nothing: never write a half-updated section.
        print(f"Skipping update, GitHub lookup failed: {error}", file=sys.stderr)
        return
    if updated != original:
        README.write_text(updated, encoding="utf-8", newline="\n")
        print("README.md updated")
    else:
        print("No changes")


if __name__ == "__main__":
    main()
