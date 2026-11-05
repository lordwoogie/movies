#!/usr/bin/env python3
"""Turn a public Letterboxd RSS diary feed into letterboxd.json for the vault app.

Usage:
  letterboxd_sync.py <username> <out.json>         fetch the live feed
  letterboxd_sync.py --from-file <rss> <out.json>  parse a saved feed (tests)
"""
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

LB = '{https://letterboxd.com}'
TMDB = '{https://themoviedb.org}'
STARS = {'★': 1.0, '½': 0.5}


def stars_to_number(text):
    """'★★★½' -> 3.5. Returns None when the title carries no rating."""
    total = sum(STARS.get(ch, 0) for ch in text)
    return total if total else None


def text_of(item, *paths):
    for p in paths:
        el = item.find(p)
        if el is not None and el.text and el.text.strip():
            return el.text.strip()
    return None


def parse(xml_bytes):
    root = ET.fromstring(xml_bytes)
    entries = []
    for item in root.iter('item'):
        title = text_of(item, LB + 'filmTitle')
        raw_title = text_of(item, 'title') or ''
        year = text_of(item, LB + 'filmYear')
        rating = text_of(item, LB + 'memberRating')

        # Fall back to the human title: "Film Name, 2024 - ★★★★"
        if not title:
            m = re.match(r'^(.*?),\s*(\d{4})\s*(?:-\s*(.*))?$', raw_title)
            if not m:
                continue          # lists and other non-diary items
            title, year = m.group(1).strip(), m.group(2)
            if rating is None and m.group(3):
                r = stars_to_number(m.group(3))
                rating = str(r) if r is not None else None
        if rating is None and ' - ' in raw_title:
            r = stars_to_number(raw_title.rsplit(' - ', 1)[1])
            rating = str(r) if r is not None else None
        if not title:
            continue

        try:
            year_n = int(year) if year else None
        except ValueError:
            year_n = None
        try:
            rating_n = float(rating) if rating else None
        except ValueError:
            rating_n = None

        entries.append({
            'title': title,
            'year': year_n,
            'rating': rating_n,
            'watched': text_of(item, LB + 'watchedDate'),
            'rewatch': (text_of(item, LB + 'rewatch') or '').lower() == 'yes',
            'uri': text_of(item, 'link'),
            'tmdbId': text_of(item, TMDB + 'movieId'),
        })
    return entries


def main():
    args = sys.argv[1:]
    if len(args) == 3 and args[0] == '--from-file':
        xml_bytes = open(args[1], 'rb').read()
        out = args[2]
    elif len(args) == 2:
        url = 'https://letterboxd.com/%s/rss/' % args[0]
        req = urllib.request.Request(url, headers={'User-Agent': 'movie-vault-sync'})
        with urllib.request.urlopen(req, timeout=45) as r:
            xml_bytes = r.read()
        out = args[1]
        # Printed so the feed's real shape is visible in the run log.
        head = xml_bytes.decode('utf-8', 'replace')
        start = head.find('<item>')
        print('--- first raw item ---')
        print(head[start:start + 1200] if start >= 0 else head[:1200])
        print('--- end sample ---')
    else:
        sys.exit(__doc__)

    entries = parse(xml_bytes)
    print('parsed %d diary entries' % len(entries))
    if not entries:
        sys.exit('no entries parsed - feed shape may have changed')
    for e in entries[:3]:
        print('  ', e['title'], e['year'], e['rating'], e['watched'])

    # Keep the old timestamp when nothing changed, so a quiet day makes no commit.
    updated = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        prev = json.load(open(out))
        if prev.get('entries') == entries:
            updated = prev.get('updated', updated)
            print('no new diary entries')
    except (OSError, ValueError):
        pass

    json.dump({
        'updated': updated,
        'count': len(entries),
        'entries': entries,
    }, open(out, 'w'), indent=1, ensure_ascii=False)
    print('wrote', out)


if __name__ == '__main__':
    main()
