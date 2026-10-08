#!/usr/bin/env python3
"""
Monitor de stock — Pokémon TCG 30th Celebration / 30th Anniversary.

Verifica todas as lojas em stores.csv e envia notificação push (ntfy e/ou
Telegram) quando um produto que corresponde às palavras-chave passa a estar
disponível. Guarda o estado em state.json para só avisar em mudanças.

Deteta automaticamente a plataforma de cada loja:
  shopify  -> /search/suggest.json (campo "available") ou /products.json
  woo      -> WooCommerce Store API (/wp-json/wc/store/v1/products)
  presta   -> pesquisa AJAX do PrestaShop (JSON)
  html     -> página de pesquisa + heurística de texto (menos fiável)
"""
import csv
import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- configuração
SEARCH_QUERIES = ["30th celebration", "30th anniversary", "30 aniversario"]

# Um título conta se corresponder a QUALQUER um destes padrões (já normalizado:
# minúsculas, sem acentos).
MATCH_PATTERNS = [
    r"30th\s*celebration",
    r"30th\s*anniversary",
    r"30\s*(o|º)?\s*aniversario",
    r"30\s*celebration",
    r"\bm6a\b",
]

# Ignorar cartas avulsas e acessórios (evita spam). Edita à vontade.
EXCLUDE_PATTERNS = [
    r"\b\d{1,3}\s*/\s*\d{2,3}\b",      # numeração de carta tipo 025/160
    r"\bsingle\b", r"\bavuls", r"\bgraded\b", r"\bpsa\s*\d", r"\bcgc\b",
    r"\bsleeves?\b", r"\bprotetor", r"\bbinder\b", r"\bplaymat\b",
]
# Põe ONLY_POKEMON=0 nas variáveis do workflow para aceitar outras marcas.
ONLY_POKEMON = os.environ.get("ONLY_POKEMON", "1") == "1"

TIMEOUT = 20
WORKERS = 12
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
REPORT_FILE = os.path.join(HERE, "report.md")
STORES_FILE = os.path.join(HERE, "stores.csv")

OUT_WORDS = re.compile(r"esgotad|sem stock|sem estoque|out of stock|sold out|"
                       r"indisponivel|nao disponivel|agotado|esgotou|"
                       r"brevemente|avisa-me|avise-me|notify me", re.I)
IN_WORDS = re.compile(r"adicionar ao carrinho|add to cart|comprar|em stock|"
                      r"in stock|disponivel|anadir al carrito|pre-?venda|"
                      r"pre-?reserva|pre-?order", re.I)


# ------------------------------------------------------------------- utilidades
def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()


def is_match(title, extra=""):
    t = norm(title)
    if not any(re.search(p, t) for p in MATCH_PATTERNS):
        return False
    if any(re.search(p, t) for p in EXCLUDE_PATTERNS):
        return False
    if ONLY_POKEMON:
        ctx = t + " " + norm(extra)
        # "30th celebration" é praticamente exclusivo de Pokémon; para
        # "30th anniversary" exige-se menção a Pokémon algures.
        if "celebration" not in t and "m6a" not in t and "pok" not in ctx:
            return False
    return True


def session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA,
                      "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8"})
    return s


def get_json(s, url, **kw):
    r = s.get(url, timeout=TIMEOUT, **kw)
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


# --------------------------------------------------------------- por plataforma
def check_shopify(s, base):
    items = {}
    for q in SEARCH_QUERIES:
        data = get_json(s, f"{base}/search/suggest.json", params={
            "q": q, "resources[type]": "product", "resources[limit]": 10,
            "resources[options][unavailable_products]": "show"})
        prods = (((data or {}).get("resources") or {}).get("results") or {}).get("products")
        if prods is None:
            break
        for p in prods:
            if is_match(p.get("title", ""), p.get("vendor", "") + " " + p.get("type", "")):
                url = urljoin(base, p.get("url", "").split("?")[0])
                items[url] = {"title": p["title"], "url": url,
                              "available": bool(p.get("available")),
                              "price": p.get("price")}
    else:
        return items
    # suggest.json desligado -> percorre o catálogo (até 8 páginas x 250)
    for page in range(1, 9):
        data = get_json(s, f"{base}/products.json", params={"limit": 250, "page": page})
        prods = (data or {}).get("products") or []
        for p in prods:
            if is_match(p.get("title", ""), p.get("vendor", "") + " " +
                        p.get("product_type", "") + " " + " ".join(p.get("tags", []))):
                url = f"{base}/products/{p['handle']}"
                v = p.get("variants") or [{}]
                items[url] = {"title": p["title"], "url": url,
                              "available": any(x.get("available") for x in v),
                              "price": v[0].get("price")}
        if len(prods) < 250:
            break
    return items


