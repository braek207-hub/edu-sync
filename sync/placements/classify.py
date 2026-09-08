# -*- coding: utf-8 -*-
"""Классификатор площадок РСЯ: имя → вердикт.

Ядро чистильщика. Судит площадку ПО ИМЕНИ, а не по деньгам, и это выбор,
а не упрощение: спам-инвентарь показывает конверсии Директа, которые выглядят
живыми, и статистический критерий такую площадку выгораживает (решение Павла
06.09.2026). Деньги решают не «кого резать», а «кого резать сейчас» — этим
занимаются ворота в plan.py.

Пять вердиктов:
  app   — мобильное приложение, которого нет в allowlist → резать
  dsp   — обменник трафика, перепроданный инвентарь        → резать
  game  — игровой инвентарь                                 → резать
  junk  — мусорная зона или мусорный сайт                   → резать
  keep  — приложение из allowlist                           → оставить
  site  — обычный сайт                                      → оставить

Обычные сайты не трогаем вовсе: mail.ru у EDU сливает 2.59 млн ₽ за две
недели, но мусором по имени не является, и решение по нему — за человеком.
"""

import os
import re
from typing import Dict, Set, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_TLDS_PATH = os.path.join(_HERE, "data", "tlds.txt")


def _load_tlds() -> Set[str]:
    """Доменные зоны IANA — единственный способ отличить домен от bundle id.

    Список лежит в репозитории, а не тянется из сети: чистильщик ходит по
    крону, и внешний недоступный ресурс превратил бы каждую площадку в
    «приложение» — то есть отрезал бы живые сайты целиком.
    """
    with open(_TLDS_PATH, encoding="utf-8") as fh:
        return {ln.strip().lower() for ln in fh
                if ln.strip() and not ln.startswith("#")}


TLDS = _load_tlds()

# Обменники и DSP: инвентарь перепродан через биржу, качество не
# контролирует никто. Режется всегда и у всех клиентов.
DSP_TOKENS = frozenset({
    "dsp", "ssp", "rtb", "exchange", "adexchange", "programmatic", "adfox",
    "betweendigital", "otm", "soloway", "getintent", "mediasniper", "hybrid",
    "bidswitch", "smaato", "mopub", "mobfox", "inneractive", "adsterra",
    "propellerads", "adcash", "rtbhouse", "admixer", "adriver", "adnetwork",
})

GAME_TOKENS = frozenset({
    "game", "games", "gaming", "igra", "igry", "arcade", "puzzle", "puzzles",
})

# Зоны, на которых сидит спам-инвентарь: регистрация копеечная, репутации нет.
JUNK_TLDS = frozenset({
    "xyz", "top", "click", "icu", "buzz", "win", "loan", "cyou", "sbs", "cfd",
    "rest", "quest", "bar", "monster", "boats", "beauty", "hair", "skin",
    "makeup", "lol", "cam", "download", "stream", "date", "faith", "review",
    "racing", "party", "science", "gdn", "work", "men", "mom", "uno", "kim",
})

# Мусор по содержанию: пиратка, adult, знакомства, гадания, обои, взлом,
# VPN и «чистилки». Слово ищется как отдельный сегмент имени, не подстрокой.
JUNK_TOKENS = frozenset({
    "torrent", "torrents", "rutracker", "kinogo", "hdrezka", "rezka", "filmix",
    "seasonvar", "kinokrad", "lordfilm", "zetflix", "flixtor", "kinopoisk720",
    "porn", "xxx", "sex", "erotic", "erotika", "hentai", "seks", "intim",
    "znakomstva", "dating", "znakomstvo", "flirt", "sexdate",
    "goroskop", "gadanie", "gadalka", "magiya", "ezoterika", "taro", "privorot",
    "oboi", "wallpaper", "wallpapers", "avatarka", "avatarki",
    "chit", "chity", "cheat", "cheats", "vzlom", "warez", "crack", "keygen",
    "apk", "modapk", "vpn", "proxy", "cleaner", "booster", "antivirus",
    "emulator", "emulyator", "flashlight", "fonarik", "screenrecorder",
})

