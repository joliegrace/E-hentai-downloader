import asyncio
import aiohttp
import os
import re
import ast
import json
import cgi
import math
import sys
import datetime
import time
import argparse
from bs4 import BeautifulSoup
from os import path
from tqdm import tqdm

CONTENT_TYPE = 'application/x-www-form-urlencoded'
USER_AGENT = 'Mozilla/5.0 (Linux; Android 11; Redmi Note 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/93.0.4577.52 Mobile Safari/537.36'
FORUM_URL = 'https://forums.e-hentai.org/index.php'
GALLERY_URL = 'https://e-hentai.org/g/{}/{}/?p={}'
HOME_URL = 'https://e-hentai.org/home.php/'
API_URL = 'https://api.e-hentai.org/api.php'
LOGIN_ENDPOINT = '?act=Login&CODE=01'
COOKIES = 'cookies.json'
VALID_URL_REGEX = r"https:\/\/e-hentai.org\/g\/(.+?)\/(.+?)\/"
IMAGES_PER_PAGE = 40
COUNT_MSG = "---- get image page links(total files) : {}"
MAX_PAGE_MSG = 'Total page of this gallery is {}'


class Downloader:
    def __init__(self, session):
        self.session = session
        self.headers = get_base_headers()
        self.semaphore = 0
        self.download_path = ''
        self.download_quality = ''
        self.page = 0
        self.max_page = 0
        self.multiple_pages = []
        self.max_retry = 0

    async def get_image_pages(self, api_resp):
        meta_data = json.loads(api_resp)['gmetadata'][0]
        gid = meta_data['gid']
        token = meta_data['token']
        set_names = {'en' : meta_data['title'],
                     'jp' : meta_data['title_jpn']}
        file_count = meta_data['filecount']
        gallery_max_page = math.ceil(int(file_count) / IMAGES_PER_PAGE)
        max_page = gallery_max_page if self.max_page <= 0 else self.max_page
        assert(self.page <= gallery_max_page), MAX_PAGE_MSG.format(gallery_max_page)
        assert(max_page <= gallery_max_page), MAX_PAGE_MSG.format(gallery_max_page)
        assert(self.page <= max_page), 'From page always <= to page'
        image_pages = []

        if len(self.multiple_pages) >= 2:
            for page in self.multiple_pages:
                assert(page <= gallery_max_page), MAX_PAGE_MSG.format(gallery_max_page)
                async with self.session.get(GALLERY_URL.format(gid, token, str(page -1)), headers = self.headers) as resp:
                    image_pages += self.page_parser(await resp.text())
                    print(COUNT_MSG.format(len(image_pages)), end = "\r", flush = True)
        else:
            for page in range(self.page - 1 if self.page >= 1 else 0,max_page):
                async with self.session.get(GALLERY_URL.format(gid, token, str(page)), headers = self.headers) as resp:
                    image_pages += self.page_parser(await resp.text())
                    print(COUNT_MSG.format(len(image_pages)), end = "\r", flush = True)
        return image_pages, set_names

    async def api_request(self, gid, token):
        content_payload = {"method": "gdata",
                           "gidlist": [
                           [gid,"{}".format(token)]
                           ],
                           "namespace": 1}
        async with aiohttp.ClientSession() as session:
            async with session.post(API_URL, headers = self.headers, data = json.dumps(content_payload)) as resp:
                return await resp.text()

    def page_parser(self, resp):
        image_pages = []
        doc = BeautifulSoup(resp, 'html.parser')
        div_gdtm = doc.find_all('div', class_ = 'gdtm')
        for item in div_gdtm:
            a = item.find('a')
            image_pages.append(a['href'])
        return image_pages

    async def image_page_parser(self, image_page_url):
        async with self.session.get(image_page_url, headers = self.headers) as resp:
            doc = BeautifulSoup(await resp.text(), 'html.parser')
            a = doc.find_all('a')
            src = ''
            src_low = ''
            img = doc.find_all('img')
            is_available_src = True
            for c in img:
                if 'src' in c.attrs and '/h/' in c['src']:
                    src_low = c['src']
            for c in a:
                if 'href' in c.attrs and 'fullimg' in c['href']:
                    src = c['href']
            src = src_low if not src or self.download_quality == 'low' else src
            if not src:
                is_available_src = False
            return is_available_src, src

    def create_dir(self, set_names):
        dir_name = set_names['jp'] if not set_names['en'] else set_names['en']
        dir_name = dir_name.translate ({ord(c): "_" for c in "\/:*?<>"})
        dl_path = os.path.join(self.download_path, dir_name + '/')
        os.makedirs(os.path.dirname(dl_path), exist_ok = True)
        self.download_path = dl_path
        
    async def write_file(self, resp, file_path, progress_bar):
        buffer_size = 1024 * 2
        with open(file_path, "wb") as f:
            async for chunk in resp.content.iter_chunked(buffer_size):
                f.write(chunk)
                progress_bar.update(len(chunk))

    async def download(self, image_page, page):
        is_available_src, url = await self.image_page_parser(image_page)
        max_retry = self.max_retry + 1 if self.max_retry >= 1 else 2
        attemps = 0
        success = False
        if not is_available_src:
            print('Cannot find source of this picture !')
        else :
            while not success and attemps < max_retry:
                try:
                    async with self.session.get(url , headers = self.headers) as resp:
                        if self.is_valid_image(resp):
                            file_size = int(resp.headers.get("Content-Length" , 0))
                            default_filename = self.get_filename(url)
                            content_disposition = resp.headers.get("Content-Disposition")
                            if content_disposition:
                                value , params = cgi.parse_header(content_disposition)
                                filename = params.get("filename" , default_filename)
                            else :
                                filename = default_filename
                            progress = tqdm(desc = f" {'Retry>' + str(attemps) +'>' if attemps >= 1 else ''}File {filename},page {page}", total = file_size, unit = "B", unit_scale = True, unit_divisor = 1024)
                            await self.write_file(resp, os.path.join(self.download_path, filename), progress)
                            success = True
                        else:
                            success = True
                            if await self.is_exceeded(resp):
                                print(f"\n\n\n\nYou have exceeded your image viewing limits. Reset at {HOME_URL} and all download process will stop here !\n\n\n\n")
                                for task in asyncio.all_tasks():
                                    task.cancel()
                            else:
                                print('\nFailed !\n')
                except Exception as e:
                    attemps += 1
                    await asyncio.sleep(2)
            if not success:
                print('\nFailed !\n')

    async def _download(self, url, page, sem):
        async with sem:
            await self.download(url, page)

    async def start_downloads(self, image_pages):
        mul_page_list = self.multiple_pages
        index = 1
        index_2 = 0
        page = 1
        tasks = []
        semaphore = asyncio.Semaphore(self.semaphore)
        max_page = math.ceil(len(image_pages) / IMAGES_PER_PAGE)
        if self.page >= 1:
            page = self.page
        elif len(mul_page_list) >= 2:
            page = mul_page_list[index_2]
        for image_link in image_pages:
            task = asyncio.create_task(self._download(image_link, page, semaphore))
            tasks.append(task)
            if index >= IMAGES_PER_PAGE:
                index = 0
                index_2 += 1
                if index_2 >= max_page:
                    index_2 = index_2 - 1
                if len(mul_page_list) >= 2:
                    page = mul_page_list[index_2]
                else :
                    page += 1
            index += 1
        try:
            await asyncio.wait(tasks)
        except asyncio.CancelledError:
            pass
        except ValueError:
            print('Error !')

    def get_filename(self, url):
        fragment_removed = url.split("#")[0]
        query_string_removed = fragment_removed.split("?")[0]
        scheme_removed = query_string_removed.split("://")[-1].split(":")[-1]
        if scheme_removed.find("/") == -1:
            return ""
        return path.basename(scheme_removed)

    def is_valid_image(self, resp):
        if resp.status < 400 and 'Content-Type' in resp.headers and 'image' in resp.headers['Content-Type']:
            return True
        else:
            return False

    async def is_exceeded(self, resp):
        if resp.status < 400 and 'Content-Type' in resp.headers and 'text' in resp.headers['Content-Type']:
            if 'You have exceeded your image viewing limits' in await resp.text():
                return True
            else :
                return False
        else :
            return False


