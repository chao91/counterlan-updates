"""Daily link check for counterlan.pages.dev and dota2mobilelan.pages.dev (owner 2026-11-27: GitHub Actions, reports to
the hidden staff channel). No AI, no tokens: plain HTTP checks.

- Reads each live site and takes its download links from the data-dl buttons, so a new build needs no change here.
- MEGA file links: MEGA's public link info (the same call its own web page makes) must return the file size.
- GitHub release links: the release must exist and have assets.
- GoFile: not checked. GoFile has no public way to ask about a link, so it is listed as "not checked".
- Posts to Discord only when something is broken, plus a weekly summary on Mondays (UTC) with GitHub download counts,
  or on a manual run with FORCE_REPORT=true. No pings. Exit 1 when a link is broken, so the Actions run shows red too.

  DISCORD_WEBHOOK=... python3 link_watchdog.py      # what the workflow runs
  python3 link_watchdog.py --dry                    # print the message, post nothing
"""
import datetime
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

SITES = [('CounterLAN', 'https://counterlan.pages.dev/'), ('Dota 2 Mobile LAN', 'https://dota2mobilelan.pages.dev/')]
UA = 'counterlan-link-watchdog/1.0 (+https://counterlan.pages.dev)'
TIMEOUT = 30


def get(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={'User-Agent': UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, r.read()


def download_links(page):
    """{kind: set(urls)} from <a ... data-dl="kind" href="..."> in any attribute order."""
    out = {}
    for tag in re.findall(r'<a\b[^>]*>', page):
        kind = re.search(r'\bdata-dl="([^"]+)"', tag)
        href = re.search(r'\bhref="([^"]+)"', tag)
        if kind and href:
            out.setdefault(kind.group(1), set()).add(html.unescape(href.group(1)))
    return out


def mega_handle(url):
    m = re.match(r'https://mega\.(?:nz|co\.nz|io)/file/([A-Za-z0-9_-]{8})#', url) or \
        re.match(r'https://mega\.(?:nz|co\.nz|io)/#!([A-Za-z0-9_-]{8})!', url)
    return m.group(1) if m else None


def check_mega(url):
    h = mega_handle(url)
    if not h:
        return None, 'not a MEGA file link (folder links are not checked)'
    for attempt in range(2):  # -3 (try again) and -2 are MEGA hiccups, not a dead link: retry once
        _, body = get('https://g.api.mega.co.nz/cs?id=1', json.dumps([{'a': 'g', 'p': h}]).encode(), {'Content-Type': 'application/json'})
        res = json.loads(body)
        item = res[0] if isinstance(res, list) and res else res
        if item not in (-3, -2) or attempt:
            break
        time.sleep(10)
    if isinstance(item, dict) and item.get('s'):
        return True, f'{item["s"] / 1e9:.2f} GB'
    if item == -9:
        return False, 'MEGA says the file does not exist (-9)'
    return None, f'MEGA answered {item}, could not tell (not counted as broken)'


def gh_api(path):
    headers = {'Accept': 'application/vnd.github+json'}
    if os.environ.get('GITHUB_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']
    _, body = get('https://api.github.com' + path, headers=headers)
    return json.loads(body)


def check_github(url):
    m = re.match(r'https://github\.com/([^/]+/[^/]+)/releases(?:/tag/([^/?#]+))?/?$', url)
    if not m:
        return None, 'not a GitHub releases link'
    repo, tag = m.groups()
    rel = gh_api(f'/repos/{repo}/releases/tags/{tag}') if tag else gh_api(f'/repos/{repo}/releases/latest')
    names = [a['name'] for a in rel.get('assets', [])]
    if not names:
        return False, f'release {rel.get("tag_name")} has no files'
    return True, f'{rel.get("tag_name")}: {", ".join(names)}'


def github_counts(repo):
    rels = gh_api(f'/repos/{repo}/releases?per_page=100')
    def apks(r):  # only the app: the updater also fetches manifest files on every check
        return sum(a['download_count'] for a in r.get('assets', []) if a['name'].lower().endswith('.apk'))
    latest = rels[0] if rels else {}
    return sum(apks(r) for r in rels), latest.get('tag_name'), apks(latest)


def run():
    lines, broken, repos = [], [], set()
    for name, site in SITES:
        try:
            status, body = get(site)
            page = body.decode('utf-8', 'replace')
        except (urllib.error.URLError, OSError) as e:
            broken.append(f'{name}: the website does not open ({e})')
            continue
        links = download_links(page)
        if not links:
            broken.append(f'{name}: no download buttons found on the page')
        for kind, urls in sorted(links.items()):
            for url in sorted(urls):
                try:
                    if 'mega.' in url:
                        ok, why = check_mega(url)
                    elif 'github.com' in url:
                        ok, why = check_github(url)
                        if ok:
                            repos.add('/'.join(url.split('/')[3:5]))
                    elif 'gofile.io' in url:
                        ok, why = None, 'GoFile is not checked automatically'
                    else:
                        ok, why = None, 'unknown host, not checked'
                except urllib.error.HTTPError as e:   # 404 = the release is gone; other HTTP answers are hiccups/limits
                    ok, why = (False, 'HTTP 404, not found') if e.code == 404 else (None, f'HTTP {e.code}, could not tell (not counted as broken)')
                except (urllib.error.URLError, OSError, ValueError, KeyError) as e:   # timeout, DNS, bad JSON: not proof of a dead link
                    ok, why = None, f'check failed ({e}), not counted as broken'
                short = f'MEGA file {mega_handle(url)}' if mega_handle(url) else url.split('#')[0]
                mark = {True: 'OK', False: 'BROKEN', None: 'not checked'}[ok]
                lines.append(f'{name} {kind}: {mark} ({why}) {short}')
                if ok is False:
                    broken.append(f'{name} {kind}: {why} {short}')
    counts = []
    for repo in sorted(repos):
        try:
            total, tag, latest = github_counts(repo)
            counts.append(f'{repo}: {total} app downloads in all, {latest} for {tag} (GitHub; new installs and in-game updates)')
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            counts.append(f'{repo}: download count not available ({e})')
    return lines, broken, counts


def post(text):
    hook = os.environ.get('DISCORD_WEBHOOK')
    if not hook:
        raise SystemExit('DISCORD_WEBHOOK is not set')
    body = json.dumps({'content': text[:1900], 'allowed_mentions': {'parse': []}}).encode()
    get(hook, body, {'Content-Type': 'application/json'})


def main():
    dry = '--dry' in sys.argv
    today = datetime.datetime.now(datetime.timezone.utc)
    lines, broken, counts = run()
    weekly = today.weekday() == 0 or os.environ.get('FORCE_REPORT', '').lower() == 'true'
    if broken:
        text = (f'⚠️ **Link check {today:%Y-%m-%d}: {len(broken)} problem(s)**\n' + '\n'.join(f'• {b}' for b in broken)
                + '\n\nAll checks:\n' + '\n'.join(f'• {l}' for l in lines)
                + ('\n\n' + '\n'.join(f'📥 {c}' for c in counts) if weekly and counts else ''))
    elif weekly:
        text = (f'✅ **Weekly link check {today:%Y-%m-%d}: all checked links work.**\n' + '\n'.join(f'• {l}' for l in lines)
                + ('\n\n' + '\n'.join(f'📥 {c}' for c in counts) if counts else ''))
    else:
        text = None
    print('\n'.join(lines + counts))
    if text:
        print('--- message ---\n' + text)
        if not dry:
            post(text)
    return 1 if broken else 0


if __name__ == '__main__':
    sys.exit(main())