def check_woo(s, base):
    items = {}
    for q in SEARCH_QUERIES:
        data = get_json(s, f"{base}/wp-json/wc/store/v1/products",
                        params={"search": q, "per_page": 50})
        if not isinstance(data, list):
            continue
        for p in data:
            title = BeautifulSoup(p.get("name", ""), "html.parser").get_text()
            cats = " ".join(c.get("name", "") for c in p.get("categories", []))
            if is_match(title, cats):
                url = p.get("permalink")
                price = (p.get("prices") or {}).get("price")
                minor = (p.get("prices") or {}).get("currency_minor_unit", 2)
                if price and str(price).isdigit():
                    price = f"{int(price) / 10 ** minor:.2f}"
                items[url] = {"title": title, "url": url, "price": price,
                              "available": bool(p.get("is_in_stock")) and
                              bool(p.get("is_purchasable", True))}
    return items


def check_presta(s, base):
    items = {}
    for q in SEARCH_QUERIES:
        data = get_json(s, f"{base}/index.php", params={
            "controller": "search", "s": q, "ajax": 1, "action": "productSearch",
            "resultsPerPage": 50},
            headers={"Accept": "application/json",
                     "X-Requested-With": "XMLHttpRequest"})
        prods = (data or {}).get("products")
        if not isinstance(prods, list):
            continue
        for p in prods:
            title = p.get("name", "")
            if is_match(title, p.get("category_name", "") + " " + p.get("manufacturer_name", "")):
                url = p.get("url") or p.get("link")
                avail = p.get("availability")
                ok = bool(p.get("add_to_cart_url")) or avail in ("available", "last_remaining_items")
                if avail == "unavailable":
                    ok = False
                items[url] = {"title": title, "url": url, "available": ok,
                              "price": p.get("price")}
    return items


HTML_SEARCH_PATHS = ["/search?q={q}", "/?s={q}&post_type=product",
                     "/pesquisa?controller=search&s={q}",
                     "/catalogsearch/result/?q={q}", "/pesquisa?q={q}",
                     "/search?query={q}"]


def check_html(s, base, path_hint=None):
    items = {}
    paths = [path_hint] if path_hint else HTML_SEARCH_PATHS
    used = None
    for path in paths:
        for q in SEARCH_QUERIES[:2]:
            url = base + path.format(q=quote_plus(q))
            try:
                r = s.get(url, timeout=TIMEOUT)
            except requests.RequestException:
                continue
            if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                continue
            used = path
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                title = a.get_text(" ", strip=True) or a.get("title", "")
                if not title or len(title) > 200 or not is_match(title, "pokemon"):
                    continue
                box = a
                for _ in range(4):  # sobe até ao "cartão" do produto
                    if box.parent is None:
                        break
                    box = box.parent
                    if len(box.get_text(" ", strip=True)) > 60:
                        break
                txt = norm(box.get_text(" ", strip=True))
                if OUT_WORDS.search(txt):
                    avail = False
                elif IN_WORDS.search(txt):
                    avail = True
                else:
                    avail = None  # desconhecido
                href = urljoin(base, a["href"]).split("#")[0]
                prev = items.get(href)
                if prev is None or (avail and not prev["available"]):
                    items[href] = {"title": title, "url": href,
                                   "available": avail, "price": None}
        if used:
            break
    return items, used


