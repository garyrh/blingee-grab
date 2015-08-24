# encoding=utf8
import datetime
from distutils.version import StrictVersion
import hashlib
import os.path
import random
from seesaw.config import realize, NumberConfigValue
from seesaw.item import ItemInterpolation, ItemValue
from seesaw.task import SimpleTask, LimitConcurrent
from seesaw.tracker import GetItemFromTracker, PrepareStatsForTracker, \
    UploadWithTracker, SendDoneToTracker
import shutil
import socket
import subprocess
import sys
import time
import string
import requests
import re
from base64 import b64decode
from Crypto.Cipher import AES
from lxml import etree

import seesaw
from seesaw.externalprocess import WgetDownload
from seesaw.pipeline import Pipeline
from seesaw.project import Project
from seesaw.util import find_executable


# check the seesaw version
if StrictVersion(seesaw.__version__) < StrictVersion("0.8.5"):
    raise Exception("This pipeline needs seesaw version 0.8.5 or higher.")


###########################################################################
# Find a useful Wget+Lua executable.
#
# WGET_LUA will be set to the first path that
# 1. does not crash with --version, and
# 2. prints the required version string
WGET_LUA = find_executable(
    "Wget+Lua",
    ["GNU Wget 1.14.lua.20130523-9a5c"],
    [
        "./wget-lua",
        "./wget-lua-warrior",
        "./wget-lua-local",
        "../wget-lua",
        "../../wget-lua",
        "/home/warrior/wget-lua",
        "/usr/bin/wget-lua"
    ]
)

if not WGET_LUA:
    raise Exception("No usable Wget+Lua found.")

###########################################################################
# The version number of this pipeline definition.
#
# Update this each time you make a non-cosmetic change.
# It will be added to the WARC files and reported to the tracker.
VERSION = "20150821.02"
TRACKER_ID = 'blingee'
TRACKER_HOST = 'tracker.archiveteam.org'
# Number of blingees per item
NUM_BLINGEES = 100
# Number of profiles per item
NUM_PROFILES = 10
# Number of stamps per item
NUM_STAMPS = 20

USER_AGENTS = ['Mozilla/5.0 (Windows NT 6.3; rv:24.0) Gecko/20100101 Firefox/39.0',
               'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.9; rv:25.0) Gecko/20100101 Firefox/39.0',
               'Mozilla/5.0 (Windows NT 6.1; WOW64; Trident/7.0; AS; rv:11.0) like Gecko',
               'Mozilla/5.0 (compatible; MSIE 9.0; Windows NT 6.1; Trident/5.0)',
               'Mozilla/5.0 (compatible; MSIE 9.0; Windows NT 6.1; WOW64; Trident/5.0)',
               'Opera/9.80 (Windows NT 6.0; rv:2.0) Presto/2.12.388 Version/12.16',
               'Mozilla/5.0 (Windows NT 6.3; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/44.0.2403.155 Safari/537.36']
USER_AGENT = random.choice(USER_AGENTS)
REQUESTS_HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
myparser = etree.HTMLParser(encoding="utf-8")

###########################################################################
# Stamp grabbing variables
# Apparently Blingee has a limit of 9 tags on their tag search(?)
LIMIT = 9
# The ciphertext needs to be base64-decoded but *not* the key. wat.
# Also, only the first 16 bytes of the key are needed.
key = 'rAI1P8bpXoReutED8XOTT0lh26MWhWz87IH4t39LjJp3wxLkEHDKE2Er'[:16]
cipher = AES.new(key, AES.MODE_ECB)
BLOCK_SIZE = 16

def base36_encode(n):
    """
    Encode integer value `n` using `alphabet`. The resulting string will be a
    base-N representation of `n`, where N is the length of `alphabet`.

    Copied from https://github.com/benhodgson/basin/blob/master/src/basin.py
    """
    alphabet="0123456789abcdefghijklmnopqrstuvwxyz"
    if not (isinstance(n, int) or isinstance(n, long)):
        raise TypeError('value to encode must be an int or long')
    r = []
    base  = len(alphabet)
    while n >= base:
        r.append(alphabet[n % base])
        n = n / base
    r.append(str(alphabet[n % base]))
    r.reverse()
    return ''.join(r)


def get_url(url):
    tries = 0
    while tries < 50:
        try:
            html = requests.get(url, headers=REQUESTS_HEADERS)
        except requests.ConnectionError:
            print("Got a connection error getting url, sleeping...")
            sys.stdout.flush()
            time.sleep(5)
            tries += 1
            continue
        if html.status_code == 200 and html.text:
            return html.text
        else:
            print("Got status code {0}, sleeping...".format(html.status_code))
            sys.stdout.flush()
            time.sleep(5)
            tries += 1
    raise Exception("Failed downloading {0}".format(url))


