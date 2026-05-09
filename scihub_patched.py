# -*- coding: utf-8 -*-

import argparse
import hashlib
import logging
import os

import requests
from bs4 import BeautifulSoup
from tenacity import (
    retry,
    stop_after_attempt,
    wait_random,
    retry_if_not_exception_type,
)

# log config
logging.basicConfig()
logger = logging.getLogger('scihub')
logger.setLevel(logging.DEBUG)

# constants
RETRY_TIMES = 3
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

AVAILABLE_SCIHUB_BASE_URL = [
    "sci-hub.al",     # CDN-fronted (sci.bban.top), ~150-500ms, no anti-bot gate
    "sci-hub.mk",     # same as .al — identical layout and CDN
    "sci-hub.ru",     # ddos-guard but session-friendly; primary fallback
    "sci-hub.st",     # ddos-guard, equivalent to .ru
    "sci-hub.ren",    # Cloudflare; backup
    "sci-hub.ee",     # Cloudflare; backup
]


class NoMoreMirrorsException(Exception):
    """Raised by _change_base_url when all mirrors in the list are exhausted.

    The @retry decorator on fetch() short-circuits on this via
    retry_if_not_exception_type so retries don't waste attempts trying to
    access an out-of-bounds index. Defined before SciHub because
    retry_if_not_exception_type resolves its argument at decorator time.
    """
    pass