class LoginCheck:
    def __init__(self):
        pass

    def is_login(self,resp):
        regex = r"<b>Logged\sin\sas:"
        isMatchs = re.search(regex , str(resp), re.MULTILINE)
        user = ''
        if isMatchs:
            doc = BeautifulSoup(resp, 'html.parser')
            p = doc.find('p', class_ = 'home')
            a = p.find_all('a')
            for c in a:
                if 'href' in c.attrs and 'showuser=' in c['href']:
                    user = c.string
        return user
        
    def login_success(self,resp):
        regex = r"logged\sin\sas:"
        return re.search(regex, str(resp), re.MULTILINE)
        
    def user_pass_incorrect(self,resp):
        regex = r">Username\sor\spassword\sincorrect"
        return re.search(regex, str(resp), re.MULTILINE)

async def main():
    args = init_args()
    if os.path.exists(COOKIES):
        cookies_f = open(COOKIES, "r").read()
        if not cookies_f:
            print('   cookies is empty, login again !')
            await login()
        else :
            cookies = ast.literal_eval(cookies_f)
            async with aiohttp.ClientSession(cookies = cookies) as session:
                async with session.get(FORUM_URL, headers = get_base_headers()) as resp:
                    user = LoginCheck().is_login(await resp.text())
                    if not user:
                        print('   Cannot find any infomation of user is login ! or something like cookie has expried ! login again')
                        await login()
                    else:
                        print(f'------------------- Hello {user}')
                        await print_imagelimits(session)
                        await starting(args, session)
    else :
        await login()