def get_list(query, stamp_id, args=""):
    url = "http://blingee.com/stamp/embedded_list?query={0}{1}".format(query, args)
    search = get_url(url)
    tree = etree.HTML(search, parser=myparser)
    if 'galleryItem_{0}"'.format(stamp_id) in search:
        ciphers = tree.xpath("//div[@class='list-action_link']/a/@onclick")
        for cipher in ciphers:
            if 'galleryItem_{0}"'.format(stamp_id) in cipher:
                cipher = re.findall("addStampToBlingeeMaker\('([^']+)'\)", cipher)
                if cipher:
                    return cipher[0]
    return ""


def page_loop(query, stamp_id):
    #print "Trying at most 10 pages..."
    for page in xrange(1, 50):
        cipher = get_list(query, stamp_id, "&page={0}".format(page))
        if cipher:
            return cipher
    return ""


def get_ciphertext(stamp_id, html):
    if html:
        if "Oops, Error" in html or "Account Login" in html:
            return "Denied"
        else:
            tree = etree.HTML(html, parser=myparser)

    title = tree.xpath("//div[@id='picdisplay_wrapper']/h1/text()")
    title = title[0].encode('utf-8') or ""
    title = title.replace(" ", "+")
    tags = tree.xpath("//*[@id='picdetails']/dl/dd[3]/div/a/text()")

    # Make sure space become + in title/tags
    tags = " ".join(tags).encode('utf-8').split(" ")
    tags_str = "+".join(" ".join(tags).split(" ")[:LIMIT]) or ""
    title = title.split("+")
    title_str = "+".join(title)
    if tags_str:
        title_tags = "{0}+{1}".format(title_str, "+".join(tags[:LIMIT-len(title)]))
    else:
        title_tags = ""

    #print tags_str or title_str or stamp_id
    for query in [tags_str, title_str, title_tags]:
	if query:
            #print query
            cipher = get_list(query, stamp_id)
            if cipher:
                return cipher

    # One last try (with quotes around title now.)
    tags_str = "+".join(tags[:8])
    if tags_str:
        tags_str = "+{0}".format(tags_str)
    else:
        tags_str = ""
    cipher = page_loop('"{0}"{1}'.format(title_str, tags_str), stamp_id)
    if cipher:
        return cipher
    else: 
        return "Unknown"


def decrypt(ciphertext):
    ciphertext = ciphertext.decode('base64')
    decrypted = cipher.decrypt(ciphertext)

    # Ciphertext padding is simply the byte
    # chr(BLOCK_SIZE-(len(plaintext)%BLOCK_SIZE)) repeated 
    # that many times.
    end = len(decrypted)-ord(decrypted[-1])
    plaintext = decrypted[0:end]
    return plaintext

def stamp_scraper(min_stamp_id, max_stamp_id):
    min_stamp_id = min_stamp_id
    max_stamp_id = max_stamp_id
    urls = []
    for stamp_id in xrange(min_stamp_id, max_stamp_id):
        print("Finding stamp swf with ID {0}".format(str(stamp_id)))
        stamp_page = get_url("http://blingee.com/stamp/view/{0}".format(str(stamp_id)))
        ciphertext = get_ciphertext(stamp_id, stamp_page)

        # Found the ciphertext!
        if ciphertext != "Denied" and ciphertext != "Unknown":
            plaintext = decrypt(ciphertext)
            # Get the original swf only (skip the thumbnail.)
            swf_urls = re.findall('swfHref="(http[^"]+)', plaintext)
            urls.extend(swf_urls)
        # Couldn't find the ciphertext. :(
        else:
            print("Skipping {0}, error is: {1}".format(str(stamp_id), ciphertext))
    return urls

###########################################################################
# This section defines project-specific tasks.
#
# Simple tasks (tasks that do not need any concurrency) are based on the
# SimpleTask class and have a process(item) method that is called for
# each item.
class CheckIP(SimpleTask):
    def __init__(self):
        SimpleTask.__init__(self, "CheckIP")
        self._counter = 0

    def process(self, item):
        # NEW for 2014! Check if we are behind firewall/proxy
        if self._counter <= 0:
            item.log_output('Checking IP address.')
            ip_set = set()

            ip_set.add(socket.gethostbyname('twitter.com'))
            ip_set.add(socket.gethostbyname('facebook.com'))
            ip_set.add(socket.gethostbyname('youtube.com'))
            ip_set.add(socket.gethostbyname('microsoft.com'))
            ip_set.add(socket.gethostbyname('icanhas.cheezburger.com'))
            ip_set.add(socket.gethostbyname('archiveteam.org'))

            if len(ip_set) != 6:
                item.log_output('Got IP addresses: {0}'.format(ip_set))
                item.log_output(
                    'Are you behind a firewall/proxy? That is a big no-no!')
                raise Exception(
                    'Are you behind a firewall/proxy? That is a big no-no!')

        # Check only occasionally
        if self._counter <= 0:
            self._counter = 10
        else:
            self._counter -= 1