# Известные приложения, которые мусором не являются. ТОЧНЫЕ bundle id:
# подстрока здесь недопустима — в живых данных EDU есть vk.baraholka.russia,
# и по подстроке «vk» барахолка прошла бы как ВКонтакте.
ALLOW_EXACT = frozenset({
    # Одно приложение живёт под двумя нотациями: у ВК Видео и Авито в
    # AppGallery/Google Play префиксы com.*, а не ru.*, и списка ru.* мало —
    # аудит запретов 08.09.2026 нашёл com.avito.android в пяти кампаниях.
    "com.vkontakte.android", "com.vk.vkclient", "com.vk.im", "com.vk.music",
    "com.vk.vkvideo.prod", "com.vk.calls", "com.avito.android",
    "com.rutube.app",
    "ru.ok.android", "ru.ok.messages", "ru.odnoklassniki.iphone",
    "ru.zen.android",
    "ru.mail.mailapp", "ru.mail.mail", "ru.mail.cloud",
    "ru.mail.search.electroscope",
    "ru.avito.android", "ru.auto.ara", "ru.drom.app",
    "com.farpost.dromfilter", "ru.rutube.app",
    "ru.wildberries.wbclient", "com.wildberries.ru",
    "ru.ozon.app.android", "ru.beru.android", "ru.megamarket.android",
    "com.lamoda.android", "com.aliexpress.aer",
    "ru.dublgis.dgismobile", "ru.kinopoisk", "ru.ivi.client", "ru.more.play",
    "ru.hh.android", "com.cian.mobile", "ru.domclick.mortgage",
    "com.idamob.tinkoff.android", "ru.tinkoff.mobile", "ru.sberbankmobile",
    "ru.vtb24.mobilebanking.android", "ru.alfabank.mobile.android",
    "ru.gosuslugi.eds", "ru.rt.mlk", "com.gismeteo.Gismeteo",
})

# Неймспейсы издателей. Префикс здесь безопасен, в отличие от подстроки:
# bundle id в сторах уникален, занять чужой неймспейс нельзя.
ALLOW_PREFIX = ("ru.yandex.", "com.yandex.", "ru.sberbank", "ru.alfabank.",
                "ru.vtb", "com.google.android.")

# Приложения, которые СТРУКТУРНО неотличимы от домена: имя из двух сегментов,
# и оба — настоящие зоны IANA. video.like — это Likee, но «.video» и «.like»
# обе зарегистрированы, так что признак обратной нотации здесь молчит, и
# никакое правило по форме имени такой случай не разрешит. Список ведётся
# руками и пополняется, когда очередное имя всплывает в отчёте.
KNOWN_APPS = frozenset({
    "video.like", "video.tiktok", "app.buzz", "news.app",
})

# Корни обратной нотации: с чего РЕАЛЬНО начинается bundle id. Список
# закрытый, и это принципиально. Первая версия правила считала нотацией
# любой первый сегмент, совпавший с какой-нибудь зоной IANA, — и на живом
# кабинете Russever записала в приложения win.mail.ru, поддомен Mail.ru,
# потому что «.win» зарегистрирована. Ложный срез домена молча отрезает
# живой трафик, поэтому здесь перечисление, а не проверка по TLDS.
NOTATION_ROOTS = frozenset({
    "com", "ru", "org", "net", "io", "me", "app", "dev", "co", "eu",
    "info", "biz", "mobi", "name", "pro", "tv",
    "jp", "kr", "cn", "de", "uk", "us", "ai", "fr", "it", "es", "pl", "nl",
    "se", "no", "fi", "dk", "cz", "at", "ch", "be", "pt", "gr", "hu", "ro",
    "br", "in", "au", "ca", "mx", "tr", "ua", "by", "kz", "il", "sg", "hk",
    "tw", "th", "vn", "id", "ph", "my", "nz", "za", "ar", "cl", "pe",
})

# Классические зоны, в которых живут САЙТЫ. Если имя кончается на такую
# зону, оно домен, и правило обратной нотации к нему не применяется.
CORE_TLDS = frozenset({
    "ru", "com", "net", "org", "info", "biz", "pro", "su", "xn--p1ai",
    "ua", "by", "kz", "uz", "am", "ge", "md", "kg", "tj", "az",
    "de", "uk", "fr", "it", "es", "pl", "nl", "cz", "tr", "cn", "jp", "kr",
    "us", "ca", "au", "in", "br", "eu", "co", "cc", "tv", "me", "online",
    "site", "shop", "store", "club", "space", "website", "fun", "life",
    "world", "news", "media", "digital", "blog", "art", "tech", "agency",
})

