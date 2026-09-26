"""
Discord 遊戲情報 Bot

  /game <遊戲名稱>  從聯合新聞網・遊戲角落搜尋這款遊戲，列出最新幾篇情報

環境變數：
  DISCORD_TOKEN  必填
  SEARCH_URL     搜尋網址，{q} 會換成遊戲名稱（預設 https://game.udn.com/game/search/{q}）
"""
import os
import re
import asyncio
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
SEARCH_URL = os.getenv("SEARCH_URL", "https://game.udn.com/game/search/{q}")
# 文章網址：/game/story/<分類>/<文章編號> 或 /news/story/<分類>/<文章編號>，文章編號越大越新
STORY_RE = re.compile(r"^/(?:game|news)/story/\d+/(\d+)$")
HEADERS = {
    # 有些新聞網站會擋自稱機器人的請求，所以用一般瀏覽器的標頭
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}
MAX_PAGE_BYTES = 2_000_000


async def fetch_html(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url) as resp:
        if resp.status != 200:
            raise RuntimeError(f"網頁回應 HTTP {resp.status}")
        raw = await resp.content.read(MAX_PAGE_BYTES)
        return raw.decode(resp.charset or "utf-8", errors="replace")


class LinkParser(HTMLParser):
    """收集所有 <a href> 與連結裡的文字（當作標題的備案）。"""

    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), []

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None

    def handle_data(self, data):
        if self._href:
            self._text.append(data)


class MetaParser(HTMLParser):
    """文章頁的 og:title、og:description、og:image 與發布時間。"""

    KEYS = ("og:title", "og:description", "description", "og:image", "article:published_time", "date")

    def __init__(self):
        super().__init__()
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            a = dict(attrs)
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key == "datepublished":
                key = "article:published_time"
            if key in self.KEYS and a.get("content"):
                self.meta.setdefault(key, a["content"].strip())


def find_articles(html: str, base: str, limit: int) -> list[tuple[str, str]]:
    """搜尋結果頁 → [(文章網址, 連結文字)]，依文章編號由新到舊。"""
    parser = LinkParser()
    parser.feed(html)
    found: dict[str, tuple[int, str, str]] = {}
    for href, text in parser.links:
        u = urlparse(urljoin(base, href))
        m = STORY_RE.match(u.path)
        if not m or not (u.hostname or "").endswith("udn.com"):
            continue
        url = f"https://{u.hostname}{u.path}"  # 去掉追蹤參數
        old = found.get(url)
        if not old or len(text) > len(old[1]):  # 同一篇常有圖片、標題兩個連結，留文字較長的
            found[url] = (int(m.group(1)), text, url)
    ranked = sorted(found.values(), key=lambda x: -x[0])
    return [(url, text) for _, text, url in ranked[:limit]]


async def article_embed(session: aiohttp.ClientSession, url: str, fallback_title: str) -> discord.Embed:
    meta: dict[str, str] = {}
    try:
        p = MetaParser()
        p.feed(await fetch_html(session, url))
        meta = p.meta
    except Exception as e:
        print(f"[game] 讀取 {url} 失敗：{e!r}")
    title = (meta.get("og:title") or fallback_title or url).split(" | ")[0].strip()
    desc = meta.get("og:description") or meta.get("description") or ""
    embed = discord.Embed(title=title[:256], url=url, description=desc[:300], color=discord.Color.blurple())
    published = meta.get("article:published_time") or meta.get("date")
    if published:
        try:
            embed.timestamp = datetime.fromisoformat(published.replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            embed.set_footer(text=published)
    if meta.get("og:image"):
        embed.set_thumbnail(url=meta["og:image"])
    return embed


async def latest_news(game: str, count: int) -> tuple[str, list[discord.Embed]]:
    search_url = SEARCH_URL.format(q=quote(game))
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as session:
        articles = find_articles(await fetch_html(session, search_url), search_url, count)
        print(f"[game] {game!r} 找到 {len(articles)} 篇")
        embeds = await asyncio.gather(*(article_embed(session, url, text) for url, text in articles))
    return search_url, list(embeds)


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def setup_hook():
    await bot.tree.sync()


@bot.event
async def on_ready():
    print(f"已登入：{bot.user}")


@bot.tree.command(name="game", description="查某款遊戲的最新情報（聯合新聞網・遊戲角落）")
@app_commands.describe(name="遊戲名稱，例如 艾爾登法環", count="要看幾篇（預設 5）")
async def game(interaction: discord.Interaction, name: app_commands.Range[str, 1, 50],
               count: app_commands.Range[int, 1, 10] = 5):
    await interaction.response.defer(thinking=True)
    name = name.strip()
    try:
        search_url, embeds = await latest_news(name, count)
    except Exception as e:
        print(f"[game] 錯誤：{e!r}")
        await interaction.followup.send(f"⚠️ 查詢失敗：{str(e)[:300]}")
        return
    if not embeds:
        await interaction.followup.send(f"📭 遊戲角落找不到「{name}」的相關文章。")
        return
    await interaction.followup.send(f"🎮 **{name}** 最新 {len(embeds)} 篇情報（[遊戲角落搜尋結果](<{search_url}>)）",
                                    embeds=embeds)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