class PrepareDirectories(SimpleTask):
    def __init__(self, warc_prefix):
        SimpleTask.__init__(self, "PrepareDirectories")
        self.warc_prefix = warc_prefix

    def process(self, item):
        item_name = item["item_name"]
        escaped_item_name = item_name.replace(':', '_').replace('/', '_').replace('~', '_')
        dirname = "/".join((item["data_dir"], escaped_item_name))

        if os.path.isdir(dirname):
            shutil.rmtree(dirname)

        os.makedirs(dirname)

        item["item_dir"] = dirname
        item["warc_file_base"] = "%s-%s-%s" % (self.warc_prefix, escaped_item_name,
            time.strftime("%Y%m%d-%H%M%S"))

        open("%(item_dir)s/%(warc_file_base)s.warc.gz" % item, "w").close()


class MoveFiles(SimpleTask):
    def __init__(self):
        SimpleTask.__init__(self, "MoveFiles")

    def process(self, item):
        # NEW for 2014! Check if wget was compiled with zlib support
        if os.path.exists("%(item_dir)s/%(warc_file_base)s.warc" % item):
            raise Exception('Please compile wget with zlib support!')

        os.rename("%(item_dir)s/%(warc_file_base)s.warc.gz" % item,
              "%(data_dir)s/%(warc_file_base)s.warc.gz" % item)

        shutil.rmtree("%(item_dir)s" % item)


def get_hash(filename):
    with open(filename, 'rb') as in_file:
        return hashlib.sha1(in_file.read()).hexdigest()


CWD = os.getcwd()
PIPELINE_SHA1 = get_hash(os.path.join(CWD, 'pipeline.py'))
LUA_SHA1 = get_hash(os.path.join(CWD, 'blingee.lua'))


def stats_id_function(item):
    # NEW for 2014! Some accountability hashes and stats.
    d = {
        'pipeline_hash': PIPELINE_SHA1,
        'lua_hash': LUA_SHA1,
        'python_version': sys.version,
    }

    return d