# «llm» ставит второй судья (llm.py) поверх словаря: сайт правильной формы,
# который модель опознала как дорвей, помойку или пиратку.
CUT_VERDICTS = frozenset({"app", "dsp", "game", "junk", "llm"})


def normalize(name: str) -> str:
    """Имя площадки в каноническом виде.

    Хвостовой слэш снимается: справка Директа записывает приложения как
    «com.block.juggle/», а отчёт отдаёт их без него.
    """
    return str(name or "").strip().lower().rstrip("/")


def _segments(name: str):
    return [s for s in name.split(".") if s]


_WORD_SPLIT = re.compile(r"[.\-_]+")


def _tokens(name: str) -> Set[str]:
    """Слова имени: сегменты, разбитые ещё и по дефису с подчёркиванием.

    Обменники подписываются составным поддоменом — dsp-opera-exchange.yandex.ru,
    dsp-inneractive.yandex.ru, dsp-yeahmobi.yandex.ru. По одним точкам такой
    поддомен — единый сегмент, и словарь его не узнаёт: на Russever 07.09.2026
    из шести dsp-площадок в топе распознавалась одна (dsp.yandex.ru), остальные
    шли как «обычный сайт» и оставались откручиваться.
    """
    return {t for t in _WORD_SPLIT.split(name) if t}


def is_app(name: str) -> bool:
    """Bundle id мобильного приложения против доменного имени.

    Основной признак — последний сегмент не является доменной зоной:
    com.dom.home, video.like, ru.zen.android, pdf.office.doc.reader.editor.
    Проверено на живых отчётах EDU и Russever, ошибок нет.

    Второй признак нужен bundle id, чей хвост случайно совпал с зоной
    (ai.character.app, jp.ne.ibis.ibispaintx.app — .app зона настоящая).
    Он намеренно узкий: корень берётся из закрытого списка NOTATION_ROOTS,
    а имя, оканчивающееся классической зоной сайта, под нотацию не идёт
    вовсе. Иначе win.mail.ru уезжает в приложения — «.win» зарегистрирована.
    """
    if name in KNOWN_APPS:
        return True
    segs = _segments(name)
    if len(segs) < 2:
        return False
    if segs[-1] not in TLDS:
        return True
    return (len(segs) >= 3
            and segs[0] in NOTATION_ROOTS
            and segs[-1] not in CORE_TLDS)


def classify(name: str,
             allow_exact: Set[str] = ALLOW_EXACT,
             allow_prefix: Tuple[str, ...] = ALLOW_PREFIX) -> Tuple[str, str]:
    """Имя площадки → (вердикт, причина словами).

    Причина едет в журнал и в Telegram: запрет, который нельзя объяснить
    человеку одной строкой, нельзя и проверить.
    """
    site = normalize(name)
    if not site:
        return "junk", "пустое имя площадки"

    segs = _tokens(site)

    # DSP и game проверяются ДО приложений: dsp.yandex и game.yandex по
    # структуре имени выглядят как bundle id, но резать их надо с их
    # собственной причиной, иначе журнал соврёт о том, что произошло.
    hit = segs & DSP_TOKENS
    if hit:
        return "dsp", "обменник трафика: %s" % ", ".join(sorted(hit))

    hit = segs & GAME_TOKENS
    if hit:
        return "game", "игровой инвентарь: %s" % ", ".join(sorted(hit))

    if is_app(site):
        if site in allow_exact:
            return "keep", "известное приложение"
        for prefix in allow_prefix:
            if site.startswith(prefix):
                return "keep", "приложение доверенного издателя (%s)" % prefix
        hit = segs & JUNK_TOKENS
        if hit:
            return "app", "мусорное приложение: %s" % ", ".join(sorted(hit))
        return "app", "мобильное приложение"

    tld = _segments(site)[-1]
    if tld in JUNK_TLDS:
        return "junk", "мусорная зона .%s" % tld
    hit = segs & JUNK_TOKENS
    if hit:
        return "junk", "мусорный сайт: %s" % ", ".join(sorted(hit))
    return "site", "обычный сайт"


def classify_all(names) -> Dict[str, Tuple[str, str]]:
    return {normalize(n): classify(n) for n in names if normalize(n)}
