# Use local patched version instead of pip-installed scihub package
# Patched to support: 1) Updated mirrors (mirror list lives in scihub_patched.py)
#                     2) <object> tag parsing (newer Sci-Hub layout, e.g. .se/.st/.ru)
#                     3) <embed> tag parsing (sci-hub.al / .mk — CDN-fronted via sci.bban.top)
#                     4) Increased ping timeout (10s)
from scihub_patched import SciHub
from tenacity import retry, stop_after_attempt, wait_fixed
import re
import os
import urllib3
import requests

# Disable HTTPS certificate verification warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Crossref polite-pool: with a User-Agent + mailto we get 50 req/sec instead of 1.
# Override CROSSREF_MAILTO via env var to override the default.
CROSSREF_MAILTO = os.environ.get("CROSSREF_MAILTO", "openclaw.tisdell@gmail.com")
CROSSREF_HEADERS = {"User-Agent": f"SciHubMCPServer/1.0 (mailto:{CROSSREF_MAILTO})"}

# In-process metadata cache. Keyed by lowercased DOI; FIFO-evicted at _CACHE_MAX.
_CROSSREF_CACHE: dict = {}
_CACHE_MAX = 256


def create_scihub_instance():
    """Create and configure a SciHub instance"""
    sh = SciHub()
    # Mirror list and ordering live in scihub_patched.py (AVAILABLE_SCIHUB_BASE_URL).
    # Library rotates through them on failure; no per-instance override needed.
    return sh


def _extract_metadata_from_crossref_item(item: dict) -> dict:
    """Map a Crossref `message` item (or items[N]) to {title, author, year}.

    All fields default to '' when missing — matches Crossref's actual data quality
    (older / editorial / dataset DOIs often lack author or date fields).
    """
    title = (item.get('title') or [''])[0]
    authors = item.get('author') or []
    author_str = ', '.join(
        f"{a.get('given', '')} {a.get('family', '')}".strip()
        for a in authors if a.get('family') or a.get('given')
    )
    year = ''
    # Fallback chain — preprints often have published-online but not published.
    # Guard against Crossref returning date-parts: [[null]] (common for figure DOIs)
    # by also requiring parts[0][0] is not None.
    for key in ('published', 'published-online', 'published-print', 'issued'):
        parts = (item.get(key) or {}).get('date-parts') or []
        if parts and parts[0] and parts[0][0] is not None:
            year = str(parts[0][0])
            break
    return {'title': title, 'author': author_str, 'year': year}


def _normalize_title(s: str) -> str:
    """Lowercase, strip non-alphanumerics, collapse whitespace. Unicode-safe."""
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', ' ', (s or '').lower())).strip()


def _score_title_match(query: str, candidate: str) -> float:
    """SequenceMatcher ratio on normalized strings. Range [0.0, 1.0]."""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, _normalize_title(query),
                           _normalize_title(candidate)).ratio()


@retry(stop=stop_after_attempt(2), wait=wait_fixed(0.5), reraise=True)
def _fetch_crossref_metadata(doi: str):
    """Look up Crossref metadata for a DOI, returning the `message` dict or None.

    Returns None on 404 or other non-200 (not all Sci-Hub-indexed DOIs are in
    Crossref). Raises on network failure after one retry — caller handles.
    """
    key = doi.lower()
    if key in _CROSSREF_CACHE:
        return _CROSSREF_CACHE[key]
    url = f"https://api.crossref.org/works/{doi}"
    r = requests.get(url, headers=CROSSREF_HEADERS, timeout=10)
    if r.status_code != 200:
        return None
    item = r.json().get('message')
    if len(_CROSSREF_CACHE) >= _CACHE_MAX:
        _CROSSREF_CACHE.pop(next(iter(_CROSSREF_CACHE)))
    _CROSSREF_CACHE[key] = item
    return item


def search_paper_by_doi(doi, crossref_item=None):
    """Search for a paper on Sci-Hub by DOI, enriching with Crossref metadata.

    If `crossref_item` is provided (e.g. by title/keyword search that already
    fetched it), reuse it instead of hitting Crossref again.
    """
    sh = create_scihub_instance()
    try:
        result = sh.fetch(doi)
    except Exception as e:
        print(f"Search error: {str(e)}")
        return {'doi': doi, 'status': 'not_found'}

    response = {
        'doi': doi,
        'pdf_url': result['url'],
        'status': 'success',
        'title': '',
        'author': '',
        'year': '',
    }
    try:
        item = crossref_item if crossref_item is not None else _fetch_crossref_metadata(doi)
        if item:
            response.update(_extract_metadata_from_crossref_item(item))
        else:
            response['metadata_warning'] = 'Crossref lookup returned no result'
    except Exception as e:
        print(f"Crossref enrichment error: {str(e)}")
        response['metadata_warning'] = f'Crossref lookup failed: {e}'
    return response