class WgetArgs(object):
    def realize(self, item):
        wget_args = [
            WGET_LUA,
            "-U", USER_AGENT,
            "--header", "Accept-Language: en-US,en;q=0.8",
            "-nv",
            "--lua-script", "blingee.lua",
            "-o", ItemInterpolation("%(item_dir)s/wget.log"),
            "--no-check-certificate",
            "--output-document", ItemInterpolation("%(item_dir)s/wget.tmp"),
            "--truncate-output",
            "-e", "robots=off",
            "--rotate-dns",
            "--no-cookies",
            "--no-parent",
            "--timeout", "30",
            "--tries", "inf",
            "--domains", "blingee.com,s3.amazonaws.com,image.blingee.com,image.blingee.com.s3.amazonaws.com",
            "--span-hosts",
            "--waitretry", "30",
            "--warc-file", ItemInterpolation("%(item_dir)s/%(warc_file_base)s"),
            "--warc-header", "operator: Archive Team",
            "--warc-header", "blingee-dld-script-version: " + VERSION,
            "--warc-header", ItemInterpolation("blingee: %(item_name)s")
        ]

        item_name = item['item_name']
        assert ':' in item_name
        item_type, item_value = item_name.split(':', 1)

        item['item_type'] = item_type
        item['item_value'] = item_value

        assert item_type in ('blingee',
                             'stamp',
                             'group',
                             'competition',
                             'challenge',
                             'badge',
                             'profile')

        if item_type == 'blingee':
            for val in xrange(int(item_value), int(item_value)+NUM_BLINGEES):
                wget_args.append("http://blingee.com/blingee/view/{0}".format(val))
                wget_args.append("http://blingee.com/blingee/{0}/comments".format(val))
                wget_args.append("http://bln.gs/b/{0}".format(base36_encode(val)))
        elif item_type == 'stamp':
            for val in xrange(int(item_value), int(item_value)+NUM_STAMPS):
                wget_args.append("http://blingee.com/stamp/view/{0}".format(val))
            urls = stamp_scraper(int(item_value), int(item_value)+NUM_STAMPS)
            wget_args.extend(urls)
        elif item_type == 'group':
            wget_args.extend(["--recursive", "--level=inf"])
            wget_args.append("http://blingee.com/group/{0}".format(item_value))
            wget_args.append("http://blingee.com/group/{0}/members".format(item_value))
        elif item_type == 'competition':
            wget_args.append("http://blingee.com/competition/view/{0}".format(item_value))
            wget_args.append("http://blingee.com/competition/rankings/{0}".format(item_value))
        elif item_type == 'challenge':
            wget_args.append("http://blingee.com/challenge/view/{0}".format(item_value))
            wget_args.append("http://blingee.com/challenge/rankings/{0}".format(item_value))
        elif item_type == 'badge':
            wget_args.append("http://blingee.com/badge/view/{0}".format(item_value))
            wget_args.append("http://blingee.com/badge/winner_list/{0}".format(item_value))
        elif item_type == 'profile':
            for val in xrange(int(item_value), int(item_value)+NUM_PROFILES):
                print("Getting username for ID {0}...".format(val))
                sys.stdout.flush()
                url = "http://blingee.com/badge/view/42/user/{0}".format(val)
                html = get_url(url)
                tree = etree.HTML(html, parser=myparser)
                links = tree.xpath('//div[@id="badgeinfo"]//a/@href')
                username = [link for link in links if "/profile/" in link]
                if not username:
                    print("Skipping deleted/private profile.")
                else:
                    username = username[0]
                    wget_args.append("http://blingee.com{0}".format(username))
                    wget_args.append("http://blingee.com{0}/statistics".format(username))
                    wget_args.append("http://blingee.com{0}/circle".format(username))
                    wget_args.append("http://blingee.com{0}/badges".format(username))
                    wget_args.append("http://blingee.com{0}/comments".format(username))
                    print("Username is {0}".format(username.replace("/profile/", "")))
                    sys.stdout.flush()

        else:
            raise Exception('Unknown item')

        if 'bind_address' in globals():
            wget_args.extend(['--bind-address', globals()['bind_address']])
            print('')
            print('*** Wget will bind address at {0} ***'.format(
                  globals()['bind_address']))
            print('')

        return realize(wget_args, item)

###########################################################################
# Initialize the project.
#
# This will be shown in the warrior management panel. The logo should not
# be too big. The deadline is optional.
project = Project(
    title="blingee",
    project_html="""
        <img class="project-logo" alt="Project logo" src="http://archiveteam.org/images/6/6e/Blingee_logo.png" height="50px" title=""/>
        <h2>blingee.com <span class="links"><a href="http://blingee.com/">Website</a> &middot; <a href="http://tracker.archiveteam.org/blingee/">Leaderboard</a></span></h2>
        <p>Saving all images and content from Blingee.</p>
    """,
    utc_deadline=datetime.datetime(2015, 8, 25, 0, 0, 0)
)

pipeline = Pipeline(
    CheckIP(),
    GetItemFromTracker("http://%s/%s" % (TRACKER_HOST, TRACKER_ID), downloader,
        VERSION),
    PrepareDirectories(warc_prefix="blingee"),
    WgetDownload(
        WgetArgs(),
        max_tries=2,
        accept_on_exit_code=[0, 4, 8],
        env={
            "item_dir": ItemValue("item_dir"),
            "item_value": ItemValue("item_value"),
            "item_type": ItemValue("item_type"),
        }
    ),
    PrepareStatsForTracker(
        defaults={"downloader": downloader, "version": VERSION},
        file_groups={
            "data": [
                ItemInterpolation("%(item_dir)s/%(warc_file_base)s.warc.gz")
            ]
        },
        id_function=stats_id_function,
    ),
    MoveFiles(),
    LimitConcurrent(NumberConfigValue(min=1, max=4, default="1",
        name="shared:rsync_threads", title="Rsync threads",
        description="The maximum number of concurrent uploads."),
        UploadWithTracker(
            "http://%s/%s" % (TRACKER_HOST, TRACKER_ID),
            downloader=downloader,
            version=VERSION,
            files=[
                ItemInterpolation("%(data_dir)s/%(warc_file_base)s.warc.gz")
            ],
            rsync_target_source_path=ItemInterpolation("%(data_dir)s/"),
            rsync_extra_args=[
                "--recursive",
                "--partial",
                "--partial-dir", ".rsync-tmp",
            ]
            ),
    ),
    SendDoneToTracker(
        tracker_url="http://%s/%s" % (TRACKER_HOST, TRACKER_ID),
        stats=ItemValue("stats")
    )
)