async def starting(args, session):
    url = args.url
    url = url + '/' if not url.endswith('/') else url
    assert is_valid_url(url),"Invalid URL !"
    downloader = Downloader(session)
    downloader.semaphore = args.num_proc
    downloader.download_path = args.save_path
    downloader.download_quality = args.quality
    downloader.max_retry = args.retry
    if args.single is not None:
        downloader.page = args.single
        downloader.max_page = args.single
    if args.from_page is not None:
        downloader.page = args.from_page
    if args.to_page is not None:
        downloader.max_page = args.to_page
    if args.multiple is not None:
        if len(args.multiple) <= 1:
            print('  better you should typing -s / --single to download only one page :)')
            sys.exit()
        else:
            downloader.multiple_pages = args.multiple
    gid, token = get_gid_token(url)
    print(' Get Gallery metadata from API....')
    api_resp = await downloader.api_request(gid,token)
    print_metadata(api_resp)
    image_pages, set_name = await downloader.get_image_pages(api_resp)
    downloader.create_dir(set_name)
    await downloader.start_downloads(image_pages)

async def login():
    loginCheck = LoginCheck()
    print('   Login to E-hentai with username & password')
    username = input('>>>Username: ')
    password = input('>>>Password: ')
    login_headers = get_base_headers()
    login_headers['CONTENT-TYPE'] = CONTENT_TYPE
    login_form = {"UserName" : username,
                  "PassWord" :password,
                  "CookieDate": str(1)}
    print('    Authenticated Login.....')
    async with aiohttp.ClientSession() as session:
        async with session.post(FORUM_URL + LOGIN_ENDPOINT, headers = login_headers, data = login_form) as login_resp:
            if loginCheck.login_success(await login_resp.text()):
                print('   Login success, run program again')
                cookies = login_resp.cookies
                cookies_d = {}
                for key, cookie in cookies.items():
                    cookies_d[cookie.key] = cookie.value
                if os.path.exists(COOKIES):
                    os.remove(COOKIES)
                f = open(COOKIES, "a")
                f.write(str(cookies_d))
                f.close()
            elif loginCheck.user_pass_incorrect(await login_resp.text()):
                print('   Username & Password incorrect !')
                await login()
            else :
                print('   Login failed !')
                await login()

async def print_imagelimits(session):
    async with session.get(HOME_URL, headers = get_base_headers()) as resp:
        doc = BeautifulSoup(await resp.text(), 'html.parser')
        div_homebox = doc.find('div', class_ = 'homebox')
        strong = div_homebox.find_all('strong')
        current = int(strong[0].string)
        limit = int(strong[1].string)
        cost = int(strong[2].string)
        print(f"-------------	Your image limits: {current}/ {limit} | Reset cost: {cost} at {HOME_URL}")
        if current > limit:
            print(f"Impossible to countinue downloads any image because you has been reached limits images you can browse. Reset cost: {cost} at {HOME_URL}")
            sys.exit()