def search_paper_by_title(title):
    """Resolve title -> DOI via Crossref query.bibliographic, then fetch.

    Filters Crossref's top 5 results by SequenceMatcher similarity >= 0.65
    to reject low-confidence matches (the prior code blindly took the first
    hit using the deprecated query.title parameter, returning wrong papers
    for ambiguous queries like "Attention Is All You Need").
    """
    SIM_THRESHOLD = 0.65
    candidates = []
    try:
        url = "https://api.crossref.org/works"
        params = {'query.bibliographic': title, 'rows': 5}
        response = requests.get(url, headers=CROSSREF_HEADERS,
                                params=params, timeout=10)
        if response.status_code == 200:
            items = response.json().get('message', {}).get('items', []) or []
            for it in items:
                cand_title = (it.get('title') or [''])[0]
                if not it.get('DOI'):
                    continue  # rare but possible for some record types
                score = _score_title_match(title, cand_title)
                candidates.append((score, it, cand_title))
            candidates.sort(key=lambda x: x[0], reverse=True)
            if candidates and candidates[0][0] >= SIM_THRESHOLD:
                best_item = candidates[0][1]
                result = search_paper_by_doi(best_item['DOI'],
                                             crossref_item=best_item)
                if result.get('status') != 'success':
                    # High-confidence Crossref match found but Sci-Hub fetch
                    # failed. Preserve title-search context so the consumer
                    # sees what we tried and why.
                    result['title'] = (best_item.get('title') or [''])[0]
                    result['reason'] = 'matched Crossref entry not available on Sci-Hub'
                    result['match_score'] = round(candidates[0][0], 3)
                return result
    except Exception as e:
        print(f"CrossRef search error: {str(e)}")

    return {
        'title': title,
        'status': 'not_found',
        'reason': 'no high-confidence title match' if candidates
                  else 'crossref returned no results',
        'candidates': [{'doi': it.get('DOI'), 'title': t, 'score': round(s, 3)}
                       for s, it, t in candidates[:3]],
    }


def search_papers_by_keyword(keyword, num_results=10):
    """Search papers by keyword. Returns a list of dicts with mixed status.

    Success entries carry pdf_url; not_found entries carry pdf_url=None plus
    Crossref-sourced title/author/year and a reason field — so the caller
    knows what Crossref found even when Sci-Hub couldn't fetch the PDF.
    """
    # Single Crossref hit returns N items with full metadata; pass each item
    # through to search_paper_by_doi to avoid N redundant /works/{doi} calls.
    # query.bibliographic scopes the search to bibliographic fields (title,
    # author, year, ISSN) — better signal than raw `query=` which also searches
    # abstracts/funders. Using params= dict also gets URL encoding for free.
    papers = []
    try:
        url = "https://api.crossref.org/works"
        params = {'query.bibliographic': keyword, 'rows': num_results}
        response = requests.get(url, headers=CROSSREF_HEADERS,
                                params=params, timeout=10)
        if response.status_code == 200:
            for item in response.json()['message']['items']:
                doi = item.get('DOI')
                if not doi:
                    continue
                result = search_paper_by_doi(doi, crossref_item=item)
                if result['status'] != 'success':
                    # Sci-Hub couldn't fetch this paper — surface the Crossref
                    # metadata so the consumer at least sees what exists.
                    meta = _extract_metadata_from_crossref_item(item)
                    result = {
                        'doi': doi,
                        'pdf_url': None,
                        'status': 'not_found',
                        'reason': 'not available on Sci-Hub',
                        **meta,
                    }
                papers.append(result)
    except Exception as e:
        print(f"Search error: {str(e)}")

    return papers


