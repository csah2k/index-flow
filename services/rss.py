
import re 
import time
import uuid 
import random
import logging
import datetime
import feedparser
import concurrent.futures
from retrying import retry
from requests.structures import CaseInsensitiveDict

import services.utils as util

class Service:

    def __init__(self, logging, config, idol): 
        self.logging = logging 
        self.config = config.copy()
        self.re_http_url = re.compile(r'^.*(https?://.+)$', re.IGNORECASE)   
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.config.get('threads', 2), thread_name_prefix='RssPool')
        self.idol = idol 

    def index_feeds(self, max_feeds=0):
        self.executor.submit(self._index_feeds, max_feeds).result()
        self.executor.shutdown()
        
    def _index_feeds(self, max_feeds=0):
        self.logging.info(f"==== Starting ====>  RSS indextask '{self.config.get('name')}'")
        start_time = time.time()
        filename = self.config.get('feeds', 'data/feeds')
        feeds_file = open(filename, 'r') 
        Lines = feeds_file.readlines() 
        feeds_urls = set()  ## a set assures to no have duplicated url's
        for _l in Lines: 
            _url = _l.strip()
            if self.re_http_url.match(_url):
                feeds_urls.add(_url)
        feeds_file.close()
        feeds_urls = list(feeds_urls)
        random.shuffle(feeds_urls) ## shuffle to avoid flood same domain with all threads at same time
        if max_feeds <= 0: max_feeds = len(feeds_urls)
        feeds_urls = feeds_urls[:max_feeds]
        self.logging.info(f"Crawling {len(feeds_urls)} urls using {self.config.get('threads', 2)} threads")
       
        index_threads = []
        for _url in feeds_urls:
            index_threads.append(
                self.executor.submit(self.index_feed, _url, 
                    self.executor.submit(self.get_feed_from_url, _url).result())) 

        total_process_docs = 0
        total_indexed_docs = 0
        for _t in index_threads:
            result =  _t.result()
            self.logging.debug(f"{result}")
            total_process_docs += result.get('total', 0)
            total_indexed_docs += result.get('indexed', 0)
        
        elapsed_time = int(time.time() - start_time)
        result = { 'feeds': len(feeds_urls), 'threads': self.config.get('threads', 2), 'scanned': total_process_docs, 'indexed': total_indexed_docs, 'elapsed_seconds': elapsed_time }
        self.logging.info(f"RSS indextask '{self.config.get('name')}' finished: {result}")
        return result

    def index_feed(self, feed_url, feed):
        try:
            return self._index_feed(feed_url, feed)
        except Exception as error:
            self.logging.error(f"RSS_URL: {feed_url} | {str(error)}")
            return { 'url': feed_url, 'error': str(error) }

    def _index_feed(self, feed_url, feed):      
        self.logging.debug(f"_index_feed: '{feed_url}'")
        docsToIndex = []
        for _e in feed.entries:
            link = None
            try:
                link = _e.get('link', _e.get('href', _e.get('url', _e.get('links', [{'href':feed_url}])[0].get('href', feed_url) )))
                if self.re_http_url.match(link): link = self.re_http_url.search(link).group(1)
                self.logging.debug(f"processing feed entry: {link}")
                reference = uuid.uuid3(uuid.NAMESPACE_URL, link)
                date = _e.get('published', _e.get('timestamp', _e.get('date')))
                summr = util.cleanText(_e.get('summary', _e.get('description', _e.get('text',''))))
                title = util.cleanText(_e.get('title', _e.get('titulo', _e.get('headline', summr))))
                
                content = f"{title}\n{summr}"
                lang_info= self.idol.detect_language(content)
                
                summQuery = {
                    'Summary': 'Concept',
                    'Sentences': 2,
                    'LanguageType': lang_info.get('name'),
                    'Text': title
                }   
                title = self.idol.summarize_text(summQuery)

                idolHits = []
                for _query in self.config.get('filters'):
                    idolQuery = _query.copy()
                    idolQuery.update({
                        'Text': content,
                        'SingleMatch': True,
                        'IgnoreSpecials': True,
                    })
                    idolHits += self.idol.query(idolQuery)
                
                if len(idolHits) > 0:
                    docsToIndex.append({
                        'reference': reference,
                        'drecontent': content,
                        'fields': [
                            ('LANGUAGE', lang_info.get('name')),
                            ('DATE', date),
                            ('TITLE', title),
                            ('SUMMARY', summr),
                            ('URL', link),
                            ('FEED', feed_url) ]
                        + [(f'{util.FIELDPREFIX_FILTER}_DBS', _hit.get('database')) for _hit in idolHits] 
                        + [(f'{util.FIELDPREFIX_FILTER}_LNKS', _hit.get('links')) for _hit in idolHits]
                        + [(f'{util.FIELDPREFIX_FILTER}_REFS', _hit.get('reference')) for _hit in idolHits]
                    })
            except Exception as error:
                self.logging.error(f"ENTRY_URL: {link} | {str(error)}")

        if len(docsToIndex) > 0:
            query = {
                'DREDbName': self.config.get('database'),
                'KillDuplicates': 'REFERENCE=2', ## check for the same reference in ALL databases
                'CreateDatabase': True,
                'KeepExisting': True, ## do not replace content for matched references in KillDuplicates
                'Priority': 0
            }
            self.idol.index_into_idol(docsToIndex, query)

        return { 'url': feed_url, 'total': len(feed.entries), 'indexed': len(docsToIndex) }

    @retry(wait_fixed=10000, stop_max_delay=30000)
    def get_feed_from_url(self, feed_url):
        return feedparser.parse(feed_url)



