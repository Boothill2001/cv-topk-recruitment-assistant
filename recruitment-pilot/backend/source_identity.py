"""Deduplicate profile URLs, never people by their names."""
from urllib.parse import urlsplit, unquote

def profile_key(url):
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    path = unquote(parts.path).rstrip('/')
    if host == 'linkedin.com' or host.endswith('.linkedin.com'):
        if path.startswith('/in/') and len(path.split('/')) == 3:
            return 'linkedin.com' + path.casefold()
    return url.split('#', 1)[0]

def distinct_sources(sources):
    grouped = {}
    for source in sources:
        key = profile_key(source['url'])
        if key not in grouped:
            grouped[key] = {**source, 'source_urls': [], 'strategy_ids': [], 'search_ids': []}
        target = grouped[key]
        for field, values in [('source_urls', [source['url']]),
                              ('strategy_ids', source.get('strategy_ids', [])),
                              ('search_ids', source.get('search_ids', []))]:
            for value in values:
                if value not in target[field]: target[field].append(value)
    return list(grouped.values())