def download_paper(pdf_url, output_path):
    """Download a PDF via direct HTTP GET. Auto-creates parent dir if missing."""
    try:
        from scihub_patched import HEADERS
        out_dir = os.path.dirname(output_path) or '.'
        os.makedirs(out_dir, exist_ok=True)
        r = requests.get(pdf_url, headers=HEADERS, verify=False,
                         timeout=30, stream=True)
        r.raise_for_status()
        ctype = r.headers.get('Content-Type', '').lower()
        if 'pdf' not in ctype and 'octet-stream' not in ctype:
            print(f"Warning: unexpected Content-Type {ctype!r} for {pdf_url}")
        with open(output_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return os.path.getsize(output_path) > 0
    except Exception as e:
        print(f"Download error: {str(e)}")
        return False


if __name__ == "__main__":
    print("Sci-Hub Paper Search Test\n")

    # 1. DOI search test
    print("1. Search paper by DOI")
    test_doi = "10.1002/jcad.12075"  # A neuroscience-related paper
    result = search_paper_by_doi(test_doi)

    if result['status'] == 'success':
        print(f"Title: {result['title']}")
        print(f"Author: {result['author']}")
        print(f"Year: {result['year']}")
        print(f"PDF URL: {result['pdf_url']}")
        if result.get('metadata_warning'):
            print(f"Metadata warning: {result['metadata_warning']}")

        # Assertions for the metadata-enrichment fix
        assert result['title'], f"title empty for {test_doi}"
        assert result['author'], f"author empty for {test_doi}"
        assert result['year'], f"year empty for {test_doi}"
        print("[OK] title/author/year all populated")

        # Try to download the paper
        output_file = f"paper_{test_doi.replace('/', '_')}.pdf"
        if download_paper(result['pdf_url'], output_file):
            print(f"Paper downloaded to: {output_file}")
        else:
            print("Paper download failed")
    else:
        print(f"Paper with DOI {test_doi} not found")

    # 2. Title search test
    print("\n2. Search paper by title")
    test_title = "Choosing Assessment Instruments for Posttraumatic Stress Disorder Screening and Outcome Research"
    result = search_paper_by_title(test_title)

    if result['status'] == 'success':
        print(f"DOI: {result['doi']}")
        print(f"Title: {result.get('title', '')}")
        print(f"Author: {result['author']}")
        print(f"Year: {result['year']}")
        print(f"PDF URL: {result['pdf_url']}")
    else:
        print(f"Paper with title '{test_title}' not found")

    # 3. Keyword search test
    print("\n3. Search papers by keyword")
    test_keyword = "artificial intelligence medicine 2023"
    papers = search_papers_by_keyword(test_keyword, num_results=3)

    for i, paper in enumerate(papers, 1):
        print(f"\nPaper {i}:")
        print(f"Status: {paper['status']}")
        print(f"Title: {paper['title']}")
        print(f"DOI: {paper['doi']}")
        print(f"Author: {paper['author']}")
        print(f"Year: {paper['year']}")
        if paper.get('pdf_url'):
            print(f"PDF URL: {paper['pdf_url']}")
        else:
            print(f"[no PDF] reason: {paper.get('reason', 'unknown')}")
        if paper['status'] == 'not_found':
            assert paper.get('title'), f"not_found entry missing title: {paper}"
            assert paper.get('reason'), f"not_found entry missing reason: {paper}"

    # 4. Adversarial title test — must NOT return junk
    print("\n4. Title search precision test")
    result = search_paper_by_title("Attention Is All You Need")
    if result['status'] == 'success':
        assert 'attention' in result['title'].lower(), \
            f"wrong paper returned: {result['title']}"
        print(f"[OK] resolved to {result['doi']}: {result['title']}")
    else:
        assert 'reason' in result, "missing reason field on not_found"
        print(f"[OK] clean not_found: {result['reason']}")
        print(f"     candidates: {result.get('candidates', [])}")

    # 5. Download test — must actually write a non-empty PDF
    print("\n5. Download test")
    test_doi2 = "10.1002/jcad.12075"
    r = search_paper_by_doi(test_doi2)
    if r['status'] == 'success':
        out = "smoke_download.pdf"
        ok = download_paper(r['pdf_url'], out)
        assert ok and os.path.getsize(out) > 1000, \
            "download produced empty/tiny file"
        print(f"[OK] downloaded {os.path.getsize(out)} bytes")
        os.remove(out)

    # 6. Bad-DOI rotation safety test — exercises _change_base_url + @retry
    # interaction to ensure exhausted mirrors yield clean not_found, not
    # IndexError/TypeError/raw exception.
    print("\n6. Bad-DOI rotation safety test")
    r = search_paper_by_doi("10.9999/definitely-not-a-real-doi-xyz")
    assert r['status'] == 'not_found' and r.get('doi'), \
        f"bad DOI must return clean not_found, got {r}"
    print("[OK] bad DOI returned clean not_found, no exception escaped")

    # 7. Bug-fix regression checks (year string, captcha cruft, tenacity)
    print("\n7. Bug-fix regression checks")

    # Bug 1: figure-DOI year must not leak the literal string "None"
    fig_item = _fetch_crossref_metadata("10.7717/peerj-cs.3254/fig-10")
    if fig_item:
        meta = _extract_metadata_from_crossref_item(fig_item)
        assert meta['year'] != 'None', f"year leaked 'None' string: {meta}"
        print(f"[OK] figure DOI year handled cleanly: {meta['year']!r}")
    else:
        print("[SKIP] figure DOI lookup returned no Crossref item")

    # Bug 2: captcha cruft removed
    sh = create_scihub_instance()
    assert not hasattr(sh, 'captcha_url'), "captcha_url should be removed"
    assert not hasattr(sh, 'get_captcha_url'), "get_captcha_url should be removed"
    assert not hasattr(sh, '_set_captcha_url'), "_set_captcha_url should be removed"
    print("[OK] captcha cruft removed")

    # Bug 3: tenacity present (retrying may still be installed transitively)
    import tenacity  # must resolve
    print(f"[OK] tenacity available")
    try:
        import retrying
        print("[note] retrying still importable (transitive dep, not used)")
    except ImportError:
        print("[OK] retrying uninstalled")

    # Bug 4 implicitly covered by section #3 (keyword search exercises the path)