def detect(s, base):
    d = get_json(s, f"{base}/products.json", params={"limit": 1})
    if isinstance(d, dict) and "products" in d:
        return "shopify"
    d = get_json(s, f"{base}/wp-json/wc/store/v1/products", params={"per_page": 1})
    if isinstance(d, list):
        return "woo"
    d = get_json(s, f"{base}/index.php", params={
        "controller": "search", "s": "pokemon", "ajax": 1, "action": "productSearch"},
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
    if isinstance(d, dict) and isinstance(d.get("products"), list):
        return "presta"
    return "html"


def check_store(name, domain, known_method):
    s = session()
    base = "https://" + domain
    method = known_method
    try:
        if not method or method.startswith("html") or method == "error":
            method = detect(s, base)
        if method == "shopify":
            items = check_shopify(s, base)
        elif method == "woo":
            items = check_woo(s, base)
        elif method == "presta":
            items = check_presta(s, base)
        else:
            hint = known_method.split(":", 1)[1] if known_method and ":" in known_method else None
            items, used = check_html(s, base, hint)
            method = f"html:{used}" if used else "error"
        return name, domain, method, items, None
    except Exception as e:  # noqa: BLE001
        return name, domain, known_method or "error", {}, f"{type(e).__name__}: {e}"[:200]


# ---------------------------------------------------------------- notificações
def notify(title, body, click=None):
    sent = False
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        headers = {"Title": title.encode("utf-8"), "Priority": "high",
                   "Tags": "rotating_light"}
        if click:
            headers["Click"] = click
        try:
            requests.post(f"{server}/{topic}", data=body.encode("utf-8"),
                          headers=headers, timeout=15)
            sent = True
        except requests.RequestException as e:
            print("ntfy falhou:", e)
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": f"{title}\n{body}",
                                "disable_web_page_preview": False}, timeout=15)
            sent = True
        except requests.RequestException as e:
            print("telegram falhou:", e)
    if not sent:
        print("[sem canal configurado]", title, "|", body)


# -------------------------------------------------------------------- principal
def main():
    if "--test" in sys.argv:
        notify("Teste do monitor 30th Celebration",
               "Se estás a ver isto, as notificações funcionam ✅",
               "https://pokelotas.com")
        return

    state = {"items": {}, "methods": {}}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    first_run = not state["items"]

    with open(STORES_FILE, encoding="utf-8") as f:
        stores = list(csv.DictReader(f))

    results = []
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = [ex.submit(check_store, st["name"], st["domain"].strip(),
                          state["methods"].get(st["domain"].strip()))
                for st in stores]
        for fu in as_completed(futs):
            results.append(fu.result())
    results.sort(key=lambda r: r[0].lower())

    new_items, alerts = {}, []
    for name, domain, method, items, err in results:
        state["methods"][domain] = method
        if err:
            # mantém o que sabíamos para não gerar falsos alertas
            for k, v in state["items"].items():
                if v.get("domain") == domain:
                    new_items[k] = v
            continue
        for url, it in items.items():
            it.update(store=name, domain=domain)
            prev = state["items"].get(url)
            was = prev.get("available") if prev else None
            if it["available"] and not was:
                alerts.append(it)
            new_items[url] = it

    state["items"] = new_items
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    if first_run and alerts:
        # 1.ª execução: um resumo em vez de dezenas de pushes
        lines = [f"• {a['store']}: {a['title']}" for a in alerts[:25]]
        notify(f"Monitor ativo — {len(alerts)} produto(s) já com stock",
               "\n".join(lines) + ("\n…" if len(alerts) > 25 else ""))
    else:
        for a in alerts:
            price = f" — {a['price']}€" if a.get("price") else ""
            notify(f"🟢 STOCK: {a['store']}", f"{a['title']}{price}\n{a['url']}", a["url"])

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    write_report(results, state)
    print(f"{len(results)} lojas verificadas, {len(alerts)} alerta(s).")


def write_report(results, state):
    L = [f"# Estado do monitor — {state['last_run']}\n",
         "| Loja | Método | Produtos encontrados | Com stock | Erro |",
         "|---|---|---|---|---|"]
    for name, domain, method, items, err in results:
        n = len(items)
        ok = sum(1 for i in items.values() if i["available"])
        L.append(f"| [{name}](https://{domain}) | {method} | {n} | {ok} | {err or ''} |")
    L += ["", "## Produtos com stock agora", ""]
    avail = [i for i in state["items"].values() if i.get("available")]
    for i in sorted(avail, key=lambda x: (x["store"], x["title"])):
        L.append(f"- **{i['store']}** — [{i['title']}]({i['url']})"
                 + (f" — {i['price']}€" if i.get("price") else ""))
    if not avail:
        L.append("_Nenhum._")
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