def print_metadata(api_resp):
    meta_data = json.loads(api_resp)['gmetadata'][0]
    if 'error' in meta_data.keys():
        print(meta_data['error'])
        sys.exit()
    title = meta_data['title']
    title_jp = meta_data['title_jpn']
    category = meta_data['category']
    uploader = meta_data['uploader']
    posted_timestamp = meta_data['posted']
    posted = datetime.datetime.fromtimestamp(int(posted_timestamp)).strftime('%a,%d %b %Y')
    filecount = meta_data['filecount']
    page = math.ceil(int(filecount) / IMAGES_PER_PAGE)
    filesize_bytes = meta_data['filesize']
    filesize = human_readable_bytes(filesize_bytes)
    rating = meta_data['rating']
    torrentcount = meta_data['torrentcount']
    
    general_metadata = f"   Gallery name : {title_jp if not title else title} \n   Category : {category} \n   Uploader : {uploader} \n   Posted : {posted} \n   Total Files : {filecount} \n   Total Page : {page} \n   Gallery Size : {filesize} \n   Rating : {rating} \n   Torrent : {torrentcount}"
    print(general_metadata)
    if int(torrentcount) > 0:
        print('   Torrents :')
        torrents = meta_data['torrents']
        for item in torrents:
            hash = item['hash']
            added_timestamp = item['added']
            added = datetime.datetime.fromtimestamp(int(added_timestamp)).strftime('%a,%d %b %Y')
            name = item['name']
            tsize = human_readable_bytes(int(item['tsize']))
            fsize = human_readable_bytes(int(item['fsize']))
            torrent_format = f"\n	hash : {hash} \n	added : {added} \n	name : {name} \n	torrent size : {tsize} \n	file size : {fsize} \n"
            print(torrent_format)

def human_readable_bytes(size_bytes):
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return "%s %s" % (s, size_name[i])

def is_valid_url(url):
    return re.search(VALID_URL_REGEX, url, re.IGNORECASE)

def get_gid_token(url):
    matchs = re.search(VALID_URL_REGEX, url, re.IGNORECASE)
    gid = matchs.group(1)
    token = matchs.group(2)
    return gid, token

def get_base_headers():
    base_headers = {'USER-AGENT':USER_AGENT}
    return base_headers

def init_args():
    parser = argparse.ArgumentParser(description = "-------------	E-Hentai Gallery Downloader	-----------", usage = "%(prog)s [URL] [GALLERY DOWNLOAD OPTIONS] [FILE SAVED OPTIONS]")
    parser.add_argument("url", type = str, help = "E-hentai gallery url")
    download_group = parser.add_argument_group("Gallery download options")
    group_1 = download_group.add_mutually_exclusive_group()
    arg_s = group_1.add_argument("-s", "--single", type = int, metavar = "PAGE", help = "Download single page (e.g., -s or --single 15)")
    arg_m = group_1.add_argument("-m", "--multiple",type = int, nargs = '+', metavar = "PAGES" , help = "Download multiple pages[list] (e.g., -m or --multiple 15 20 25 30 35...)")
    group_1.add_argument("-f", "--from-page", type = int, metavar = "PAGE", help = "Download from page (e.g., -f or --from-page 15)")
    
    group_2 = download_group.add_mutually_exclusive_group()
    group_2.add_argument("-t", "--to-page", type = int, metavar = "PAGE", help = "Download to page(e.g., -t or --to-page 15)")
    group_2._group_actions.append(arg_s)
    group_2._group_actions.append(arg_m)
    
    file_group = parser.add_argument_group("File saved options")
    file_group.add_argument("-r", "--retry", type = int, help = "Retry download attemps when error, default 1", default = 1)
    file_group.add_argument("-S", "--save-path", help = "The downloads path", default = './Downloads')
    file_group.add_argument("-q", "--quality", choices = ["high","low"], help = "Choose quality when download [high or low],default is high but some pictures no highres will auto download with lowres", default = "high")
    file_group.add_argument("-n", "--num-proc", type = int, help = "The number of semaphore(default 4, download process will downloading 4 image at same time) ", default = 4)
    
    args = parser.parse_args()
    return args

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass