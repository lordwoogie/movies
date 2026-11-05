#!/usr/bin/env python3
"""Weekly streaming picks for the Movie Vault.

TMDB says what is on the household's services; Claude keeps the newsworthy
films and explains each choice. Writes suggestions.json for the site.

Needs TMDB_TOKEN and ANTHROPIC_API_KEY in the environment.
  suggest.py             full run
  suggest.py --dry-run   gather candidates only, no Claude call
"""
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = json.load(open(os.path.join(ROOT, 'data', 'suggest_config.json')))
OUT = os.path.join(ROOT, 'suggestions.json')
REGION = CONFIG['region']
TMDB = os.environ.get('TMDB_BASE', 'https://api.themoviedb.org/3')
MODEL = 'claude-opus-5-5'
BRACKETS = ['4-8', '8-12', '12-16', '16-18']


def norm(title):
    return re.sub(r'[^a-z0-9]', '', re.sub(r'^the\s+', '', str(title or '').lower()))


# ---------------------------------------------------------------- TMDB

def tmdb(path, params=None):
    query = urllib.parse.urlencode(params or {})
    req = urllib.request.Request(
        TMDB + path + ('?' + query if query else ''),
        headers={'Authorization': 'Bearer ' + os.environ['TMDB_TOKEN'], 'Accept': 'application/json'})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise


def resolve_providers():
    """Map the configured services onto TMDB provider ids for the region."""
    wanted = {n.lower(): label for label, names in CONFIG['services'].items() for n in names}
    label_of = {}
    for p in tmdb('/watch/providers/movie', {'watch_region': REGION}).get('results', []):
        label = wanted.get(p['provider_name'].strip().lower())
        if label:
            label_of[p['provider_id']] = label
    missing = sorted(set(CONFIG['services']) - set(label_of.values()))
    if missing:
        print('warning: no TMDB provider matched', missing)
    print('providers:', {pid: label for pid, label in sorted(label_of.items())})
    return label_of


def discover(params, pages):
    found = []
    for page in range(1, pages + 1):
        data = tmdb('/discover/movie', dict(params, page=page))
        found += data.get('results', [])
        if page >= data.get('total_pages', 0):
            break
    return found


def candidate_pools(label_of, today):
    base = {
        'watch_region': REGION,
        'with_watch_providers': '|'.join(str(p) for p in sorted(label_of)),
        'with_watch_monetization_types': 'flatrate|free|ads',
        'include_adult': 'false',
        'language': 'en-US',
    }
    return {
        'new': discover(dict(base, **{
            'primary_release_date.gte': (today - timedelta(days=400)).isoformat(),
            'sort_by': 'popularity.desc', 'vote_count.gte': 20}), 5),
        'kids': discover(dict(base, **{
            'with_genres': '16|10751', 'certification_country': 'US', 'certification.lte': 'PG',
            'primary_release_date.gte': (today - timedelta(days=730)).isoformat(),
            'sort_by': 'popularity.desc', 'vote_count.gte': 10}), 3),
        'archive': discover(dict(base, **{
            'primary_release_date.lte': '%d-12-31' % (today.year - 15),
            'sort_by': 'vote_average.desc', 'vote_count.gte': 400, 'vote_average.gte': 7.2}), 5),
    }


def film_details(movie_id, label_of):
    d = tmdb('/movie/%d' % movie_id, {'append_to_response': 'credits,release_dates,watch/providers'})
    cert = ''
    for country in d.get('release_dates', {}).get('results', []):
        if country.get('iso_3166_1') == 'US':
            cert = next((r['certification'] for r in country.get('release_dates', []) if r.get('certification')), '')
    offers = d.get('watch/providers', {}).get('results', {}).get(REGION, {})
    services = sorted({label_of[p['provider_id']]
                       for kind in ('flatrate', 'free', 'ads')
                       for p in offers.get(kind, []) if p['provider_id'] in label_of})
    released = d.get('release_date') or ''
    return {
        'tmdbId': d['id'],
        'title': d.get('title') or '',
        'year': int(released[:4]) if released[:4].isdigit() else None,
        'runtime': d.get('runtime') or None,
        'genres': [g['name'] for g in d.get('genres', [])],
        'directors': [c['name'] for c in d.get('credits', {}).get('crew', []) if c.get('job') == 'Director'][:2],
        'studios': [c['name'] for c in d.get('production_companies', [])][:3],
        'cert': cert,
        'tmdbScore': round(d.get('vote_average') or 0, 1),
        'votes': d.get('vote_count') or 0,
        'overview': (d.get('overview') or '')[:280],
        'poster': d.get('poster_path'),
        'services': services,
    }


# ---------------------------------------------------------------- taste

def load_taste():
    """Ratings keyed by (title, year), oldest source first so newer ones win."""
    ratings = {}

    def put(title, year, rating, when=None):
        try:
            year = int(year) if year not in (None, '') else None
        except ValueError:
            year = None
        try:
            rating = float(rating) if rating not in (None, '') else None
        except ValueError:
            rating = None
        ratings[(norm(title), year)] = {'title': title, 'year': year, 'rating': rating, 'when': when}

    for r in json.load(open(os.path.join(ROOT, 'data', 'taste_seed.json')))['ratings']:
        put(r['title'], r['year'], r['rating'])
    export = os.path.join(ROOT, 'data', 'ratings.csv')     # Letterboxd export, if present
    if os.path.exists(export):
        with open(export, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                put(row.get('Name'), row.get('Year'), row.get('Rating'), row.get('Date'))
    diary_path = os.path.join(ROOT, 'letterboxd.json')
    diary = json.load(open(diary_path)).get('entries', []) if os.path.exists(diary_path) else []
    for e in reversed(diary):                              # feed is newest first
        put(e['title'], e.get('year'), e.get('rating'), e.get('watched'))
    return ratings, diary


def already_seen(film, ratings):
    key = norm(film['title'])
    return (key, film['year']) in ratings or (key, None) in ratings


def taste_text(ratings, diary):
    def line(r):
        return '%s (%s)%s' % (r['title'], r['year'] or '?', ' %.1f' % r['rating'] if r['rating'] else '')
    rated = sorted((r for r in ratings.values() if r['rating']), key=lambda r: -r['rating'])
    parts = [
        'Loved (4.5-5 stars): ' + '; '.join(line(r) for r in rated if r['rating'] >= 4.5),
        'Liked (4 stars): ' + '; '.join(line(r) for r in rated if r['rating'] == 4),
        'Mixed (3-3.5 stars): ' + '; '.join(line(r) for r in rated if 3 <= r['rating'] < 4),
        'Disliked (2.5 or less): ' + ('; '.join(line(r) for r in rated if r['rating'] <= 2.5) or 'none recorded'),
        'Recent diary, newest first: ' + '; '.join(
            '%s (%s)%s on %s' % (e['title'], e.get('year') or '?',
                                 ' %.1f' % e['rating'] if e.get('rating') else '', e.get('watched') or '?')
            for e in diary[:30]),
    ]
    return '\n'.join(parts)


# ---------------------------------------------------------------- Claude

SYSTEM = """You curate a weekly "what's newly worth watching" list for one household. Every film you are shown is streaming on one of their services right now.

The household:
- Nick, 37: a serious film lover. His ratings are in the taste profile. Read them for what he actually responds to, rather than for genre labels alone.
- {kids_line}

Pick only from the CANDIDATES list, and refer to each pick by its tmdbId exactly as given.

Sections:
- kids: films for the children. For a pick marked "now", fit to their current age is the bar: for a preschooler that means a gentle tone, little peril, no frightening villains or long stretches of sadness, and ideally under about 95 minutes. A PG rating alone is not enough; plenty of PG films are too intense for a three-year-old. Mark these audience "norah_now". You may also include standout newer family films the children will be ready for when older; mark those "family_later" and give the age bracket they fit. Always give a bracket; for "norah_now" use the youngest one.
- forYou: films released within roughly the last year that are genuinely newsworthy: festival standouts, acclaimed independent releases (A24, Neon, Mubi and the like), or significant major releases. Leave out direct-to-streaming filler and middling studio product, however popular. Choose what this particular viewer will love.
- archive: films from 15 or more years ago, currently on their services, that fit his taste. Aim for what a well-read friend would recommend, not simply the most famous titles.

Aim for up to 8 kids picks and 6 to 10 in each of the other sections. Return fewer when fewer deserve a place; never pad a section.

Each reason is one or two sentences of at most about 30 words, written to Nick, and specific: name films he rated when that is what makes the case. For kids picks, say why it suits the child's age.

weekNote: one short line summing up this week's batch."""


def schema():
    pick = {'tmdbId': {'type': 'integer'}, 'reason': {'type': 'string'}}
    section = lambda props: {'type': 'array', 'items': {
        'type': 'object', 'properties': props, 'required': list(props), 'additionalProperties': False}}
    return {
        'type': 'object',
        'properties': {
            'weekNote': {'type': 'string'},
            'kids': section(dict(pick,
                                 audience={'type': 'string', 'enum': ['norah_now', 'family_later']},
                                 bracket={'type': 'string', 'enum': BRACKETS})),
            'forYou': section(pick),
            'archive': section(pick),
        },
        'required': ['weekNote', 'kids', 'forYou', 'archive'],
        'additionalProperties': False,
    }


def kids_line(today):
    ages = []
    for k in CONFIG['kids']:
        age = today.year - k['born']
        if age >= CONFIG['kidsMinAge']:
            ages.append('%s, age %d' % (k['name'], age))
        else:
            ages.append('%s, an infant: no picks for him yet' % k['name'])
    return 'The children: ' + '; '.join(ages) + '.'


def call_claude(system, user_text):
    import anthropic
    client = anthropic.Anthropic()
    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=32000,
            betas=['server-side-fallback-2026-07-01'],
            fallbacks='default',
            thinking={'type': 'adaptive'},
            output_config={'effort': 'high', 'format': {'type': 'json_schema', 'schema': schema()}},
            system=system,
            messages=[{'role': 'user', 'content': user_text}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        sys.exit('Anthropic rejected the API key: check the ANTHROPIC_API_KEY repository secret.')
    except anthropic.RateLimitError:
        sys.exit('Anthropic rate limit hit: the next scheduled run will try again.')

    u = msg.usage
    print('model %s | stop %s | input %d | output %d tokens | about $%.2f' % (
        msg.model, msg.stop_reason, u.input_tokens, u.output_tokens,
        u.input_tokens * 4e-6 + u.output_tokens * 20e-6))
    if msg.stop_reason == 'refusal':
        sys.exit('Claude declined the request (%s).' % getattr(msg.stop_details, 'category', None))
    if msg.stop_reason == 'max_tokens':
        sys.exit('Ran out of output tokens before finishing the picks.')
    text = [b.text for b in msg.content if b.type == 'text']
    if not text:
        sys.exit('No picks came back.')
    return json.loads(text[-1])


# ---------------------------------------------------------------- main

def main():
    dry = '--dry-run' in sys.argv
    today = date.today()
    previous = json.load(open(OUT)) if os.path.exists(OUT) else {}
    keep_from = (today - timedelta(weeks=CONFIG['keepWeeks'])).isoformat()
    history = [p for p in previous.get('picks', []) if p.get('week', '') >= keep_from and p.get('week') != today.isoformat()]
    shown = {p['tmdbId'] for p in history}

    label_of = resolve_providers()
    if not label_of:
        sys.exit('None of the configured services were found on TMDB for ' + REGION)
    ratings, diary = load_taste()

    pools = candidate_pools(label_of, today)
    pool_of = {}
    for name, films in pools.items():
        for f in films:
            pool_of.setdefault(f['id'], name)
    print('pool sizes:', {k: len(v) for k, v in pools.items()}, '| unique:', len(pool_of))

    candidates = []
    for movie_id, pool in pool_of.items():
        if movie_id in shown:
            continue
        try:
            film = film_details(movie_id, label_of)
        except urllib.error.HTTPError as e:
            print('skipping', movie_id, e.code)
            continue
        if not film['services'] or already_seen(film, ratings):
            continue
        film['pool'] = pool
        candidates.append(film)
        time.sleep(0.03)
    print('candidates after removing seen and already-suggested films:', len(candidates))
    if not candidates:
        sys.exit('No candidates left to choose from.')

    compact = [{k: f[k] for k in ('tmdbId', 'title', 'year', 'runtime', 'genres', 'directors', 'studios',
                                  'cert', 'tmdbScore', 'votes', 'services', 'pool', 'overview')}
               for f in candidates]
    user_text = ('TASTE PROFILE\n' + taste_text(ratings, diary) +
                 '\n\nTODAY: ' + today.isoformat() +
                 '\n\nCANDIDATES (JSON, one per line)\n' +
                 '\n'.join(json.dumps(c, ensure_ascii=False) for c in compact))
    system = SYSTEM.replace('{kids_line}', kids_line(today))
    print('prompt size: about %d tokens' % ((len(system) + len(user_text)) // 4))
    if dry:
        print('dry run: stopping before the Claude call')
        return

    result = call_claude(system, user_text)
    by_id = {f['tmdbId']: f for f in candidates}
    week = today.isoformat()
    picks, used = [], set()
    for section in ('kids', 'forYou', 'archive'):
        for item in result.get(section, []):
            film = by_id.get(item['tmdbId'])
            if not film or item['tmdbId'] in used:
                print('ignoring unknown or repeated pick', item['tmdbId'])
                continue
            used.add(item['tmdbId'])
            card = {k: v for k, v in film.items() if k != 'pool'}
            card.update(section=section, reason=item['reason'].strip(), week=week)
            if section == 'kids':
                card.update(audience=item['audience'], bracket=item['bracket'])
            picks.append(card)
    print('picks:', {s: sum(1 for p in picks if p['section'] == s) for s in ('kids', 'forYou', 'archive')})
    if not picks:
        sys.exit('Claude returned no usable picks.')

    json.dump({
        'generated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'week': week,
        'weekNote': result.get('weekNote', '').strip(),
        'region': REGION,
        'services': list(CONFIG['services']),
        'picks': picks + [p for p in history if p['tmdbId'] not in used],
    }, open(OUT, 'w'), indent=1, ensure_ascii=False)
    print('wrote', OUT)


if __name__ == '__main__':
    main()
