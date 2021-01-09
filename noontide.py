import re
import sys
import hashlib
import asyncio
import httpx
from pathlib import Path
from urllib.parse import unquote
from urllib.parse import urlparse

import pyppeteer
from lxml.html import fromstring as string2html
from lxml.html import tostring as html2string
from loguru import logger as log


javascript_scroll = """
function() {
    window.scrollBy(0, window.innerHeight);
}
"""

javascript_nodes_count = """
function() {
    return document.getElementsByTagName('*').length;
}
"""


async def read(browser, url):
    response = await browser.goto(url, {'waitUntil': 'networkidle0'})
    if not response.ok:
        log.error("Error with: {}", url)
        return None

    # scroll down completly
    count = await browser.evaluate(javascript_nodes_count)
    while True:
        log.debug('loading...')
        await browser.evaluate(javascript_scroll)
        # TODO: check zero network requests remaining.
        count_new = await browser.evaluate(javascript_nodes_count)
        if count == count_new:
            break
        else:
            count = count_new
    # TODO: open toggle block.
    content = await browser.content()
    return content


def clean(html):
    META_NAMES = [
        "description",
        "twitter:card",
        "twitter:site",
        "twitter:title",
        "twitter:description",
        "twitter:image",
        "twitter:url",
        "apple-itunes-app",
    ]

    for name in META_NAMES:
        for element in html.xpath("//meta[name='{}']".format(name)):
            element.getparent().remove(element)

    META_PROPERTIES = [
        "og:site_name",
        "og:type",
        "og:url",
        "og:title",
        "og:description",
        "og:image",
    ]

    for property in META_PROPERTIES:
        for element in html.xpath("//meta[property='{}']".format(property)):
            element.getparent().remove(element)

    XPATH_QUERIES = [
        "//script",
        "//div[@id='intercom-frame']",
        "//div[@class='intercom-lightweight-app']",
        "//div[@class='notion-overlay-container']",
        "//link[starts-with(@href,'/vendors~')]",
    ]

    for query in XPATH_QUERIES:
        for element in html.xpath(query):
            element.getparent().remove(element)


def is_image_notion_forward(url):
    return url.startswith('/image/https')


def image_notion_forward_filename(url):
    return unquote(urlparse(url).path).split('/')[-1]


def massage_images(html):
    out = set()
    for element in html.xpath('//img'):
        src = element.attrib["src"]
        if 'notion-emoji' in element.attrib.get('class', ''):
            assert src.startswith('data:image')
            style = element.attrib["style"]
            result = re.search(r'background: url\("([^\"]+)"\)', style)
            if result is None:
                continue
            new = set("https://www.notion.so" + src for src in result.groups())
            out = out | new
        else:
            if src.startswith("https://images.unsplash.com/"):
                # Do not need to cache. According to the terms of
                # services of unsplash, hot linking is recommended.
                continue
            if src.startswith('/'):
                if is_image_notion_forward(src):
                    filename = image_notion_forward_filename(src)
                    element.attrib['src'] = '/' + filename
                src = "https://www.notion.so" + src
            else:
                element.attrib["src"] = urlparse(src).path
            out.add(src)
    return out


def massage_stylesheets(html):
    # maybe TODO: Extract custom fonts
    out = set()
    for element in html.xpath("//link[@rel='stylesheet']"):
        url = "https://www.notion.so" + element.attrib["href"]
        out.add(url)
    return out


def massage_ahrefs(html):
    out = set()
    for element in html.xpath("//a"):
        href = element.attrib["href"]
        if href.startswith('/'):
            if '?' in href:
                href_rewritten = hashlib.md5(href.encode('utf8')).hexdigest()
                element.attrib["href"] = '/' + href_rewritten
            url = 'https://www.notion.so' + href
            out.add(url)
        # TODO: else just check whether url is a dead link or not
    return out


async def is_html(http, url):
    try:
        response = await http.head(url, allow_redirects=True)
    except httpx.ConnectError:
        log.critical('Need to restart!')
        return None
    except httpx.ConnectTimeout:
        log.critical('Need to restart!')
        return None
    except httpx.ReadTimeout:
        log.critical('Need to restart!')
        return None # too bad!

    if response.status_code != 200:
        msg = "Error with: `{}`, code={}"
        log.error(msg, url, response.status_code)
        raise Exception('Oops!')

    content_type = response.headers["content-type"]
    return content_type.startswith("text/html")


async def crawl(browser, http, url):
    log.debug("crawling url: {}", url)

    # content-type check
    html = await is_html(http, url)
    if html is None:
        # XXX: connection error notion side... not sure what it is
        return None, False, set()

    if not html:
        response = await http.get(url)
        if response.status_code != 200:
            msg = "Error with: `{}`, code={}"
            log.error(msg, url, response.status_code)
            raise Exception('Oops!')
        return response.content, False, set()

    # Otherwise crawl...

    content = await read(browser, url)
    if content is None:
        return None, False, set()

    html = string2html(content.encode('utf8'))
    clean(html)  # sideeffect!
    images = massage_images(html)  # sideeffect
    stylesheets = massage_stylesheets(html)
    hrefs = massage_ahrefs(html)
    # prepare return
    content = html2string(html, pretty_print=True)
    todo = images | stylesheets | hrefs
    return content, True, todo


def write(root, url, content, html):
    if is_image_notion_forward(url):
        path = image_notion_forward_filename(url)
    else:
        path = urlparse(url).path
        if html:
            if '?' in url:
                path = '/' + hashlib.md5(path.encode('utf8')).hexdigest()
            path += ".html"

    path = Path(path[1:])
    target = root / path

    target.parent.mkdir(parents=True, exist_ok=True)

    with target.open('wb') as f:
        f.write(content)


async def main(index):
    root = Path(__file__).resolve().parent / "build"
    todo = set([index])
    done = set()
    output = {}

    shell = await pyppeteer.launch(
        defaultViewport=dict(width=1920, height=1080)
    )
    browser = await shell.newPage()
    try:
        async with httpx.AsyncClient() as http:
            while todo:
                url = todo.pop()
                done.add(url)
                try:
                    content, html, todo_new = await crawl(browser, http, url)
                except Exception:
                    log.exception("error with: `{}`", url)
                else:
                    output[url] = (content, html)
                    todo = todo | todo_new
                    todo = {x for x in todo if x not in done}
    except Exception:
        log.exception("Oops")
    else:
        for url, (content, html) in output.items():
            if content is not None:
                write(root, url, content, html)
    finally:
        await browser.close()
        await shell.close()


if __name__ == "__main__":
    log.debug("Starting noontide's main...")
    asyncio.run(main(sys.argv[1]))
