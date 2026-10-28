#!/usr/bin/env python3
"""Atualiza data/eventos-sa.json a partir dos avisos oficiais do Black Desert SA.

A coleta falha de forma fechada: se o site mudar e não for possível reconhecer
os avisos, o arquivo JSON existente não é substituído.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "index.html"
JSON_PATH = ROOT / "data" / "eventos-sa.json"
SOURCE_URL = "https://www.sa.playblackdesert.com/pt-BR/News/Notice?boardType=3"
ALLOWED_HOSTS = ("sa.playblackdesert.com",)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BDO-Rotina-EventosSA/1.0; +https://github.com/ApokaNRNS/bdo-rotina)",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
}
TIMEOUT = 25
MAX_DETAIL_PAGES = 30
MAX_EVENTS = 120


def valid_official_url(value: str, image: bool = False) -> str:
    try:
        parsed = urlparse(value)
    except (TypeError, ValueError):
        return ""
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    allowed = host.endswith(".pearlcdn.com") if image else any(
        host == item or host.endswith("." + item) for item in ALLOWED_HOSTS
    )
    return value if allowed else ""


def normalize_url(value: str) -> str:
    """Converte links relativos/legados para uma URL oficial com país explícito."""
    url = urljoin("https://www.sa.playblackdesert.com", value.strip())
    if not valid_official_url(url):
        return ""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if "groupContentNo" not in query:
        return ""
    query.setdefault("countryType", ["pt-BR"])
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(query, doseq=True), ""))


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_days(text: str):
    match = re.search(r"(\d+)\s*dias?\s+restantes?", text, re.I)
    return int(match.group(1)) if match else None


def parse_date_tokens(text: str):
    """Extrai datas dos formatos mais comuns exibidos nas páginas oficiais."""
    patterns = [
        (r"(?<!\d)(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", "ymd"),
        (r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})(?:[ T](\d{1,2}):(\d{2}))?", "dmy"),
    ]
    found = []
    for pattern, order in patterns:
        for m in re.finditer(pattern, text):
            try:
                if order == "ymd":
                    year, month, day = map(int, m.group(1, 2, 3))
                else:
                    day, month, year = map(int, m.group(1, 2, 3))
                hour = int(m.group(4) or 0)
                minute = int(m.group(5) or 0)
                found.append((m.start(), datetime(year, month, day, hour, minute)))
            except ValueError:
                continue
    found.sort(key=lambda pair: pair[0])
    return [item for _, item in found]


def parse_event_end_date(text: str):
    # Prioriza o intervalo do evento (data ~ data), não datas de rodapé/atualização.
    token = r"(?:20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]20\d{2})(?:[ T]\d{1,2}:\d{2})?"
    ranges = list(re.finditer(rf"({token})\s*[~～–—]\s*({token})", text))
    if ranges:
        dates = parse_date_tokens(ranges[-1].group(0))
        if len(dates) >= 2:
            return dates[-1]
    dates = parse_date_tokens(text)
    return dates[-1] if len(dates) >= 2 else None


def extract_expiry(soup: BeautifulSoup, card_text: str, today: date):
    text = clean_text(soup.get_text(" ", strip=True))
    if re.search(r"\bpermanente\b", card_text + " " + text, re.I):
        return True, None, "andamento"

    end_date = parse_event_end_date(text)
    if end_date:
        end = end_date.date()
        days = max(0, (end - today).days)
        return False, days, "encerrado" if end < today else "andamento"

    days = parse_days(card_text)
    if days is not None:
        return False, days, "encerrado" if days <= 0 and re.search(r"encerrad[oa]", card_text, re.I) else "andamento"

    return False, None, "andamento"


def load_snapshot():
    if not HTML_PATH.exists():
        return []
    html = HTML_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"<script[^>]*id=['\"]eventos-sa-fallback['\"][^>]*>(.*?)</script>",
        html,
        flags=re.I | re.S,
    )
    if not match:
        print("Aviso: snapshot não encontrado no index.html.", file=sys.stderr)
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Snapshot incorporado no HTML inválido: {exc}") from exc
    events = data.get("events", [])
    return [e for e in events if isinstance(e, dict) and e.get("title") and e.get("url")]


def load_existing():
    if not JSON_PATH.exists():
        return []
    try:
        data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("Aviso: JSON existente inválido; o snapshot será usado como base.", file=sys.stderr)
        return []
    return data.get("events", []) if isinstance(data, dict) and isinstance(data.get("events"), list) else []


def find_event_cards(soup: BeautifulSoup):
    """Localiza links de anúncios e sobe a árvore para recuperar título e miniatura."""
    results = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        raw_href = anchor.get("href", "")
        if "groupContentNo=" not in raw_href or "Detail" not in raw_href:
            continue
        url = normalize_url(raw_href)
        title = clean_text(anchor.get("title") or anchor.get_text(" ", strip=True))
        if not url or not title or len(title) < 3:
            continue
        if title.lower() in {"saiba mais", "ver mais", "mais"}:
            title = ""
        node = anchor
        best = anchor
        # O cartão normalmente contém imagem e texto de duração; limita subida para
        # evitar associar a miniatura de um cartão vizinho.
        for _ in range(6):
            if node.parent is None:
                break
            node = node.parent
            best = node
            text = clean_text(node.get_text(" ", strip=True))
            if node.find("img") and len(text) > len(title) + 5:
                break
        if not title:
            heading = best.find(["h2", "h3", "h4", "strong"])
            if heading:
                title = clean_text(heading.get_text(" ", strip=True))
        if not title or title.lower() in {"saiba mais", "ver mais", "mais"}:
            continue
        if url in seen:
            continue
        seen.add(url)
        img = best.find("img")
        image = ""
        if img:
            image = img.get("src") or img.get("data-src") or img.get("data-original") or ""
            image = urljoin("https://www.sa.playblackdesert.com", image)
            image = valid_official_url(image, image=True)
        results.append({
            "title": title,
            "url": url,
            "image": image,
            "_card_text": clean_text(best.get_text(" ", strip=True)),
        })
    return results


def get_detail(session: requests.Session, event: dict):
    try:
        response = session.get(event["url"], timeout=TIMEOUT)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        return BeautifulSoup(response.text, "html.parser")
    except requests.RequestException as exc:
        print(f"Aviso: não foi possível ler detalhes de {event['title']}: {exc}", file=sys.stderr)
        return None


def main():
    session = requests.Session()
    session.headers.update(HEADERS)
    today = datetime.now(ZoneInfo("America/Sao_Paulo")).date()

    response = session.get(SOURCE_URL, timeout=TIMEOUT)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    listing = BeautifulSoup(response.text, "html.parser")
    scraped = find_event_cards(listing)

    # Uma mudança de HTML ou bloqueio do site nunca deve zerar os dados publicados.
    if len(scraped) < 5:
        raise RuntimeError(
            f"Foram reconhecidos apenas {len(scraped)} avisos na página oficial; "
            "o JSON existente foi preservado. Revise o seletor antes de publicar."
        )

    snapshot_events = load_snapshot()
    existing_events = load_existing()
    old_by_url = {}
    for event in snapshot_events + existing_events:
        url = normalize_url(str(event.get("url", "")))
        if url:
            old_by_url[url] = event

    output_by_url = {}
    for index, event in enumerate(scraped[:MAX_DETAIL_PAGES]):
        old = old_by_url.get(event["url"], {})
        permanent, days, status = False, None, "andamento"

        # Duração do cartão é uma fonte mais direta para "dias restantes".
        card_text = event.pop("_card_text", "")
        card_days = parse_days(card_text)
        card_permanent = bool(re.search(r"\bpermanente\b", card_text, re.I))
        if card_permanent or re.search(r"\bpermanente\b", event["title"], re.I):
            permanent, status = True, "andamento"
        detail = get_detail(session, event) if (card_days is None and not card_permanent) else None
        if detail:
            detail_text = clean_text(detail.get_text(" ", strip=True))
            if re.search(r"\bpermanente\b", detail_text, re.I):
                permanent, days, status = True, None, "andamento"
            else:
                end_date = parse_event_end_date(detail_text)
                if end_date:
                    end = end_date.date()
                    days = max(0, (end - today).days)
                    status = "encerrado" if end < today else "andamento"
                elif card_days is not None:
                    days = card_days
            if not event["image"]:
                # Em páginas de detalhe, imagens de thumbnail podem ser usadas
                # somente se estiverem em domínio de CDN oficial permitido.
                image = detail.find("meta", property="og:image")
                if image and image.get("content"):
                    candidate = urljoin(event["url"], image["content"])
                    event["image"] = valid_official_url(candidate, image=True)

        if card_permanent:
            permanent, days, status = True, None, "andamento"
        elif days is None and card_days is not None:
            days, status = card_days, "andamento"
        if permanent:
            days, status = None, "andamento"
        elif days is None:
            days = old.get("daysRemaining")
            status = old.get("status", status)
            if old.get("permanent") is False and old.get("status") == "encerrado":
                status = "encerrado"

        normalized = {
            "title": event["title"],
            "url": event["url"],
            "image": event["image"] or old.get("image", ""),
            "status": status,
            "daysRemaining": days,
            "permanent": permanent or old.get("permanent") is True,
        }
        if normalized["permanent"]:
            normalized["status"] = "andamento"
            normalized["daysRemaining"] = None
        output_by_url[normalized["url"]] = normalized

        # Evita sobrecarregar o site oficial.
        if index < min(len(scraped), MAX_DETAIL_PAGES) - 1:
            time.sleep(0.15)

    # Mantém eventos antigos que saíram da primeira página, incluindo encerrados,
    # até o limite do feed. Eventos já coletados são atualizados, nunca duplicados.
    for old in snapshot_events + existing_events:
        url = normalize_url(str(old.get("url", "")))
        if not url or url in output_by_url or not old.get("title"):
            continue
        item = dict(old)
        item["url"] = url
        item["image"] = valid_official_url(str(item.get("image", "")), image=True)
        if item.get("permanent") is True:
            item["status"], item["daysRemaining"] = "andamento", None
        output_by_url[url] = item

    events = list(output_by_url.values())[:MAX_EVENTS]
    if len(events) < 5:
        raise RuntimeError("A coleta resultou em poucos eventos; arquivo existente preservado.")

    # Não gera commit/deploy se o conteúdo dos eventos continuar idêntico.
    if JSON_PATH.exists():
        try:
            current = json.loads(JSON_PATH.read_text(encoding="utf-8"))
            if current.get("source") == SOURCE_URL and current.get("events") == events:
                print(f"Nenhuma mudança nos {len(events)} eventos; JSON mantido sem novo commit.")
                return
        except (json.JSONDecodeError, AttributeError, OSError):
            pass

    data = {
        "source": SOURCE_URL,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "events": events,
    }
    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = JSON_PATH.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Confere o JSON antes da substituição atômica do arquivo publicado.
    checked = json.loads(temp_path.read_text(encoding="utf-8"))
    if not isinstance(checked.get("events"), list) or len(checked["events"]) < 5:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError("Validação final falhou; JSON publicado não foi alterado.")
    temp_path.replace(JSON_PATH)
    print(f"Eventos SA atualizados: {len(events)} itens; arquivo {JSON_PATH.relative_to(ROOT)}.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        sys.exit(1)