class SciHub(object):
    """
    SciHub class can fetch/download papers from sci-hub.io
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers = HEADERS
        self.available_base_url_list = AVAILABLE_SCIHUB_BASE_URL
        self.tries = 0
        self.current_base_url_index = 0

    def set_proxy(self, proxy):
        '''
        Set proxy for session.
        Proxy format like socks5://user:pass@host:port
        '''
        self.session.proxies = {
            "http": proxy,
            "https": proxy,
        }

    @property
    def base_url(self):
        return 'https://{0}/'.format(
            self.available_base_url_list[self.current_base_url_index]
        )

    def _change_base_url(self):
        """Advance to the next mirror in the list.

        Raises NoMoreMirrorsException BEFORE incrementing past the end so the
        instance is never left in an out-of-bounds state. The @retry predicate
        on fetch() catches this and stops retrying.
        """
        if self.current_base_url_index + 1 >= len(self.available_base_url_list):
            raise NoMoreMirrorsException(
                "Exhausted all {0} sci-hub mirrors".format(
                    len(self.available_base_url_list)))
        self.current_base_url_index += 1
        logger.info(
            "Changing to {0}".format(
                self.available_base_url_list[self.current_base_url_index]))

    @retry(
        wait=wait_random(min=0.1, max=1.0),
        stop=stop_after_attempt(RETRY_TIMES),
        retry=retry_if_not_exception_type(NoMoreMirrorsException),
        reraise=True,
    )
    def fetch(self, identifier):
        """
        Fetches the paper by first retrieving the direct link to the pdf.
        If the indentifier is a DOI, PMID, or URL pay-wall, then use Sci-Hub
        to access and download paper. Otherwise, just download paper directly.
        """
        self.tries += 1
        logger.info(
            '{0} Downloading with {1}'.format(self.tries, self.base_url)
        )
        try:
            url = self._get_direct_url(identifier)
        except Exception as e:
            self._change_base_url()
            raise e
        else:
            if url is None:
                self._change_base_url()
                raise DocumentUrlNotFound('Direct url could not be retrieved')

        logger.info('direct_url = {0}'.format(url))

        try:
            # verify=False is dangerous but sci-hub.io
            # requires intermediate certificates to verify
            # and requests doesn't know how to download them.
            # as a hacky fix, you can add them to your store
            # and verifying would work. will fix this later.
            res = self.session.get(url, verify=False)

            if res.headers.get('Content-Type', '') != 'application/pdf':
                logger.error('CAPTCHA needed')
                # Rotate so the retry hits a different mirror. Swallow exhaustion
                # here — we still want to raise the captcha error to the caller.
                try:
                    self._change_base_url()
                except NoMoreMirrorsException:
                    pass
                raise CaptchaNeededException(
                    'Failed to fetch pdf with identifier {0} '
                    '(resolved url {1}) due to captcha'.format(identifier, url))
            return {'pdf': res.content, 'url': url}

        except CaptchaNeededException:
            # Already rotated above; just propagate.
            raise

        except requests.exceptions.ConnectionError:
            current = self.available_base_url_list[self.current_base_url_index]
            logger.error('{0} cannot access, changing'.format(current))
            # Rotate then re-raise so @retry fires on a different mirror.
            # If exhausted, NoMoreMirrorsException propagates (retry predicate stops).
            self._change_base_url()
            raise

        except requests.exceptions.RequestException as e:
            # Other request errors (timeout, SSL, etc.) — same pattern.
            self._change_base_url()
            raise Exception(
                'Failed to fetch pdf with identifier {0} '
                '(resolved url {1}) due to request exception: {2}'.format(
                    identifier, url, e))

        except Exception:
            # Catch-all for any other failure: rotate and re-raise the original
            # exception so @retry can fire on a new mirror.
            try:
                self._change_base_url()
            except NoMoreMirrorsException:
                pass
            raise


    def _get_direct_url(self, identifier):
        """
        Finds the direct source url for a given identifier.
        """
        id_type = self._classify(identifier)
        logger.debug('id_type = {0}'.format(id_type))

        return identifier if id_type == 'url-direct' \
            else self._search_direct_url(identifier)

    def _search_direct_url(self, identifier):
        """
        Sci-Hub embeds papers in an iframe. This function finds the actual
        source url which looks something like

            https://moscow.sci-hub.io/.../....pdf.
        """

        logger.debug('Pinging {0}'.format(self.base_url))
        ping = self.session.get(self.base_url, timeout=10, verify=False)
        if not ping.status_code == 200:
            logger.error('Server {0} is down '.format(self.base_url))
            return None

        logger.info('Server {0} is up'.format(self.base_url))

        url = self.base_url + identifier
        logger.info('scihub url {0}'.format(url))
        res = self.session.get(url, verify=False)
        logger.debug('Scraping scihub site')
        s = BeautifulSoup(res.content, 'html.parser')
        iframe = s.find('iframe')
        if iframe:
            logger.info('iframe found in scihub\'s html')
            return iframe.get('src') if not iframe.get('src').startswith('//') \
                else 'https:' + iframe.get('src')

        # Also check for <object> tag (newer Sci-Hub layout)
        obj = s.find('object', {'type': 'application/pdf'})
        if obj and obj.get('data'):
            logger.info('object tag found in scihub html')
            data_url = obj.get('data').split('#')[0]  # Remove fragment
            if data_url.startswith('/'):
                return self.base_url.rstrip('/') + data_url
            elif data_url.startswith('//'):
                return 'https:' + data_url
            return data_url

        # Also check for <embed> tag (sci-hub.al, sci-hub.mk layout — CDN-fronted via sci.bban.top)
        embed_tag = s.find('embed', {'type': 'application/pdf'})
        if embed_tag and embed_tag.get('src'):
            logger.info('embed tag found in scihub html')
            src = embed_tag.get('src').split('#')[0]
            if src.startswith('/'):
                return self.base_url.rstrip('/') + src
            elif src.startswith('//'):
                return 'https:' + src
            return src

    def _classify(self, identifier):
        """
        Classify the type of identifier:
        url-direct - openly accessible paper
        url-non-direct - pay-walled paper
        pmid - PubMed ID
        doi - digital object identifier
        """
        if (identifier.startswith('http') or identifier.startswith('https')):
            if identifier.endswith('pdf'):
                return 'url-direct'
            else:
                return 'url-non-direct'
        elif identifier.isdigit():
            return 'pmid'
        else:
            return 'doi'


class CaptchaNeededException(Exception):
    pass


class DocumentUrlNotFound(Exception):
    pass
