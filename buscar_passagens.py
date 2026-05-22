#!/usr/bin/env python3
"""
buscar_passagens.py
Pesquisa de passagens aéreas (multi-trecho), baseado em
prompt_pesquisa_passagens.md.

Suporta:
  - Ida e volta (2 trechos)
  - Multi-cidade / open-jaw (N trechos com origens e destinos diferentes)
  - Só ida (1 trecho)

Modos:
  1. Deep links (padrão, sem dependências) — gera URLs prontas para
     Google Flights, Skyscanner, Kayak, Decolar e Trip.com.
  2. Google Flights via fast-flights (opcional, grátis, sem cadastro)
     — `pip install fast-flights`. Cada trecho é consultado como
     busca one-way; o "total estimado" é a soma do melhor preço de
     cada trecho (passagens separadas).

Aeroportos em ORIG/DEST aceitam:
  - Código IATA único:               GRU, IBZ, CDG
  - Código IATA de cidade (multi):   SAO=GRU+CGH+VCP, RIO=GIG+SDU,
                                     PAR=CDG+ORY+BVA, LON=LHR+LGW+STN+…,
                                     NYC=JFK+LGA+EWR, TYO=HND+NRT, etc.
  - Lista separada por vírgula:      GRU,CGH,VCP / CDG,ORY

Exemplos:
  # ida e volta (atalho)
  python buscar_passagens.py --origem GRU --destino GIG \\
      --ida 15/06/2026 --volta 22/06/2026 --html

  # ida e volta SP→Paris incluindo TODOS os aeroportos
  python buscar_passagens.py --origem SAO --destino PAR \\
      --ida 15/06/2026 --volta 22/06/2026 --html

  # multi-trecho (open-jaw)
  python buscar_passagens.py \\
      --trecho GRU-IBZ:16/07/2026 \\
      --trecho CDG-GRU:01/08/2026 \\
      --html

  # multi-trecho com lista de aeroportos
  python buscar_passagens.py \\
      --trecho GRU,CGH,VCP-IBZ:16/07/2026 \\
      --trecho CDG,ORY-GRU:01/08/2026 \\
      --html

  # interativo
  python buscar_passagens.py
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

def _tz_brasil():
    try:
        return ZoneInfo("America/Sao_Paulo")
    except Exception:
        from datetime import timezone, timedelta
        return timezone(timedelta(hours=-3))

TZ_BR = _tz_brasil()
from typing import List, Optional
from urllib.parse import urlencode

CLASSES = {
    "economica": ("economy",         "Econômica"),
    "premium":   ("premium-economy", "Premium Economy"),
    "executiva": ("business",        "Executiva"),
    "primeira":  ("first",           "Primeira"),
}

# Códigos IATA de cidade (metroárea) → aeroportos atendidos.
# Permite o usuário digitar só "SAO" e a busca cobrir GRU + CGH + VCP.
# Adicione novos conforme precisar.
CITY_TO_AIRPORTS: dict[str, list[str]] = {
    # Brasil
    "SAO": ["GRU", "CGH", "VCP"],          # São Paulo
    "RIO": ["GIG", "SDU"],                  # Rio de Janeiro
    "BHZ": ["CNF", "PLU"],                  # Belo Horizonte
    # Europa
    "PAR": ["CDG", "ORY", "BVA"],          # Paris
    "LON": ["LHR", "LGW", "STN", "LCY", "LTN", "SEN"],  # Londres
    "MIL": ["MXP", "LIN", "BGY"],          # Milão
    "ROM": ["FCO", "CIA"],                  # Roma
    "MOW": ["SVO", "DME", "VKO"],          # Moscou
    "STO": ["ARN", "BMA", "NYO"],          # Estocolmo
    "BUH": ["OTP", "BBU"],                  # Bucareste
    # América do Norte
    "NYC": ["JFK", "LGA", "EWR"],          # Nova York
    "WAS": ["IAD", "DCA", "BWI"],          # Washington
    "CHI": ["ORD", "MDW"],                  # Chicago
    "YTO": ["YYZ", "YTZ"],                  # Toronto
    # Ásia / Pacífico
    "TYO": ["HND", "NRT"],                  # Tóquio
    "OSA": ["KIX", "ITM"],                  # Osaka
    "SEL": ["ICN", "GMP"],                  # Seul
    "BJS": ["PEK", "PKX"],                  # Pequim
    "SHA": ["PVG", "SHA"],                  # Xangai
    "BKK": ["BKK", "DMK"],                  # Bangcoc
    # Outros
    "IST": ["IST", "SAW"],                  # Istambul
    "BUE": ["EZE", "AEP"],                  # Buenos Aires
}


def _expandir_aeroportos(codigo: str) -> list[str]:
    """Recebe um código (cidade, aeroporto ou lista) e devolve a lista
    de aeroportos IATA a consultar.

    Exemplos:
      'SAO'         -> ['GRU', 'CGH', 'VCP']
      'GRU'         -> ['GRU']
      'GRU,CGH,VCP' -> ['GRU', 'CGH', 'VCP']
      'gru, cgh'    -> ['GRU', 'CGH']
    """
    if not codigo:
        return []
    if "," in codigo:
        return [
            c.strip().upper()
            for c in codigo.split(",")
            if c.strip()
        ]
    c = codigo.strip().upper()
    return CITY_TO_AIRPORTS.get(c, [c])


def _label_aeroportos(codigo: str) -> str:
    """Texto curto para exibição.

    Exemplos:
      'GRU'         -> 'GRU'
      'SAO'         -> 'SAO (GRU/CGH/VCP)'
      'GRU,CGH,VCP' -> 'GRU/CGH/VCP'
    """
    if not codigo:
        return ""
    if "," in codigo:
        return "/".join(
            p.strip().upper() for p in codigo.split(",") if p.strip()
        )
    c = codigo.strip().upper()
    aeros = CITY_TO_AIRPORTS.get(c)
    if aeros and len(aeros) > 1:
        return f"{c} ({'/'.join(aeros)})"
    return c


COMPANHIAS = [
    {
        "nome": "Air France",
        "iata": "AF",
        "pais": "🇫🇷",
        "homepage": "https://wwws.airfrance.com.br/",
        "busca": "https://wwws.airfrance.com.br/search?bookingFlow=LEISURE",
    },
    {
        "nome": "TAP Air Portugal",
        "iata": "TP",
        "pais": "🇵🇹",
        "homepage": "https://www.flytap.com/pt-br",
        "busca": "https://www.flytap.com/pt-br/reservas",
    },
    {
        "nome": "Air Europa",
        "iata": "UX",
        "pais": "🇪🇸",
        "homepage": "https://www.aireuropa.com/br/pt",
        "busca": "https://www.aireuropa.com/br/pt/vuelos.html",
    },
]


@dataclass
class Leg:
    origem: str
    destino: str
    data: datetime

    @property
    def origem_iata(self) -> str:
        """Código primário do trecho (cidade ou primeiro aeroporto).
        Usado nos deep links — cidades IATA (SAO, PAR, LON…) são aceitas
        nativamente pela maioria dos buscadores."""
        return self.origem.upper().split(",")[0].strip()

    @property
    def destino_iata(self) -> str:
        return self.destino.upper().split(",")[0].strip()

    @property
    def origens_lista(self) -> list[str]:
        """Lista de aeroportos IATA a consultar (expande city codes
        e separadores por vírgula)."""
        return _expandir_aeroportos(self.origem)

    @property
    def destinos_lista(self) -> list[str]:
        return _expandir_aeroportos(self.destino)

    @property
    def origem_label(self) -> str:
        """Texto amigável para exibição (ex: 'SAO (GRU/CGH/VCP)')."""
        return _label_aeroportos(self.origem)

    @property
    def destino_label(self) -> str:
        return _label_aeroportos(self.destino)

    @property
    def multi_aeroportos(self) -> bool:
        return len(self.origens_lista) > 1 or len(self.destinos_lista) > 1

    @property
    def data_iso(self) -> str:
        return self.data.strftime("%Y-%m-%d")

    @property
    def data_br(self) -> str:
        return self.data.strftime("%d/%m/%Y")

    @property
    def data_yymmdd(self) -> str:
        return self.data.strftime("%y%m%d")


@dataclass
class Viagem:
    legs: List[Leg] = field(default_factory=list)
    pax: int = 1
    classe: str = "economica"
    max_escalas: Optional[int] = None

    @property
    def classe_codigo(self) -> str:
        return CLASSES.get(self.classe.lower(), ("economy", ""))[0]

    @property
    def classe_label(self) -> str:
        return CLASSES.get(self.classe.lower(), ("", "Econômica"))[1]

    @property
    def trip_type(self) -> str:
        if len(self.legs) == 1:
            return "one-way"
        if (
            len(self.legs) == 2
            and self.legs[0].origem_iata == self.legs[1].destino_iata
            and self.legs[0].destino_iata == self.legs[1].origem_iata
        ):
            return "round-trip"
        return "multi-city"

    @property
    def rota_resumo(self) -> str:
        partes = [self.legs[0].origem_iata]
        for leg in self.legs:
            partes.append(leg.destino_iata)
        return " → ".join(partes)


def parse_data_br(s: str) -> datetime:
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Data inválida: {s!r}. Use DD/MM/AAAA."
    )


_TRECHO_FIND_RE = re.compile(
    r"[A-Za-z]{3}(?:\s*,\s*[A-Za-z]{3})*"
    r"\s*-\s*"
    r"[A-Za-z]{3}(?:\s*,\s*[A-Za-z]{3})*"
    r"\s*[:\s]\s*"
    r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}"
)


def split_trechos(raw: str) -> list[str]:
    """Quebra uma string em trechos individuais.

    Aceita separadores `;`, `|` ou quebra de linha (recomendados para
    quem usa listas com vírgula). Se nenhum desses separadores estiver
    presente, usa regex para extrair os trechos — assim funciona tanto
    com o formato legado (`A-B:DATA,C-D:DATA`) quanto com listas
    (`A,B-C:DATA,D-E:DATA`)."""
    if not raw:
        return []
    for sep in (";", "|", "\n"):
        if sep in raw:
            return [p.strip() for p in raw.split(sep) if p.strip()]
    achados = [m.group(0) for m in _TRECHO_FIND_RE.finditer(raw)]
    if achados:
        return achados
    # último fallback: split por vírgula (formato legado de 1 trecho)
    return [p.strip() for p in raw.split(",") if p.strip()]


def parse_trecho(s: str) -> Leg:
    """Formato: ORIG-DEST:DD/MM/AAAA  ex: GRU-IBZ:16/07/2026

    ORIG e DEST podem ser:
      - Código IATA de aeroporto único: GRU, IBZ
      - Código IATA de cidade (multi-aeroporto): SAO, RIO, PAR, LON,
        NYC, MIL, ROM, TYO, NYC, etc. — expande automaticamente
        para todos os aeroportos da metrópole.
      - Lista de aeroportos separados por vírgula: GRU,CGH,VCP

    Exemplos válidos:
      GRU-IBZ:16/07/2026
      SAO-PAR:16/07/2026          (= GRU/CGH/VCP → CDG/ORY/BVA)
      GRU,CGH-IBZ:16/07/2026      (sem VCP, só GRU e CGH)
    """
    s = s.strip()
    iata = r"[A-Za-z]{3}(?:\s*,\s*[A-Za-z]{3})*"
    m = re.match(
        rf"^\s*({iata})\s*-\s*({iata})\s*[:\s]\s*(.+?)\s*$", s
    )
    if not m:
        raise argparse.ArgumentTypeError(
            f"Trecho inválido: {s!r}. "
            f"Use ORIG-DEST:DD/MM/AAAA (ex: GRU-IBZ:16/07/2026, "
            f"SAO-PAR:16/07/2026 ou GRU,CGH,VCP-IBZ:16/07/2026)."
        )
    orig, dest, data_str = m.groups()
    orig_clean = ",".join(p.strip().upper() for p in orig.split(","))
    dest_clean = ",".join(p.strip().upper() for p in dest.split(","))
    return Leg(orig_clean, dest_clean, parse_data_br(data_str))


def gerar_link_oferta(leg: Leg, oferta: dict, classe_label: str,
                      pax: int = 1) -> str:
    """Gera URL do Google Flights para uma oferta específica de um trecho.
    Inclui a companhia para refinar a busca. Quando a oferta tem
    `rota_aero` (par de aeroportos específico em legs multi-aeroporto),
    a busca é feita por esse par e não pelo código de cidade."""
    cia = (oferta.get("cia") or "").strip()
    rota = (oferta.get("rota_aero") or "").strip()
    if rota and "→" in rota:
        orig, dest = [p.strip() for p in rota.split("→", 1)]
    else:
        orig, dest = leg.origem_iata, leg.destino_iata
    base = (
        f"Voo só ida de {orig} para {dest} "
        f"em {leg.data_iso} {pax} passageiros classe {classe_label}"
    )
    if cia and cia != "—":
        base += f" pela {cia}"
    return "https://www.google.com/travel/flights?" + urlencode(
        {"q": base, "curr": "BRL", "hl": "pt-BR"}
    )


def gerar_deeplinks(v: Viagem) -> List[dict]:
    """Gera links de busca preenchidos para cada site."""
    pax = v.pax
    cls = v.classe_codigo

    if v.trip_type == "round-trip":
        a, b = v.legs[0], v.legs[1]
        orig = a.origem_iata
        dest = a.destino_iata

        google_q = (
            f"Voos de {orig} para {dest} em {a.data_iso} "
            f"retorno {b.data_iso} {pax} passageiros classe {v.classe_label}"
        )
        google = "https://www.google.com/travel/flights?" + urlencode(
            {"q": google_q, "curr": "BRL", "hl": "pt-BR"}
        )
        sky = (
            f"https://www.skyscanner.com.br/transport/flights/"
            f"{orig.lower()}/{dest.lower()}/{a.data_yymmdd}/{b.data_yymmdd}/"
            f"?adults={pax}&cabinclass={cls.replace('-', '_')}"
        )
        kayak = (
            f"https://www.kayak.com.br/flights/"
            f"{orig}-{dest}/{a.data_iso}/{b.data_iso}/{pax}adults"
        )
        decolar = (
            f"https://www.decolar.com/shop/flights/results/roundtrip/"
            f"{orig}/{dest}/{a.data_iso}/{b.data_iso}/{pax}/0/0/"
        )
        trip = "https://br.trip.com/flights/booking?" + urlencode({
            "dcity": orig.lower(),
            "acity": dest.lower(),
            "ddate": a.data_iso,
            "rdate": b.data_iso,
            "class": "y",
            "quantity": pax,
        })

    elif v.trip_type == "one-way":
        a = v.legs[0]
        orig, dest = a.origem_iata, a.destino_iata
        google_q = (
            f"Voo só ida de {orig} para {dest} em {a.data_iso} "
            f"{pax} passageiros classe {v.classe_label}"
        )
        google = "https://www.google.com/travel/flights?" + urlencode(
            {"q": google_q, "curr": "BRL", "hl": "pt-BR"}
        )
        sky = (
            f"https://www.skyscanner.com.br/transport/flights/"
            f"{orig.lower()}/{dest.lower()}/{a.data_yymmdd}/"
            f"?adults={pax}&cabinclass={cls.replace('-', '_')}"
        )
        kayak = (
            f"https://www.kayak.com.br/flights/"
            f"{orig}-{dest}/{a.data_iso}/{pax}adults"
        )
        decolar = (
            f"https://www.decolar.com/shop/flights/results/oneway/"
            f"{orig}/{dest}/{a.data_iso}/{pax}/0/0/"
        )
        trip = "https://br.trip.com/flights/booking?" + urlencode({
            "dcity": orig.lower(),
            "acity": dest.lower(),
            "ddate": a.data_iso,
            "class": "y",
            "quantity": pax,
        })

    else:
        legs_desc = " e ".join(
            f"{L.origem_iata}→{L.destino_iata} em {L.data_iso}"
            for L in v.legs
        )
        google_q = (
            f"Voos multi-cidade: {legs_desc} "
            f"{pax} passageiros classe {v.classe_label}"
        )
        google = "https://www.google.com/travel/flights?" + urlencode(
            {"q": google_q, "curr": "BRL", "hl": "pt-BR"}
        )

        kayak_legs = "/".join(
            f"{L.origem_iata}-{L.destino_iata}/{L.data_iso}" for L in v.legs
        )
        kayak = f"https://www.kayak.com.br/flights/{kayak_legs}/{pax}adults"

        sky_legs = "/".join(
            f"{L.origem_iata.lower()}/{L.destino_iata.lower()}/"
            f"{L.data_yymmdd}"
            for L in v.legs
        )
        sky = (
            f"https://www.skyscanner.com.br/transport/flights-multistop/"
            f"{sky_legs}/?adults={pax}&cabinclass={cls.replace('-', '_')}"
        )

        dec_legs = "/".join(
            f"{L.origem_iata}/{L.destino_iata}/{L.data_iso}" for L in v.legs
        )
        decolar = (
            f"https://www.decolar.com/shop/flights/results/multipledestinations/"
            f"{dec_legs}/{pax}/0/0/"
        )

        trip = (
            "https://br.trip.com/flights/multi-city?"
            + urlencode({"class": "y", "quantity": pax})
        )

    return [
        {"nome": "Google Flights", "url": google},
        {"nome": "Skyscanner",     "url": sky},
        {"nome": "Kayak",          "url": kayak},
        {"nome": "Decolar",        "url": decolar},
        {"nome": "Trip.com",       "url": trip},
    ]


_PRICE_RE = re.compile(r"[\d.,]+")
_MOEDA_SIMBOLOS = {
    "R$": "BRL", "BRL": "BRL", "RS": "BRL",
    "US$": "USD", "USD": "USD",
    "€": "EUR", "EUR": "EUR",
    "£": "GBP", "GBP": "GBP",
    "$": "USD",  # fallback: $ sozinho = USD
}


def _detectar_moeda(preco_str: str) -> str:
    """Detecta a moeda do preço retornado pelo Google Flights."""
    s = str(preco_str or "").upper().strip()
    for simb, code in _MOEDA_SIMBOLOS.items():
        if simb in s:
            return code
    return "BRL"


def _preco_para_float(preco) -> Optional[float]:
    if preco is None:
        return None
    if isinstance(preco, (int, float)):
        return float(preco)
    m = _PRICE_RE.search(str(preco))
    if not m:
        return None
    s = m.group(0)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


_cache_cotacao: dict[str, tuple[float, float]] = {}


def cotacao_para_brl(moeda: str) -> float:
    """Retorna quantos BRL vale 1 unidade da moeda. Cache de 10 min.
    Usa AwesomeAPI (grátis, sem cadastro). Fallback: 1.0 se falhar."""
    moeda = moeda.upper()
    if moeda == "BRL":
        return 1.0
    agora = time.time()
    if moeda in _cache_cotacao:
        valor, ts = _cache_cotacao[moeda]
        if agora - ts < 600:
            return valor
    try:
        url = f"https://economia.awesomeapi.com.br/json/last/{moeda}-BRL"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        chave = f"{moeda}BRL"
        valor = float(data[chave]["bid"])
        _cache_cotacao[moeda] = (valor, agora)
        return valor
    except Exception as e:
        print(f"[aviso] cotação {moeda}→BRL falhou: {e}; usando 1.0",
              file=sys.stderr)
        return 1.0


def normalizar_para_brl(preco_num: Optional[float],
                        preco_str: str) -> tuple[Optional[float], str]:
    """Converte preço para BRL. Retorna (valor_brl, str_exibicao).
    Se já era BRL, mantém igual. Se converteu, mostra '~R$ X (USD Y)'."""
    if preco_num is None:
        return None, preco_str
    moeda = _detectar_moeda(preco_str)
    if moeda == "BRL":
        return preco_num, preco_str
    taxa = cotacao_para_brl(moeda)
    brl = preco_num * taxa
    if taxa == 1.0:
        return preco_num, f"{preco_str} (sem conversão)"
    return brl, f"~R$ {brl:,.0f} ({preco_str})".replace(",", ".")


_TP_API = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
_GATE_FONTE = {
    "Aviasales": "Aviasales",
    "JetRadar": "JetRadar",
    "Jetradar": "JetRadar",
    "KIWI": "Kiwi.com",
    "Kiwi": "Kiwi.com",
    "Trip": "Trip.com",
    "TripCom": "Trip.com",
    "Tripcom": "Trip.com",
    "Skyscanner": "Skyscanner",
    "Kupibilet": "Kupibilet",
    "Tickets": "Tickets",
    "OneTwoTrip": "OneTwoTrip",
    "Biletix": "Biletix",
    "Mytrip": "Mytrip",
    "Gotogate": "Gotogate",
    "Edreams": "eDreams",
    "Booking": "Booking.com",
    "Travix": "Travix",
}


def _fonte_normalizada(gate: str) -> str:
    if not gate:
        return "Travelpayouts"
    for k, v in _GATE_FONTE.items():
        if k.lower() in gate.lower():
            return v
    return gate


_TP_PARTNERS = {
    "skyscanner": 4115,
    "kiwi":       4220,
    "tripcom":    8626,
    "kayak":      0,
    "decolar":    0,
    "booking":    1146,
}


def _tp_redirect(marker: str, partner_id: int, url_final: str) -> str:
    """Encurta via tp.media com marker — comissão Travelpayouts."""
    from urllib.parse import quote
    if not marker or not partner_id:
        return url_final
    return (
        f"https://tp.media/r?marker={marker}&p={partner_id}"
        f"&u={quote(url_final, safe='')}&campaign_id=100"
    )


_KAYAK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

# Detecta blocos JSON do tipo {"price": 1234, ...} ou "price":"R$ 1.234"
# que o Kayak embute no HTML inicial.
_KAYAK_RE_PRECO = re.compile(
    r'"(?:price|displayPrice|totalPrice)"\s*:\s*"?'
    r'(?:R\$\s*)?([\d.,]+)"?',
    re.IGNORECASE,
)
_KAYAK_RE_CIA = re.compile(
    r'"(?:airline(?:Name)?|carrierName)"\s*:\s*"([^"]{2,40})"',
    re.IGNORECASE,
)
_KAYAK_RE_STOPS = re.compile(
    r'"(?:stops|numStops|stopCount)"\s*:\s*(\d+)',
    re.IGNORECASE,
)


def _scrape_kayak(leg: Leg, pax: int, classe_codigo: str
                  ) -> Optional[dict]:
    """Tenta extrair preço/cia do HTML do Kayak. Best-effort: pode
    falhar por bot-detection (Cloudflare/captcha). Retorna None nesse
    caso — o chamador usa fallback de link."""
    cabin = {
        "economy": "economy", "premium-economy": "premium",
        "business": "business", "first": "first",
    }.get(classe_codigo, "economy")
    url = (
        f"https://www.kayak.com.br/flights/"
        f"{leg.origem_iata}-{leg.destino_iata}/"
        f"{leg.data_iso}/{pax}adults/{cabin}"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": _KAYAK_UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
        "Accept-Encoding": "identity",
        "Referer": "https://www.kayak.com.br/",
    })
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            corpo = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[kayak-scrape] HTTP falhou: {e}", file=sys.stderr)
        return None

    if any(s in corpo.lower() for s in (
        "captcha", "are you a robot", "access denied", "cf-challenge",
    )):
        print("[kayak-scrape] bloqueado (captcha/cloudflare) — usando "
              "link de busca como fallback", file=sys.stderr)
        return None

    m_preco = _KAYAK_RE_PRECO.search(corpo)
    if not m_preco:
        print("[kayak-scrape] sem preço no HTML inicial — "
              "Kayak renderiza via JS; usando link", file=sys.stderr)
        return None

    preco_num = _preco_para_float(m_preco.group(1))
    if preco_num is None or preco_num <= 0:
        return None

    m_cia = _KAYAK_RE_CIA.search(corpo)
    m_stops = _KAYAK_RE_STOPS.search(corpo)
    cia = m_cia.group(1) if m_cia else "Diversas"
    stops = int(m_stops.group(1)) if m_stops else "—"
    return {
        "cia": cia,
        "preco_num": preco_num,
        "preco_str": formatar_brl(preco_num),
        "escalas": stops,
    }


def ofertas_agregadores(leg: Leg, marker: str,
                        classe_codigo: str = "economy",
                        pax: int = 1,
                        tentar_kayak_scrape: Optional[bool] = None) -> list:
    """Cria linhas de busca em agregadores (Skyscanner / Kayak / Trip.com).
    - Pro Kayak: tenta scraping HTTP simples se ``tentar_kayak_scrape`` for
      verdadeiro (ou se a env ``KAYAK_SCRAPE=1`` estiver setada). Atenção:
      Kayak renderiza preço via JS e protege com Cloudflare, então o
      scrape geralmente devolve nada em IP de servidor. Por isso o
      padrão é DESLIGADO — devolve linha placeholder com link de busca.
    - Pra Skyscanner / Trip.com: o preço real vem da API Travelpayouts
      (ver `consultar_travelpayouts`). Aqui devolvemos só a linha
      placeholder, que é REMOVIDA depois se a API trouxer oferta real
      da mesma fonte.
    """
    if tentar_kayak_scrape is None:
        tentar_kayak_scrape = (
            os.getenv("KAYAK_SCRAPE", "").strip().lower()
            in ("1", "true", "yes", "on")
        )
    data = leg.data_iso
    yymmdd = f"{data[2:4]}{data[5:7]}{data[8:10]}"
    cabin_sky = {
        "economy": "economy", "premium-economy": "premium_economy",
        "business": "business", "first": "first",
    }.get(classe_codigo, "economy")
    cabin_kayak = {
        "economy": "economy", "premium-economy": "premium",
        "business": "business", "first": "first",
    }.get(classe_codigo, "economy")
    cabin_trip = {
        "economy": "y", "premium-economy": "s",
        "business": "c", "first": "f",
    }.get(classe_codigo, "y")

    sky_url = (
        f"https://www.skyscanner.com.br/transport/flights/"
        f"{leg.origem_iata.lower()}/{leg.destino_iata.lower()}/"
        f"{yymmdd}/?adults={pax}&cabinclass={cabin_sky}"
    )
    kayak_url = (
        f"https://www.kayak.com.br/flights/{leg.origem_iata}-"
        f"{leg.destino_iata}/{data}/{pax}adults/{cabin_kayak}"
    )
    trip_url = (
        f"https://br.trip.com/flights/booking?dcity={leg.origem_iata.lower()}"
        f"&acity={leg.destino_iata.lower()}&ddate={data}"
        f"&class={cabin_trip}&quantity={pax}"
    )

    sky_link = _tp_redirect(marker, _TP_PARTNERS["skyscanner"], sky_url)
    trip_link = _tp_redirect(marker, _TP_PARTNERS["tripcom"], trip_url)
    kayak_aff = (os.getenv("KAYAK_AFFILIATE_ID") or "").strip()
    if kayak_aff:
        sep = "&" if "?" in kayak_url else "?"
        kayak_link = f"{kayak_url}{sep}a={kayak_aff}"
    else:
        kayak_link = kayak_url

    def _mk(cia, fonte, link, preco_num=None, preco_str="Ver preço →",
            escalas="—"):
        return {
            "cia": cia, "saida": "—", "chegada": "—",
            "duracao": "—", "escalas": escalas,
            "preco_str": preco_str, "preco": preco_num,
            "melhor": False, "fonte": fonte, "link": link,
            "destaque_agregador": True,
        }

    kayak_row = _mk("Buscar ofertas", "Kayak", kayak_link)
    if tentar_kayak_scrape:
        info = _scrape_kayak(leg, pax, classe_codigo)
        if info:
            print(
                f"[kayak-scrape] OK — {info['cia']} "
                f"a partir de {info['preco_str']}",
                file=sys.stderr,
            )
            kayak_row = _mk(
                info["cia"], "Kayak", kayak_link,
                preco_num=info["preco_num"],
                preco_str=info["preco_str"],
                escalas=info["escalas"],
            )

    return [
        _mk("Buscar ofertas",  "Skyscanner", sky_link),
        kayak_row,
        _mk("Buscar ofertas",  "Trip.com",   trip_link),
    ]


def consultar_travelpayouts(leg: Leg, token: str,
                            classe: str = "economy",
                            limite: int = 100) -> list:
    """Consulta a Aviasales/Travelpayouts Data API e devolve ofertas
    no mesmo formato das do Google Flights. Retorna lista vazia se falhar."""
    trip_class = {
        "economy": 0, "business": 1, "first": 2,
        "premium-economy": 0,  # API não distingue PE
    }.get(classe, 0)
    params = urlencode({
        "origin": leg.origem_iata,
        "destination": leg.destino_iata,
        "departure_at": leg.data_iso,
        "one_way": "true",
        "currency": "brl",
        "market": "br",
        "sorting": "price",
        "direct": "false",
        "limit": str(limite),
        "page": "1",
        "unique": "false",
        "trip_class": str(trip_class),
        "token": token,
    })
    url = f"{_TP_API}?{params}"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "flyfinder-agent/1.0"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[aviso] Travelpayouts falhou: {e}", file=sys.stderr)
        return []

    if not data.get("success"):
        print(
            f"[aviso] Travelpayouts API: success=false "
            f"error={data.get('error')!r}",
            file=sys.stderr,
        )
        return []

    n_raw = len(data.get("data") or [])
    if n_raw == 0:
        print(
            "[aviso] Travelpayouts: API ok mas sem dados em cache "
            f"para {leg.origem_iata}->{leg.destino_iata} em {leg.data_iso}.",
            file=sys.stderr,
        )
        return []

    marker = (os.getenv("TRAVELPAYOUTS_MARKER") or "").strip()
    ofertas = []
    for it in data.get("data", []) or []:
        preco_num = float(it.get("price", 0)) or None
        if not preco_num:
            continue
        cia = it.get("airline") or "—"
        gate = it.get("gate") or ""
        link_rel = it.get("link") or ""
        link_url = ""
        if link_rel:
            if "?" in link_rel:
                link_url = f"https://www.aviasales.com{link_rel}"
            else:
                link_url = f"https://www.aviasales.com{link_rel}"
            if marker:
                sep = "&" if "?" in link_url else "?"
                if "marker=" in link_url:
                    import re as _re
                    link_url = _re.sub(
                        r"marker=[^&]*", f"marker={marker}", link_url
                    )
                else:
                    link_url = f"{link_url}{sep}marker={marker}"
        fonte_norm = _fonte_normalizada(gate)
        ofertas.append({
            "cia":       cia,
            "saida":     (it.get("departure_at") or "")[11:16] or "—",
            "chegada":   (it.get("return_at") or "")[11:16] or "—",
            "duracao":   f"{(it.get('duration') or 0) // 60}h "
                         f"{(it.get('duration') or 0) % 60:02d}min",
            "escalas":   int(it.get("transfers") or 0),
            "preco_str": formatar_brl(preco_num),
            "preco":     preco_num,
            "melhor":    False,
            "fonte":     fonte_norm,
            "link":      link_url,
            "destaque_agregador": fonte_norm in (
                "Skyscanner", "Trip.com", "Kayak",
            ),
            "origem_real":  it.get("origin_airport")
                            or it.get("origin") or "",
            "destino_real": it.get("destination_airport")
                            or it.get("destination") or "",
        })
    return ofertas


def _combos_aeroportos(leg: Leg) -> list[tuple[str, str]]:
    """Combinações origem×destino a consultar para uma leg.
    Sempre filtra pares com origem == destino (caso raro de city code
    que compartilha código com aeroporto, ex: SHA)."""
    combos = [
        (o, d)
        for o in leg.origens_lista
        for d in leg.destinos_lista
        if o != d
    ]
    return combos or [(leg.origem_iata, leg.destino_iata)]


def _sig_oferta(o: dict) -> tuple:
    """Assinatura única de uma oferta (pra dedup entre combos)."""
    return (
        o.get("cia") or "",
        str(o.get("saida") or ""),
        str(o.get("chegada") or ""),
        o.get("preco") if o.get("preco") is not None else o.get("preco_str"),
        o.get("fonte") or "",
    )


def consultar_google_flights(v: Viagem) -> Optional[dict]:
    """Consulta o Google Flights via fast-flights, um trecho por vez
    (busca one-way para cada leg). Para legs com múltiplos aeroportos
    (códigos de cidade como SAO/PAR ou listas como GRU,CGH,VCP), itera
    por todas as combinações e mescla as ofertas (dedup por assinatura).
    Retorna dict com lista por trecho ou None se o pacote não estiver
    instalado."""
    try:
        from fast_flights import FlightData, Passengers, get_flights
    except ImportError:
        print(
            "[info] 'fast-flights' não instalado — usando apenas deep links. "
            "Para preços reais: pip install fast-flights",
            file=sys.stderr,
        )
        return None

    marker_env = (os.getenv("TRAVELPAYOUTS_MARKER") or "").strip()
    tp_token = (os.getenv("TRAVELPAYOUTS_TOKEN") or "").strip()

    trechos = []
    for i, leg in enumerate(v.legs, 1):
        combos = _combos_aeroportos(leg)
        n_combos = len(combos)

        if leg.multi_aeroportos:
            print(
                f"[buscando] trecho {i}/{len(v.legs)}: "
                f"{leg.origem_label} → {leg.destino_label} "
                f"({n_combos} combinações) em {leg.data_br}...",
                file=sys.stderr,
            )
        else:
            print(
                f"[buscando] trecho {i}/{len(v.legs)}: "
                f"{leg.origem_iata} → {leg.destino_iata} em {leg.data_br}...",
                file=sys.stderr,
            )

        ofertas: list[dict] = []
        seen: set = set()
        current_price = None
        last_error: Optional[str] = None

        for j, (orig, dest) in enumerate(combos, 1):
            if n_combos > 1:
                print(
                    f"  [combo {j}/{n_combos}] {orig} → {dest}...",
                    file=sys.stderr,
                )
            try:
                result = get_flights(
                    flight_data=[
                        FlightData(
                            date=leg.data_iso,
                            from_airport=orig,
                            to_airport=dest,
                        ),
                    ],
                    trip="one-way",
                    seat=v.classe_codigo,
                    passengers=Passengers(adults=v.pax),
                    fetch_mode="fallback",
                )
            except Exception as e:
                last_error = str(e)
                print(
                    f"  [erro fast-flights {orig}→{dest}] {e}",
                    file=sys.stderr,
                )
                continue

            if current_price is None:
                current_price = getattr(result, "current_price", None)

            for f in getattr(result, "flights", []) or []:
                preco_raw = getattr(f, "price", None)
                preco_num = _preco_para_float(preco_raw)
                preco_brl, preco_str = normalizar_para_brl(
                    preco_num,
                    str(preco_raw) if preco_raw else "—",
                )
                rota_aero = (
                    f"{orig}→{dest}" if leg.multi_aeroportos else ""
                )
                oferta = {
                    "cia":       getattr(f, "name", "—") or "—",
                    "saida":     getattr(f, "departure", "—"),
                    "chegada":   getattr(f, "arrival", "—"),
                    "duracao":   getattr(f, "duration", "—"),
                    "escalas":   getattr(f, "stops", 0),
                    "preco_str": preco_str,
                    "preco":     preco_brl,
                    "melhor":    bool(getattr(f, "is_best", False)),
                    "fonte":     "Google Flights",
                    "rota_aero": rota_aero,
                }
                sig = _sig_oferta(oferta)
                if sig in seen:
                    continue
                seen.add(sig)
                ofertas.append(oferta)

        # Agregadores (Skyscanner/Kayak/Trip.com) — gerados uma vez por
        # leg usando o código primário (city code ou primeiro aeroporto).
        # Os buscadores aceitam city codes nativamente.
        rep_leg = Leg(leg.origem_iata, leg.destino_iata, leg.data)
        ofertas_agg = ofertas_agregadores(
            rep_leg, marker_env, v.classe_codigo, v.pax,
        )

        tp_ofertas: list = []
        if tp_token:
            # Travelpayouts/Aviasales aceita city codes nativamente.
            # Se a entrada NÃO tem vírgula, mandamos o código tal qual
            # (SAO retorna voos de GRU+CGH+VCP em 1 chamada). Se tem
            # vírgula (ex: GRU,CGH), iteramos os combos.
            usa_combos = ("," in leg.origem) or ("," in leg.destino)
            tp_combos = combos if usa_combos else [
                (leg.origem_iata, leg.destino_iata)
            ]
            for (orig_tp, dest_tp) in tp_combos:
                print(
                    f"[travelpayouts] token=***{tp_token[-4:]} "
                    f"{orig_tp}->{dest_tp} {leg.data_iso}...",
                    file=sys.stderr,
                )
                sub_leg = Leg(orig_tp, dest_tp, leg.data)
                resultado_tp = consultar_travelpayouts(
                    sub_leg, tp_token, classe=v.classe_codigo,
                )
                if resultado_tp:
                    from collections import Counter
                    cont = Counter(o['fonte'] for o in resultado_tp)
                    resumo = ", ".join(
                        f"{f}={n}" for f, n in cont.most_common()
                    )
                    print(
                        f"[travelpayouts] +{len(resultado_tp)} ofertas "
                        f"({resumo})",
                        file=sys.stderr,
                    )
                else:
                    print(
                        "[travelpayouts] 0 ofertas retornadas "
                        "(token, rota sem dados ou data muito próxima)",
                        file=sys.stderr,
                    )
                for o in resultado_tp:
                    if leg.multi_aeroportos:
                        # Quando temos os aeroportos reais da resposta,
                        # usamos eles. Caso contrário caímos no par
                        # consultado (orig_tp→dest_tp).
                        o_real = o.get("origem_real") or orig_tp
                        d_real = o.get("destino_real") or dest_tp
                        o["rota_aero"] = f"{o_real}→{d_real}"
                    else:
                        o["rota_aero"] = ""
                    sig = _sig_oferta(o)
                    if sig in seen:
                        continue
                    seen.add(sig)
                    tp_ofertas.append(o)
        else:
            print(
                "[travelpayouts] SEM token — defina TRAVELPAYOUTS_TOKEN "
                "pra ativar Skyscanner/Kiwi/Trip.com",
                file=sys.stderr,
            )

        # Dedup: se o TP retornou ofertas REAIS pra Skyscanner ou Trip.com,
        # remove a respectiva linha placeholder do agregador (evita duplicar
        # "Buscar ofertas — Skyscanner" junto da oferta real).
        fontes_reais_tp = {
            o["fonte"] for o in tp_ofertas
            if o.get("preco") is not None
        }
        ofertas_agg = [
            o for o in ofertas_agg
            if not (o["fonte"] in fontes_reais_tp and o.get("preco") is None)
        ]

        ofertas.extend(ofertas_agg)
        ofertas.extend(tp_ofertas)

        trechos.append({
            "leg": leg,
            "ofertas": ofertas,
            "current_price": current_price,
            "erro": last_error if not ofertas else None,
        })

    return {"trechos": trechos}


def _melhor_oferta(ofertas: list) -> Optional[dict]:
    com_preco = [o for o in ofertas if o["preco"] is not None]
    if com_preco:
        return min(com_preco, key=lambda o: o["preco"])
    return ofertas[0] if ofertas else None


def _escalas_int(valor) -> Optional[int]:
    """Converte campo 'escalas' (pode ser int, 'Unknown', '1 stop' etc.)
    em int. Retorna None se não conseguir."""
    if isinstance(valor, int):
        return valor
    if valor is None:
        return None
    s = str(valor).strip().lower()
    if s in ("", "unknown", "—", "-"):
        return None
    if "nonstop" in s or "non-stop" in s or "direto" in s or s == "0":
        return 0
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


def _filtrar_escalas(ofertas: list, max_escalas: Optional[int]) -> list:
    """Remove ofertas com mais escalas que o limite. Mantém ofertas com
    contagem desconhecida (para não esconder voos que podem servir)."""
    if max_escalas is None:
        return ofertas
    filtradas = []
    for o in ofertas:
        n = _escalas_int(o.get("escalas"))
        if n is None or n <= max_escalas:
            filtradas.append(o)
    return filtradas


def calcular_total_estimado(dados: Optional[dict],
                            max_escalas: Optional[int]) -> Optional[float]:
    """Soma o melhor preço de cada trecho. Retorna None se não der
    para somar todos os trechos (algum sem preço)."""
    total, _, _ = calcular_total_e_moeda(dados, max_escalas)
    return total


def calcular_total_e_moeda(
    dados: Optional[dict], max_escalas: Optional[int],
) -> tuple[Optional[float], str, list]:
    """Soma o melhor preço de cada trecho e detecta a moeda predominante
    do total. Retorna (total, moeda, melhores).

    moeda pode ser:
      - "BRL": todos os trechos já estão em BRL (ou foram convertidos)
      - "USD"/"EUR"/"GBP": todos em uma única moeda estrangeira (sem
        conversão disponível)
      - "MIX": trechos em moedas diferentes — total é só aproximação
    """
    if not dados:
        return None, "BRL", []
    total = 0.0
    melhores: list = []
    for td in dados.get("trechos", []):
        ofertas = _filtrar_escalas(td["ofertas"], max_escalas)
        com_preco = [o for o in ofertas if o["preco"] is not None]
        if not com_preco:
            return None, "BRL", melhores
        m = min(com_preco, key=lambda o: o["preco"])
        melhores.append(m)
        total += m["preco"]

    moedas = {_moeda_da_oferta(m) for m in melhores if m}
    if not moedas or moedas == {"BRL"}:
        moeda = "BRL"
    elif len(moedas) == 1:
        moeda = next(iter(moedas))
    else:
        moeda = "MIX"

    return (total if total > 0 else None), moeda, melhores


def formatar_brl(v: float) -> str:
    s = f"R$ {v:,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


_MOEDA_SIMBOLO_EXIBICAO = {
    "BRL": "R$",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
}


def formatar_moeda(valor: float, moeda: str = "BRL") -> str:
    """Formata um valor monetário com símbolo + separadores adequados.
    BRL usa estilo brasileiro (R$ 12.345,67); USD/EUR/GBP usam estilo
    inglês (US$ 12,345.67)."""
    moeda = (moeda or "BRL").upper()
    if moeda == "BRL":
        return formatar_brl(valor)
    simbolo = _MOEDA_SIMBOLO_EXIBICAO.get(moeda, f"{moeda} ")
    if moeda == "USD":
        simbolo = "US$ "
    return f"{simbolo}{valor:,.2f}"


def _moeda_da_oferta(oferta: Optional[dict]) -> str:
    """Detecta a moeda do preço *já considerando* eventual conversão.
    Se o preço veio com '(sem conversão)', a moeda é a do número original
    (ex.: USD), não BRL."""
    if not oferta:
        return "BRL"
    s = str(oferta.get("preco_str") or "")
    if "(sem conversão)" in s:
        s = s.replace("(sem conversão)", "").strip()
    return _detectar_moeda(s) or "BRL"


def _formatar_total_estimado(
    total: float, melhores: list
) -> tuple[str, bool]:
    """Formata o total considerando a moeda das melhores ofertas.

    Retorna (texto_formatado, convertido_para_brl). Quando todas as
    ofertas estão na mesma moeda estrangeira (cotação indisponível),
    mostramos o total nessa moeda em vez de fingir que é BRL.
    """
    moedas = {
        _moeda_da_oferta(m)
        for m in melhores
        if m and m.get("preco") is not None
    }
    if not moedas or moedas == {"BRL"}:
        return formatar_brl(total), True
    if len(moedas) == 1:
        moeda = next(iter(moedas))
        simbolo = _MOEDA_SIMBOLO_EXIBICAO.get(moeda, f"{moeda} ")
        valor = f"{total:,.0f}".replace(",", ".")
        return f"{simbolo}{valor} (sem conversão)", False
    return (
        f"~{formatar_brl(total)} (moedas mistas — total aproximado)",
        False,
    )


CALLMEBOT_API = "https://api.callmebot.com/whatsapp.php"
ALERTA_CACHE = Path(__file__).parent / ".alerta_cache.json"


def formatar_numero_br(num: str) -> str:
    """Remove não-dígitos e adiciona o código 55 do Brasil se necessário."""
    s = re.sub(r"\D", "", str(num))
    if s.startswith("55"):
        return s
    if len(s) in (10, 11):
        return "55" + s
    return s


def enviar_whatsapp_callmebot(numero: str, mensagem: str,
                              apikey: str, dry_run: bool = False) -> str:
    """Envia mensagem WhatsApp via CallMeBot (https://www.callmebot.com).
    Setup (uma vez só):
      1. Adicione +34 644 51 95 23 nos seus contatos como 'CallMeBot'
      2. Pelo WhatsApp, envie a mensagem:
         'I allow callmebot to send me messages'
      3. Aguarde ~2 min — você receberá a API key por WhatsApp
      4. Use a key via --whatsapp-apikey ou env var CALLMEBOT_APIKEY
    """
    num = formatar_numero_br(numero)
    url = CALLMEBOT_API + "?" + urlencode({
        "phone": num, "text": mensagem, "apikey": apikey,
    })
    if dry_run:
        return (
            f"[dry-run] número formatado: {num}\n"
            f"--- mensagem que seria enviada ---\n{mensagem}\n"
            f"--- fim da mensagem ---"
        )
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return f"[ok {r.status}] " + r.read().decode(
                "utf-8", errors="ignore"
            )[:140]
    except urllib.error.HTTPError as e:
        return f"[erro http {e.code}] {e.read().decode('utf-8', 'ignore')[:140]}"
    except Exception as e:
        return f"[erro] {e}"


def agora_br() -> datetime:
    """Hora atual no fuso de Brasília (usado no monitor e no GitHub Actions)."""
    return datetime.now(TZ_BR)


def dentro_da_janela(agora: datetime, h_inicio: int, h_fim: int) -> bool:
    """Verifica se a hora atual está na janela [h_inicio, h_fim).
    Suporta janelas que cruzam meia-noite (ex: 22 -> 1)."""
    h = agora.hour + agora.minute / 60.0
    if h_inicio < h_fim:
        return h_inicio <= h < h_fim
    return h >= h_inicio or h < h_fim


def _carregar_cache_alerta() -> dict:
    if ALERTA_CACHE.exists():
        try:
            return json.loads(ALERTA_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _salvar_cache_alerta(d: dict) -> None:
    try:
        ALERTA_CACHE.write_text(
            json.dumps(d, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        print(f"[aviso] não consegui salvar cache: {e}", file=sys.stderr)


def avaliar_alerta(
    total: float,
    preco_min: float = 0,
    preco_max: Optional[float] = None,
    moeda: str = "BRL",
    preco_min_usd: Optional[float] = None,
    preco_max_usd: Optional[float] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Decide se o total estimado dispara alerta.

    Compara contra a faixa-alvo na MOEDA do total:
      - moeda='BRL' → usa (preco_min, preco_max) em reais.
        Se preco_max for None, alerta em BRL fica DESATIVADO.
      - moeda='USD' → usa (preco_min_usd, preco_max_usd) se fornecidos.
        Se preco_max_usd for None, alerta em USD fica DESATIVADO.
      - moeda='EUR'/'GBP'/'MIX' → sem alerta (sem threshold definido).

    Retorna (nível, emoji) se deve alertar, ou (None, None).
    """
    if moeda == "BRL" and preco_max is not None:
        if total < preco_min:
            return (
                f"promoção excepcional (abaixo de "
                f"{formatar_brl(preco_min)})",
                "🔥",
            )
        if total <= preco_max:
            return (
                f"dentro da faixa-alvo ({formatar_brl(preco_min)}–"
                f"{formatar_brl(preco_max)})",
                "🎯",
            )
        return None, None

    if moeda == "USD" and preco_max_usd is not None:
        pmin = preco_min_usd or 0.0
        if total < pmin:
            return (
                f"promoção excepcional (abaixo de "
                f"{formatar_moeda(pmin, 'USD')})",
                "🔥",
            )
        if total <= preco_max_usd:
            return (
                f"dentro da faixa-alvo "
                f"({formatar_moeda(pmin, 'USD')}–"
                f"{formatar_moeda(preco_max_usd, 'USD')})",
                "🎯",
            )
        return None, None

    return None, None


def pode_enviar_alerta(cache: dict, chave: str, total: float,
                       cooldown_h: float, agora: datetime,
                       moeda: str = "BRL") -> bool:
    info = cache.get(chave, {})
    ult_preco = info.get("ultimo_preco")
    ult_moeda = info.get("moeda", "BRL")
    ult_ts = info.get("timestamp")
    if ult_preco is None or not ult_ts:
        return True
    # Se a moeda mudou, o total não é comparável — pode enviar
    if ult_moeda != moeda:
        return True
    try:
        dt_ult = datetime.fromisoformat(ult_ts)
        if dt_ult.tzinfo is None and agora.tzinfo is not None:
            dt_ult = dt_ult.replace(tzinfo=agora.tzinfo)
        horas = (agora - dt_ult).total_seconds() / 3600
        mudou = abs(total - ult_preco) / max(ult_preco, 1) >= 0.03
        return not (horas < cooldown_h and not mudou)
    except Exception:
        return True


def montar_mensagem_alerta(v: Viagem, total: float, nivel: str,
                           emoji: str,
                           url_relatorio: Optional[str] = None,
                           moeda: str = "BRL") -> str:
    trechos_txt = " + ".join(
        f"{L.origem_iata}→{L.destino_iata} {L.data_br}" for L in v.legs
    )
    linhas = [
        f"{emoji} ALERTA DE PREÇO",
        f"Rota: {v.rota_resumo}",
        f"Trechos: {trechos_txt}",
        f"Classe: {v.classe_label}",
        f"Total estimado: {formatar_moeda(total, moeda)}",
        f"Status: {nivel}",
    ]
    if url_relatorio:
        linhas.append(f"Relatório: {url_relatorio}")
    linhas.append("Conferir agora antes de mudar!")
    return "\n".join(linhas)


def _post_multipart(url: str, campos: list[tuple[str, str]],
                    arquivo: Optional[tuple[str, str, bytes]] = None,
                    timeout: int = 30) -> str:
    """POST multipart/form-data simples (sem deps). Retorna corpo decodificado.
    campos: lista de (nome, valor). arquivo: (nome_campo, filename, bytes)."""
    boundary = "----flyfinder" + str(int(time.time() * 1000))
    partes: list[bytes] = []
    for nome, val in campos:
        partes.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{nome}"\r\n\r\n'
            f"{val}\r\n".encode("utf-8")
        )
    if arquivo:
        nome_campo, filename, conteudo = arquivo
        partes.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{nome_campo}"; '
            f'filename="{filename}"\r\n'
            f'Content-Type: text/html; charset=utf-8\r\n\r\n'.encode("utf-8")
        )
        partes.append(conteudo)
        partes.append(b"\r\n")
    partes.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(partes)
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "flyfinder-agent/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore").strip()


def _upload_gist(html_str: str, nome: str, token: str) -> Optional[str]:
    """Cria um gist secreto com o HTML e devolve URL htmlpreview que
    renderiza corretamente. Retorna None se falhar."""
    payload = json.dumps({
        "description": "Relatório de passagens (flyfinder-agent)",
        "public": False,
        "files": {nome: {"content": html_str}},
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.github.com/gists",
        data=payload,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "flyfinder-agent/1.0",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    raw = data.get("files", {}).get(nome, {}).get("raw_url")
    if not raw:
        return None
    return f"https://htmlpreview.github.io/?{raw}"


def upload_html_publico(html_str: str,
                        nome: str = "relatorio.html") -> Optional[str]:
    """Sobe o HTML para um hospedeiro público e retorna a URL.
    Cascata: GitHub Gist (renderiza correto) → catbox.moe → 0x0.st.
    Retorna None se todos falharem — o alerta segue sem link."""
    conteudo = html_str.encode("utf-8")
    token = os.getenv("GIST_TOKEN") or os.getenv("GH_TOKEN")

    if token:
        try:
            url = _upload_gist(html_str, nome, token)
            if url:
                return url
        except Exception as e:
            print(f"[aviso] gist falhou: {e}", file=sys.stderr)

    try:
        url = _post_multipart(
            "https://catbox.moe/user/api.php",
            campos=[("reqtype", "fileupload")],
            arquivo=("fileToUpload", nome, conteudo),
        )
        if url.startswith("http"):
            return url
        print(f"[aviso] catbox.moe respondeu: {url[:140]}", file=sys.stderr)
    except Exception as e:
        print(f"[aviso] catbox.moe falhou: {e}", file=sys.stderr)

    try:
        url = _post_multipart(
            "https://0x0.st", campos=[],
            arquivo=("file", nome, conteudo),
        )
        if url.startswith("http"):
            return url
        print(f"[aviso] 0x0.st respondeu: {url[:140]}", file=sys.stderr)
    except Exception as e:
        print(f"[aviso] 0x0.st falhou: {e}", file=sys.stderr)

    return None


def monitorar(v: Viagem, args) -> None:
    """Loop de monitoramento: checa preços periodicamente dentro da janela
    de horário e envia alerta WhatsApp quando o total estimado entra na
    faixa-alvo. Pressione Ctrl+C para parar."""
    apikey = args.whatsapp_apikey or os.getenv("CALLMEBOT_APIKEY")
    if not args.dry_run and (not args.whatsapp or not apikey):
        print(
            "[erro] modo monitor precisa de --whatsapp e --whatsapp-apikey "
            "(ou env CALLMEBOT_APIKEY). Use --dry-run para testar sem enviar.",
            file=sys.stderr,
        )
        sys.exit(2)

    intervalo_s = max(60, args.intervalo * 60)
    cooldown_h = max(0.5, args.cooldown)
    if args.preco_max is not None:
        faixa_brl_txt = (
            f"Faixa-alvo BRL: {formatar_brl(args.preco_min)} a "
            f"{formatar_brl(args.preco_max)}\n"
        )
    else:
        faixa_brl_txt = "Faixa-alvo BRL: (desativada)\n"
    if args.preco_max_usd is not None:
        faixa_usd_txt = (
            f"Faixa-alvo USD: {formatar_moeda(args.preco_min_usd, 'USD')} "
            f"a {formatar_moeda(args.preco_max_usd, 'USD')}\n"
        )
    else:
        faixa_usd_txt = "Faixa-alvo USD: (desativada)\n"
    print(
        f"\n=== Monitor de preços ativado ===\n"
        f"Rota:        {v.rota_resumo}\n"
        f"{faixa_brl_txt}"
        f"{faixa_usd_txt}"
        f"Janela:      {args.horario_inicio}h às {args.horario_fim}h\n"
        f"Intervalo:   {args.intervalo} min entre checagens\n"
        f"Cooldown:    {cooldown_h}h entre alertas iguais\n"
        f"WhatsApp:    {args.whatsapp or '(dry-run)'}\n"
        f"Ctrl+C para parar.\n",
        file=sys.stderr,
    )

    cache = _carregar_cache_alerta()
    chave = v.rota_resumo  # uma chave de cache por rota

    while True:
        agora = agora_br()
        marca = agora.strftime("%d/%m %H:%M")
        if not dentro_da_janela(
            agora, args.horario_inicio, args.horario_fim
        ):
            print(
                f"[{marca}] fora da janela "
                f"({args.horario_inicio}h-{args.horario_fim}h), aguardando "
                f"{args.intervalo} min...",
                file=sys.stderr,
            )
            time.sleep(intervalo_s)
            continue

        print(f"[{marca}] checando preços...", file=sys.stderr)
        dados = consultar_google_flights(v)
        total, moeda, _ = calcular_total_e_moeda(dados, v.max_escalas)
        if total is None:
            print(
                f"[{marca}] não foi possível calcular o total estimado "
                f"(algum trecho sem preço). Tentando de novo em "
                f"{args.intervalo} min.",
                file=sys.stderr,
            )
            time.sleep(intervalo_s)
            continue

        print(
            f"[{marca}] total estimado: {formatar_moeda(total, moeda)} "
            f"(moeda={moeda})",
            file=sys.stderr,
        )

        nivel, emoji = avaliar_alerta(
            total, args.preco_min, args.preco_max,
            moeda=moeda,
            preco_min_usd=args.preco_min_usd,
            preco_max_usd=args.preco_max_usd,
        )

        if not nivel:
            if moeda == "BRL" and args.preco_max is not None:
                alvo = formatar_brl(args.preco_max)
            elif moeda == "USD" and args.preco_max_usd is not None:
                alvo = formatar_moeda(args.preco_max_usd, "USD")
            else:
                alvo = f"(sem faixa-alvo definida pra {moeda})"
            print(
                f"[{marca}] fora da faixa-alvo (> {alvo}). Sem alerta.",
                file=sys.stderr,
            )
            time.sleep(intervalo_s)
            continue

        envia = pode_enviar_alerta(
            cache, chave, total, cooldown_h, agora, moeda=moeda,
        )
        if not envia:
            print(
                f"[{marca}] alerta semelhante recente "
                f"(cooldown {cooldown_h}h). Pulando.",
                file=sys.stderr,
            )

        if envia:
            url_html = None
            if not args.sem_html_link:
                links_dl = gerar_deeplinks(v)
                html_str = render_html(v, links_dl, dados)
                print(f"[{marca}] subindo relatório HTML...",
                      file=sys.stderr)
                url_html = upload_html_publico(html_str)
                if url_html:
                    print(f"[{marca}] relatório: {url_html}",
                          file=sys.stderr)
            msg = montar_mensagem_alerta(
                v, total, nivel, emoji, url_html, moeda=moeda,
            )
            r = enviar_whatsapp_callmebot(
                args.whatsapp or "", msg, apikey or "", dry_run=args.dry_run,
            )
            print(f"[{marca}] WhatsApp → {r}", file=sys.stderr)
            cache[chave] = {
                "ultimo_preco": total,
                "moeda": moeda,
                "timestamp": agora.isoformat(),
                "nivel": nivel,
                "url": url_html,
            }
            _salvar_cache_alerta(cache)

        time.sleep(intervalo_s)


def render_markdown(v: Viagem, links: list, dados: Optional[dict]) -> str:
    out: list[str] = []
    out.append(f"# Resultado da Pesquisa de Passagens — {v.rota_resumo}\n")
    out.append(f"_Tipo de viagem: **{v.trip_type}** • "
               f"{v.pax} adulto(s) • {v.classe_label}_\n")

    out.append("## Trechos da viagem\n")
    out.append("| # | Origem | Destino | Data |")
    out.append("|---|--------|---------|------|")
    for i, L in enumerate(v.legs, 1):
        out.append(
            f"| {i} | {L.origem_label} | {L.destino_label} | {L.data_br} |"
        )
    out.append("")

    trechos_dados = (dados or {}).get("trechos") or []
    for td in trechos_dados:
        td["ofertas"] = _filtrar_escalas(td["ofertas"], v.max_escalas)
    if v.max_escalas is not None:
        out.append(
            f"_Filtro aplicado: no máximo **{v.max_escalas}** escala(s) "
            f"por trecho_\n"
        )
    if trechos_dados:
        melhores = []
        total = 0.0
        total_ok = True
        for td in trechos_dados:
            m = _melhor_oferta(td["ofertas"])
            melhores.append(m)
            if m and m.get("preco") is not None:
                total += m["preco"]
            else:
                total_ok = False

        out.append("## Resumo Executivo\n")
        for i, (td, m) in enumerate(zip(trechos_dados, melhores), 1):
            L = td["leg"]
            if m:
                rota_extra = (
                    f" via {m.get('rota_aero')}"
                    if L.multi_aeroportos and m.get("rota_aero") else ""
                )
                out.append(
                    f"- **Trecho {i}** ({L.origem_label} → {L.destino_label}, "
                    f"{L.data_br}): melhor {m['preco_str']} — {m['cia']} "
                    f"({m['duracao']}){rota_extra}"
                )
            else:
                out.append(
                    f"- **Trecho {i}** ({L.origem_label} → {L.destino_label}, "
                    f"{L.data_br}): _sem ofertas retornadas_"
                )
        if total_ok:
            total_str, _ = _formatar_total_estimado(total, melhores)
            out.append(
                f"- **Total estimado (passagens separadas):** {total_str}"
            )
        out.append("")

        for i, td in enumerate(trechos_dados, 1):
            L = td["leg"]
            out.append(
                f"## Trecho {i}: {L.origem_label} → {L.destino_label} "
                f"({L.data_br})\n"
            )
            if td.get("erro"):
                out.append(f"> ⚠️ Erro ao buscar: {td['erro']}\n")
                continue
            if not td["ofertas"]:
                out.append("> Sem ofertas retornadas.\n")
                continue
            nivel = td.get("current_price")
            if nivel:
                out.append(f"_Nível de preço Google: **{nivel}**_\n")

            com_preco = sorted(
                [o for o in td["ofertas"] if o.get("preco") is not None],
                key=lambda o: o["preco"],
            )
            sem_preco = [
                o for o in td["ofertas"] if o.get("preco") is None
            ]
            por_preco = com_preco[:12] + sem_preco
            mostrar_rota = L.multi_aeroportos
            cabecalho = (
                "| # | Companhia | Saída | Chegada | Duração | Escalas | "
                "Preço | Fonte |"
            )
            sep = (
                "|---|-----------|-------|---------|---------|---------|"
                "-------|-------|"
            )
            if mostrar_rota:
                cabecalho = (
                    "| # | Rota | Companhia | Saída | Chegada | Duração | "
                    "Escalas | Preço | Fonte |"
                )
                sep = (
                    "|---|------|-----------|-------|---------|---------|"
                    "---------|-------|-------|"
                )
            cabecalho += " Reservar |"
            sep += "----------|"
            out.append(cabecalho)
            out.append(sep)
            for j, o in enumerate(por_preco, 1):
                marca = " ⭐" if o["melhor"] else ""
                link = gerar_link_oferta(
                    td["leg"], o, v.classe_label, v.pax,
                )
                fonte = o.get("fonte") or "Google Flights"
                rota_col = ""
                if mostrar_rota:
                    rota = (o.get("rota_aero") or "").strip() or "—"
                    rota_col = f" {rota} |"
                out.append(
                    f"| {j} |{rota_col} {o['cia']}{marca} | {o['saida']} | "
                    f"{o['chegada']} | {o['duracao']} | {o['escalas']} | "
                    f"{o['preco_str']} | {fonte} | [Buscar]({link}) |"
                )
            out.append("")
    else:
        out.append(
            "> Para ver preços reais no terminal, instale: "
            "`pip install fast-flights`.\n"
        )

    out.append("## Links diretos nos buscadores (já com sua busca preenchida)\n")
    for link in links:
        out.append(f"- **{link['nome']}**: {link['url']}")
    out.append("")

    out.append("## Reservar direto no site da companhia\n")
    out.append(
        "_Dica: comprar direto com a cia geralmente tem **menos taxas, "
        "milhas e suporte mais fácil em caso de remarcação**._\n"
    )
    for c in COMPANHIAS:
        out.append(
            f"- {c['pais']} **{c['nome']}** (`{c['iata']}`) — {c['busca']}"
        )
    out.append("")

    out.append(
        "> ⚠️ Preços são dinâmicos. Em viagens multi-cidade, comprar como "
        "passagem única costuma ser mais barato que somar trechos separados — "
        "use os links acima para conferir a tarifa real."
    )
    return "\n".join(out)


_HTML_TEMPLATE = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pesquisa de Passagens — {rota}</title>
{travelpayouts_drive}
<style>
  :root {{
    --bg: #0f172a; --panel: #1e293b; --panel2: #273449;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8;
    --good: #34d399; --warn: #fbbf24; --border: #334155;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
      Roboto, "Helvetica Neue", Arial, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.5;
  }}
  .wrap {{ max-width: 1100px; margin: 0 auto; padding: 24px 20px 60px; }}
  header {{
    background: linear-gradient(135deg, #0ea5e9 0%, #6366f1 100%);
    color: #fff; padding: 28px 32px; border-radius: 16px;
    margin-bottom: 24px; box-shadow: 0 10px 30px rgba(0,0,0,.3);
  }}
  header h1 {{ margin: 0 0 6px; font-size: 26px; }}
  header p {{ margin: 0; opacity: .9; font-size: 14px; }}
  .grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px; margin-bottom: 24px;
  }}
  .card {{
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 14px 16px;
  }}
  .card .label {{ color: var(--muted); font-size: 12px;
    text-transform: uppercase; letter-spacing: .04em; }}
  .card .value {{ font-size: 18px; font-weight: 600; margin-top: 4px; }}
  h2 {{ font-size: 18px; margin: 28px 0 12px; color: var(--accent); }}
  h3 {{ font-size: 16px; margin: 22px 0 10px; color: var(--text); }}
  .leg-card {{
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 14px 16px; margin-bottom: 10px;
    display: flex; align-items: center; gap: 14px;
  }}
  .leg-num {{
    background: var(--accent); color: #0f172a; font-weight: 700;
    width: 28px; height: 28px; border-radius: 50%;
    display: inline-flex; align-items: center; justify-content: center;
    flex-shrink: 0;
  }}
  .leg-route {{ font-weight: 600; font-size: 16px; }}
  .leg-date {{ color: var(--muted); font-size: 13px; margin-left: auto; }}
  .summary {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 12px;
  }}
  .summary .card .value {{ color: var(--good); font-size: 18px; }}
  .total {{
    background: linear-gradient(135deg, #34d399 0%, #10b981 100%);
    color: #0f172a; padding: 16px 20px; border-radius: 12px;
    font-weight: 700; font-size: 18px; margin: 14px 0;
  }}
  .total small {{ font-weight: 500; opacity: .85; display: block;
    font-size: 12px; margin-top: 4px; }}
  table {{
    width: 100%; border-collapse: collapse; background: var(--panel);
    border-radius: 12px; overflow: hidden; font-size: 14px;
    margin-bottom: 8px;
  }}
  th, td {{ padding: 10px 12px; text-align: left;
    border-bottom: 1px solid var(--border); }}
  th {{ background: var(--panel2); color: var(--muted);
    font-weight: 600; font-size: 12px; text-transform: uppercase;
    letter-spacing: .04em; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: rgba(56,189,248,.06); }}
  tr.clickable {{ cursor: pointer; transition: all .15s; }}
  tr.clickable:hover td {{ background: rgba(56,189,248,.12); }}
  tr.clickable:hover td:first-child {{ color: var(--accent); }}
  .open-icon {{ color: var(--muted); font-size: 12px; margin-left: 4px;
    opacity: .6; transition: opacity .15s; }}
  tr.clickable:hover .open-icon {{ opacity: 1; color: var(--accent); }}
  .fonte {{ display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: 11px; font-weight: 600; white-space: nowrap; }}
  .fonte-google     {{ background: rgba(56,189,248,.15); color: #38bdf8; }}
  .fonte-skyscanner {{ background: rgba(168,85,247,.15); color: #a855f7; }}
  .fonte-kayak      {{ background: rgba(255,90,90,.15);  color: #ff6b6b; }}
  .fonte-kiwi       {{ background: rgba(244,114,182,.15); color: #f472b6; }}
  .fonte-trip       {{ background: rgba(251,146,60,.15); color: #fb923c; }}
  .fonte-outras     {{ background: rgba(148,163,184,.15); color: #94a3b8; }}
  tr.agregador-row td {{
    background: rgba(56,189,248,.04);
    border-left: 3px solid var(--accent);
  }}
  tr.agregador-row td:first-child {{ padding-left: 9px; }}
  tr.agregador-placeholder td.price {{ color: var(--muted); font-weight: 500; }}
  tr.agregador-placeholder td {{ opacity: .85; }}
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: 11px; font-weight: 600; margin-left: 6px;
  }}
  .badge.best {{ background: rgba(52,211,153,.15); color: var(--good); }}
  .badge.agg  {{ background: rgba(56,189,248,.18); color: var(--accent); }}
  .price {{ font-weight: 700; color: var(--good); white-space: nowrap; }}
  .links {{ display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }}
  .links a {{
    display: block; background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 14px; color: var(--text);
    text-decoration: none; transition: all .15s;
  }}
  .links a:hover {{ border-color: var(--accent);
    transform: translateY(-1px); color: var(--accent); }}
  .links .site {{ font-weight: 600; }}
  .links .url {{ font-size: 11px; color: var(--muted);
    word-break: break-all; margin-top: 4px; }}
  .airlines {{ display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }}
  .airlines a {{
    display: flex; align-items: center; gap: 12px;
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 14px 16px; color: var(--text);
    text-decoration: none; transition: all .15s;
  }}
  .airlines a:hover {{ border-color: var(--accent);
    transform: translateY(-1px); }}
  .airlines .flag {{ font-size: 28px; line-height: 1; }}
  .airlines .name {{ font-weight: 600; font-size: 15px; }}
  .airlines .iata {{ color: var(--muted); font-size: 12px;
    font-family: monospace; }}
  .airlines .hint {{ color: var(--muted); font-size: 11px;
    margin-top: 2px; }}
  .notice {{
    background: rgba(251,191,36,.1); border: 1px solid rgba(251,191,36,.3);
    color: var(--warn); padding: 12px 16px; border-radius: 10px;
    font-size: 13px; margin: 20px 0;
  }}
  .actions {{ display: flex; flex-wrap: wrap; gap: 10px;
    margin: 16px 0 8px; }}
  .btn {{
    display: inline-flex; align-items: center; gap: 8px;
    padding: 12px 18px; border-radius: 12px; font-weight: 600;
    text-decoration: none; font-size: 14px;
    transition: all .15s; cursor: pointer; border: none;
  }}
  .btn.primary {{
    background: linear-gradient(135deg, #34d399 0%, #10b981 100%);
    color: #0f172a;
  }}
  .btn.primary:hover {{ transform: translateY(-1px);
    box-shadow: 0 8px 20px rgba(52,211,153,.3); }}
  .btn.secondary {{
    background: var(--panel); color: var(--text);
    border: 1px solid var(--border);
  }}
  .btn.secondary:hover {{ border-color: var(--accent); color: var(--accent); }}
  footer {{ margin-top: 32px; color: var(--muted); font-size: 12px;
    text-align: center; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>✈️ {rota}</h1>
    <p>{trip_label} • {n_trechos} trecho(s) • {pax} adulto(s) • {classe}{filtro_label}</p>
  </header>

  {acoes_html}

  <h2>Trechos da viagem</h2>
  {trechos_cards}

  {resumo_html}
  {detalhes_html}

  <h2>Links diretos nos buscadores (com sua pesquisa preenchida)</h2>
  <div class="links">
    {links_html}
  </div>

  <h2>Reservar direto no site da companhia</h2>
  <p style="color:var(--muted);font-size:13px;margin:0 0 12px">
    Comprar direto com a cia costuma render <b>menos taxas</b>,
    <b>acúmulo de milhas</b> e <b>suporte mais fácil em caso de remarcação</b>.
  </p>
  <div class="airlines">
    {cias_html}
  </div>

  <div class="notice">
    Preços são dinâmicos. Em viagens multi-cidade, comprar como passagem
    única costuma sair mais barato que somar trechos separados — use os
    links acima para conferir tarifas combinadas. Pesquisa em
    {gerado_em}.
  </div>

  <footer>Gerado por buscar_passagens.py</footer>
</div>
</body>
</html>
"""


def _snippet_travelpayouts_drive() -> str:
    """Retorna o <script> do Travelpayouts Drive (tracking de afiliado).
    Ativa se TRAVELPAYOUTS_DRIVE_ID ou TRAVELPAYOUTS_DRIVE_URL existirem
    no ambiente. Sem env var, retorna string vazia (sem tracker)."""
    url = os.getenv("TRAVELPAYOUTS_DRIVE_URL", "").strip()
    if not url:
        drive_id = os.getenv("TRAVELPAYOUTS_DRIVE_ID", "").strip()
        if not drive_id:
            return ""
        import base64
        slug = base64.b64encode(drive_id.encode()).decode().rstrip("=")
        url = f"https://emrldtp.cc/{slug}.js?t={drive_id}"
    return (
        '<script nowprocket data-noptimize="1" data-cfasync="false" '
        'data-wpfc-render="false" seraph-accel-crit="1" data-no-defer="1">'
        '(function(){var s=document.createElement("script");s.async=1;'
        f's.src={json.dumps(url)};document.head.appendChild(s);}})();'
        "</script>"
    )


def render_html(v: Viagem, links: list, dados: Optional[dict],
                run_workflow_url: Optional[str] = None) -> str:
    e = html.escape

    trip_labels = {
        "one-way":    "Só ida",
        "round-trip": "Ida e volta",
        "multi-city": "Multi-cidade",
    }

    if run_workflow_url is None:
        run_workflow_url = os.getenv("WORKFLOW_URL") or (
            "https://github.com/flimasales/flyfinder-agent/actions/"
            "workflows/monitor-precos.yml"
        )

    trechos_cards = "".join(
        f'<div class="leg-card">'
        f'<span class="leg-num">{i}</span>'
        f'<span class="leg-route">'
        f'{e(L.origem_label)} → {e(L.destino_label)}</span>'
        f'<span class="leg-date">{e(L.data_br)}</span>'
        f'</div>'
        for i, L in enumerate(v.legs, 1)
    )

    trechos_dados = (dados or {}).get("trechos") or []
    for td in trechos_dados:
        td["ofertas"] = _filtrar_escalas(td["ofertas"], v.max_escalas)

    resumo_html = ""
    detalhes_html = ""

    if trechos_dados:
        cards = []
        melhores = []
        total = 0.0
        total_ok = True
        for i, td in enumerate(trechos_dados, 1):
            L = td["leg"]
            m = _melhor_oferta(td["ofertas"])
            melhores.append(m)
            if m:
                if m.get("preco") is not None:
                    total += m["preco"]
                else:
                    total_ok = False
                rota_extra = ""
                if L.multi_aeroportos and m.get("rota_aero"):
                    rota_extra = (
                        f'<div class="label" style="margin-top:2px">'
                        f'via {e(m["rota_aero"])}</div>'
                    )
                cards.append(
                    f'<div class="card">'
                    f'<div class="label">Trecho {i} — '
                    f'{e(L.origem_iata)} → {e(L.destino_iata)}</div>'
                    f'<div class="value">{e(str(m["preco_str"]))}</div>'
                    f'<div class="label" style="margin-top:4px">'
                    f'{e(m["cia"])} • {e(str(m["duracao"]))}</div>'
                    f'{rota_extra}'
                    f'</div>'
                )
            else:
                total_ok = False
                cards.append(
                    f'<div class="card">'
                    f'<div class="label">Trecho {i} — '
                    f'{e(L.origem_iata)} → {e(L.destino_iata)}</div>'
                    f'<div class="value" style="color:var(--warn)">'
                    f'sem ofertas</div></div>'
                )

        total_html = ""
        if total_ok and total > 0:
            total_str, _ = _formatar_total_estimado(total, melhores)
            total_html = (
                f'<div class="total">Total estimado: {e(total_str)}'
                f'<small>Soma do melhor preço de cada trecho '
                f'(passagens separadas). Comprar combinado nos links '
                f'abaixo pode ser mais barato.</small></div>'
            )

        resumo_html = (
            "<h2>Resumo Executivo</h2>"
            f'<div class="summary">{"".join(cards)}</div>'
            f"{total_html}"
        )

        secoes = []
        for i, td in enumerate(trechos_dados, 1):
            L = td["leg"]
            titulo = (
                f"Trecho {i}: {e(L.origem_label)} → {e(L.destino_label)} "
                f"<small style='color:var(--muted);font-weight:400;font-size:13px'>"
                f"({e(L.data_br)})</small>"
            )
            if td.get("erro"):
                secoes.append(
                    f"<h3>{titulo}</h3>"
                    f'<div class="notice">Erro: {e(td["erro"])}</div>'
                )
                continue
            if not td["ofertas"]:
                secoes.append(
                    f"<h3>{titulo}</h3>"
                    f'<div class="notice">Sem ofertas retornadas.</div>'
                )
                continue

            FONTES_DESTAQUE = ("Skyscanner", "Kayak", "Trip.com")
            destaque_por_fonte: dict[str, dict] = {}
            for o in td["ofertas"]:
                f = o.get("fonte") or ""
                if f not in FONTES_DESTAQUE:
                    continue
                cur = destaque_por_fonte.get(f)
                if cur is None:
                    destaque_por_fonte[f] = o
                    continue
                cur_preco = cur.get("preco")
                novo_preco = o.get("preco")
                if cur_preco is None and novo_preco is not None:
                    destaque_por_fonte[f] = o
                elif (cur_preco is not None and novo_preco is not None
                      and novo_preco < cur_preco):
                    destaque_por_fonte[f] = o

            destaque_rows = list(destaque_por_fonte.values())
            destaque_rows.sort(
                key=lambda o: (
                    0 if o.get("preco") is not None else 1,
                    o.get("preco") if o.get("preco") is not None else 0,
                    FONTES_DESTAQUE.index(o["fonte"]),
                )
            )
            ids_destaque = {id(o) for o in destaque_rows}

            com_preco_outros = sorted(
                [o for o in td["ofertas"]
                 if o.get("preco") is not None
                 and id(o) not in ids_destaque],
                key=lambda o: o["preco"],
            )
            sem_preco_outros = [
                o for o in td["ofertas"]
                if o.get("preco") is None
                and id(o) not in ids_destaque
            ]
            por_preco = (
                destaque_rows + com_preco_outros[:10] + sem_preco_outros
            )
            mostrar_rota = L.multi_aeroportos
            linhas = []
            for o in por_preco:
                badges = []
                if o.get("melhor"):
                    badges.append(
                        '<span class="badge best">Melhor</span>'
                    )
                fonte = o.get("fonte") or "Google Flights"
                is_destaque = fonte in FONTES_DESTAQUE
                tem_preco = o.get("preco") is not None
                if is_destaque and tem_preco:
                    badges.append(
                        f'<span class="badge agg">{e(fonte)}</span>'
                    )
                link_oferta = o.get("link") or gerar_link_oferta(
                    td["leg"], o, v.classe_label, v.pax,
                )
                titulo_link = (
                    f"Abrir a oferta no {fonte}"
                    if o.get("link")
                    else "Abrir esta oferta no Google Flights"
                )
                cls_fonte = (
                    "google" if "Google" in fonte
                    else "skyscanner" if "Skyscanner" in fonte
                    else "kayak" if "Kayak" in fonte
                    else "kiwi" if "Kiwi" in fonte
                    else "trip" if "Trip" in fonte
                    else "outras"
                )
                tr_class = "clickable"
                if is_destaque:
                    tr_class += " agregador-row"
                if is_destaque and not tem_preco:
                    tr_class += " agregador-placeholder"
                href_oferta = e(link_oferta)
                badges_html = "".join(badges)
                rota_td = ""
                if mostrar_rota:
                    rota_val = (o.get("rota_aero") or "").strip() or "—"
                    rota_td = (
                        f'<td style="font-family:monospace;'
                        f'color:var(--accent);font-size:13px">'
                        f'{e(rota_val)}</td>'
                    )
                linhas.append(
                    f'<tr class="{tr_class}" '
                    f'data-href="{href_oferta}" '
                    f'title="{e(titulo_link)}">'
                    f"{rota_td}"
                    f"<td>{e(o['cia'])}{badges_html}</td>"
                    f"<td>{e(str(o['saida']))}</td>"
                    f"<td>{e(str(o['chegada']))}</td>"
                    f"<td>{e(str(o['duracao']))}</td>"
                    f"<td>{e(str(o['escalas']))}</td>"
                    f"<td class='price'>{e(str(o['preco_str']))} "
                    f'<span class="open-icon">↗</span></td>'
                    f'<td><span class="fonte fonte-{cls_fonte}">'
                    f"{e(fonte)}</span></td>"
                    f"</tr>"
                )
            nivel = td.get("current_price") or "—"
            th_rota = "<th>Rota</th>" if mostrar_rota else ""
            secoes.append(
                f"<h3>{titulo}</h3>"
                f"<div class='label' style='color:var(--muted);"
                f"font-size:12px;margin-bottom:6px'>"
                f"Nível de preço Google: <b style='color:var(--warn)'>"
                f"{e(str(nivel))}</b></div>"
                f"<table><thead><tr>"
                f"{th_rota}"
                f"<th>Companhia</th><th>Saída</th><th>Chegada</th>"
                f"<th>Duração</th><th>Escalas</th><th>Preço</th>"
                f"<th>Fonte</th>"
                f"</tr></thead><tbody>{''.join(linhas)}</tbody></table>"
            )
        detalhes_html = "<h2>Ofertas por trecho (Google Flights)</h2>" + \
            "".join(secoes)
    else:
        resumo_html = (
            '<div class="notice">Para ver preços reais aqui, instale: '
            '<code>pip install fast-flights</code> e rode novamente.</div>'
        )

    links_html = "".join(
        f'<a href="{e(L["url"])}" target="_blank" rel="noopener">'
        f'<div class="site">{e(L["nome"])}</div>'
        f'<div class="url">{e(L["url"])}</div></a>'
        for L in links
    )

    cias_html = "".join(
        f'<a href="{e(c["busca"])}" target="_blank" rel="noopener">'
        f'<span class="flag">{c["pais"]}</span>'
        f'<div><div class="name">{e(c["nome"])} '
        f'<span class="iata">{e(c["iata"])}</span></div>'
        f'<div class="hint">Reservar direto no site oficial</div></div>'
        f'</a>'
        for c in COMPANHIAS
    )

    filtro_label = (
        f" • máx. {v.max_escalas} escala(s)"
        if v.max_escalas is not None else ""
    )

    acoes_html = (
        f'<div class="actions">'
        f'<button class="btn primary" id="btnReexecutar" type="button" '
        f'onclick="reexecutarBusca()">'
        f'<span id="btnIcon">🔄</span>&nbsp;'
        f'<span id="btnText">Reexecutar busca agora</span></button>'
        f'</div>'
        f'<p id="acoesHint" style="color:var(--muted);font-size:12px;'
        f'margin:4px 0 16px">Clique no botão verde para refazer a '
        f'busca agora — leva ~5 a 15 segundos.</p>'
        f'<script>'
        f'const WORKFLOW_URL = {json.dumps(run_workflow_url)};'
        f'const hint = document.getElementById("acoesHint");'
        f'const btnText = document.getElementById("btnText");'
        f'const btnIcon = document.getElementById("btnIcon");'
        f'const btn = document.getElementById("btnReexecutar");'
        f'const isFile = location.protocol === "file:";'
        f'if (isFile) {{'
        f'  hint.innerHTML = "Você está abrindo o arquivo direto '
        f'(<code>file://</code>). Para o botão refazer a busca, abra '
        f'pela URL da Vercel ou rode <code>python buscar_passagens.py '
        f'--servir</code> em <code>http://localhost:8765</code>.";'
        f'}}'
        f'async function reexecutarBusca() {{'
        f'  if (isFile) {{ '
        f'alert("Abra pela URL da Vercel ou rode --servir local."); '
        f'return; }}'
        f'  btnIcon.textContent = "⏳"; btnText.textContent = "Buscando…";'
        f'  btn.disabled = true; hint.textContent = '
        f'"Refazendo busca (5-15s)…";'
        f'  try {{'
        f'    const r = await fetch("/atualizar", '
        f'{{method:"POST",cache:"no-store"}});'
        f'    if (r.ok) {{ location.reload(); return; }}'
        f'    if (r.status === 404 || r.status === 405) {{'
        f'      hint.innerHTML = "Endpoint <code>/atualizar</code> '
        f'não disponível aqui. Abra pela URL da Vercel pra usar o botão.";'
        f'    }} else {{'
        f'      const txt = await r.text();'
        f'      hint.textContent = "Erro " + r.status + ": " + '
        f'txt.slice(0,200);'
        f'    }}'
        f'  }} catch (err) {{'
        f'    hint.textContent = "Falhou: " + err.message;'
        f'  }} finally {{'
        f'    btnIcon.textContent = "🔄";'
        f'    btnText.textContent = "Reexecutar busca agora";'
        f'    btn.disabled = false;'
        f'  }}'
        f'}}'
        f'document.addEventListener("click", function(ev) {{'
        f'  const tr = ev.target.closest("tr.clickable");'
        f'  if (!tr) return;'
        f'  const href = tr.getAttribute("data-href");'
        f'  if (href) window.open(href, "_blank", "noopener");'
        f'}});'
        f'</script>'
    )

    return _HTML_TEMPLATE.format(
        rota=e(v.rota_resumo),
        trip_label=e(trip_labels.get(v.trip_type, v.trip_type)),
        n_trechos=len(v.legs),
        pax=v.pax,
        classe=e(v.classe_label),
        filtro_label=e(filtro_label),
        acoes_html=acoes_html,
        trechos_cards=trechos_cards,
        resumo_html=resumo_html,
        detalhes_html=detalhes_html,
        links_html=links_html,
        cias_html=cias_html,
        travelpayouts_drive=_snippet_travelpayouts_drive(),
        gerado_em=datetime.now().strftime("%d/%m/%Y %H:%M"),
    )


def viagem_from_env() -> Viagem:
    """Monta a Viagem a partir de variáveis de ambiente (Vercel / Docker).

    VIAGEM_TRECHOS aceita um ou mais trechos no formato
    ORIG-DEST:DD/MM/AAAA. ORIG/DEST podem ser código IATA, código de
    cidade (SAO=GRU/CGH/VCP, PAR=CDG/ORY/BVA, LON…) ou lista de
    aeroportos separados por vírgula (GRU,CGH,VCP).

    Separadores entre trechos: `;`, `|` ou quebra de linha (recomendado
    quando você usa listas de aeroportos com vírgula). Como fallback, a
    vírgula entre trechos também funciona graças a um reconhecedor por
    regex — ambos os formatos abaixo são válidos:

      'SAO-PAR:16/07/2026;CDG-GRU:01/08/2026'                (recomendado)
      'GRU-IBZ:16/07/2026,CDG-GRU:01/08/2026'                (formato legado)
      'GRU,CGH,VCP-IBZ:16/07/2026;CDG,ORY-GRU:01/08/2026'    (com listas)
    """
    raw = os.getenv(
        "VIAGEM_TRECHOS",
        "SAO-IBZ:16/07/2026;CDG-SAO:01/08/2026",
    )
    legs = [parse_trecho(p) for p in split_trechos(raw)]
    max_raw = os.getenv("VIAGEM_MAX_ESCALAS", "2").strip()
    max_escalas = int(max_raw) if max_raw.isdigit() else None
    return Viagem(
        legs=legs,
        pax=int(os.getenv("VIAGEM_PAX", "1")),
        classe=os.getenv("VIAGEM_CLASSE", "premium").lower(),
        max_escalas=max_escalas,
    )


def workflow_url_padrao() -> str:
    owner = os.getenv("VERCEL_GIT_REPO_OWNER", "flimasales")
    slug = os.getenv("VERCEL_GIT_REPO_SLUG", "flyfinder-agent")
    return os.getenv("WORKFLOW_URL") or (
        f"https://github.com/{owner}/{slug}/actions/"
        "workflows/monitor-precos.yml"
    )


def gerar_pagina_completa(
    v: Optional[Viagem] = None,
    workflow_url: Optional[str] = None,
) -> str:
    """Busca preços e gera HTML completo (usado por --servir e Vercel)."""
    v = v or viagem_from_env()
    wu = workflow_url or workflow_url_padrao()
    links = gerar_deeplinks(v)
    dados = consultar_google_flights(v)
    return render_html(v, links, dados, run_workflow_url=wu)


def servir_local(v: Viagem, porta: int = 8765,
                  abrir: bool = True) -> None:
    """Sobe um mini-servidor HTTP local. O HTML tem um botão que chama
    POST /atualizar para refazer a busca e recarregar com novos preços."""
    estado = {"html": "", "ultima_busca": None}

    def atualizar() -> None:
        print(f"[servir] refazendo busca de {v.rota_resumo}...",
              file=sys.stderr)
        estado["html"] = gerar_pagina_completa(v)
        estado["ultima_busca"] = datetime.now()
        print(f"[servir] busca atualizada às "
              f"{estado['ultima_busca'].strftime('%H:%M:%S')}",
              file=sys.stderr)

    atualizar()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            sys.stderr.write(
                f"[servir] {self.address_string()} - {fmt % a}\n"
            )

        def _resp(self, code: int, body: bytes,
                  ctype: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path in ("/", "/index.html"):
                self._resp(200, estado["html"].encode("utf-8"))
            elif self.path == "/healthz":
                self._resp(200, b"ok", "text/plain")
            else:
                self._resp(404, b"nao encontrado", "text/plain")

        def do_POST(self):  # noqa: N802
            if self.path == "/atualizar":
                try:
                    atualizar()
                    self._resp(200, b'{"ok":true}', "application/json")
                except Exception as e:
                    self._resp(500,
                               json.dumps({"ok": False, "erro": str(e)})
                               .encode("utf-8"), "application/json")
            else:
                self._resp(404, b"nao encontrado", "text/plain")

    srv = ThreadingHTTPServer(("127.0.0.1", porta), Handler)
    url = f"http://localhost:{porta}/"
    print(f"\n=== Servidor local em {url} (Ctrl+C para parar) ===\n",
          file=sys.stderr)
    if abrir:
        threading.Thread(
            target=lambda: (time.sleep(0.6), webbrowser.open_new_tab(url)),
            daemon=True,
        ).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[servir] encerrando.", file=sys.stderr)
        srv.shutdown()


def coletar_interativo() -> Viagem:
    print("\n=== Pesquisa de Passagens ===\n")
    print(
        "Dica: para incluir vários aeroportos da mesma cidade você pode\n"
        "  - usar o código IATA da cidade (SAO=GRU/CGH/VCP, RIO=GIG/SDU,\n"
        "    PAR=CDG/ORY/BVA, LON=LHR/LGW/STN/LCY/LTN, NYC=JFK/LGA/EWR…)\n"
        "  - ou listar os aeroportos separados por vírgula (GRU,CGH,VCP).\n"
    )
    n = int(
        input("Quantos trechos? (1=só ida, 2=ida e volta, 3+ multi-cidade) "
              "[2]: ").strip() or "2"
    )
    legs = []
    for i in range(1, n + 1):
        print(f"\n-- Trecho {i} --")
        orig = input(
            "  Origem (IATA / cidade / lista, ex: GRU, SAO, GRU,CGH): "
        ).strip()
        dest = input(
            "  Destino (IATA / cidade / lista, ex: IBZ, PAR, CDG,ORY): "
        ).strip()
        data = parse_data_br(input("  Data (DD/MM/AAAA): ").strip())
        orig_clean = ",".join(
            p.strip().upper() for p in orig.split(",") if p.strip()
        ) or orig.upper()
        dest_clean = ",".join(
            p.strip().upper() for p in dest.split(",") if p.strip()
        ) or dest.upper()
        legs.append(Leg(orig_clean, dest_clean, data))
    pax = int(input("\nNº de passageiros adultos [1]: ").strip() or "1")
    classe = (
        input("Classe [economica/premium/executiva/primeira] "
              "(economica): ").strip().lower()
        or "economica"
    )
    if classe not in CLASSES:
        print(f"[aviso] classe desconhecida '{classe}', usando 'economica'.")
        classe = "economica"
    return Viagem(legs=legs, pax=pax, classe=classe)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    p = argparse.ArgumentParser(
        description="Pesquisa passagens aéreas (ida-volta, só-ida, multi-cidade).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemplos:\n"
            "  # ida e volta (atalho)\n"
            "  python buscar_passagens.py --origem GRU --destino GIG "
            "--ida 15/06/2026 --volta 22/06/2026 --html\n\n"
            "  # ida e volta SP→Paris incluindo TODOS os aeroportos\n"
            "  python buscar_passagens.py --origem SAO --destino PAR "
            "--ida 15/06/2026 --volta 22/06/2026 --html\n\n"
            "  # multi-cidade (open-jaw)\n"
            "  python buscar_passagens.py "
            "--trecho GRU-IBZ:16/07/2026 "
            "--trecho CDG-GRU:01/08/2026 --html\n\n"
            "  # multi-cidade com lista de aeroportos\n"
            "  python buscar_passagens.py "
            "--trecho GRU,CGH,VCP-IBZ:16/07/2026 "
            "--trecho CDG,ORY-GRU:01/08/2026 --html\n\n"
            "Códigos de cidade reconhecidos: SAO, RIO, BHZ, PAR, LON, MIL,\n"
            "  ROM, MOW, STO, BUH, NYC, WAS, CHI, YTO, TYO, OSA, SEL, BJS,\n"
            "  SHA, BKK, IST, BUE — cada um expande para todos os aeroportos\n"
            "  da metrópole automaticamente.\n"
        ),
    )
    p.add_argument(
        "--trecho", action="append", type=parse_trecho, metavar="ORIG-DEST:DATA",
        help="Trecho da viagem (pode repetir). Ex: GRU-IBZ:16/07/2026, "
             "SAO-PAR:16/07/2026 ou GRU,CGH,VCP-IBZ:16/07/2026",
    )
    p.add_argument(
        "--origem",
        help="[atalho] origem: IATA (GRU), cidade (SAO=GRU/CGH/VCP) "
             "ou lista (GRU,CGH,VCP)",
    )
    p.add_argument(
        "--destino",
        help="[atalho] destino: IATA, cidade ou lista (mesma sintaxe "
             "de --origem)",
    )
    p.add_argument("--ida",   type=parse_data_br, help="[atalho] data ida")
    p.add_argument("--volta", type=parse_data_br, help="[atalho] data volta")
    p.add_argument("--pax", type=int, default=1, help="Passageiros (padrão 1)")
    p.add_argument(
        "--classe", choices=list(CLASSES.keys()), default="economica",
        help="Classe (padrão: economica)",
    )
    p.add_argument(
        "--max-escalas", type=int, default=None, metavar="N",
        help="Filtra ofertas com no máximo N escalas por trecho "
             "(0 = só voo direto)",
    )
    p.add_argument(
        "--abrir", action="store_true",
        help="Abre os 5 links de busca no navegador",
    )
    p.add_argument("--salvar", metavar="ARQUIVO",
                   help="Salva o resultado em arquivo .md")
    p.add_argument(
        "--html", nargs="?", const="resultado.html", metavar="ARQUIVO",
        help="Gera página HTML e abre no navegador "
             "(arquivo padrão: resultado.html)",
    )
    p.add_argument(
        "--servir", nargs="?", const=8765, type=int, metavar="PORTA",
        help="Sobe servidor local na porta (padrão 8765). O botão "
             "'Reexecutar busca' atualiza os preços ao vivo.",
    )

    g = p.add_argument_group("monitor de preços + WhatsApp")
    g.add_argument(
        "--monitor", action="store_true",
        help="Roda em loop, checando preços e enviando alerta no WhatsApp "
             "quando o total estimado entrar na faixa-alvo",
    )
    g.add_argument("--whatsapp", metavar="NUMERO",
                   help="Número WhatsApp (com DDD, ex: 11986185400)")
    g.add_argument("--whatsapp-apikey", metavar="KEY",
                   help="API key do CallMeBot (ou env CALLMEBOT_APIKEY)")
    g.add_argument("--preco-min", type=float, default=0,
                   metavar="VALOR",
                   help="Limite inferior da faixa-alvo (R$)")
    g.add_argument("--preco-max", type=float, default=None,
                   metavar="VALOR",
                   help="Limite superior da faixa-alvo (R$). Se omitido, "
                        "o alerta em BRL fica DESATIVADO.")
    g.add_argument("--preco-min-usd", type=float, default=0,
                   metavar="VALOR",
                   help="[opcional] Limite inferior da faixa-alvo em US$ "
                        "(usado quando o total vier em USD sem conversão)")
    g.add_argument("--preco-max-usd", type=float, default=None,
                   metavar="VALOR",
                   help="[opcional] Limite superior da faixa-alvo em US$ "
                        "(ativa o alerta também pra totais em USD)")
    g.add_argument("--horario-inicio", type=int, default=22, metavar="H",
                   help="Hora (0-23) em que o monitor começa a checar")
    g.add_argument("--horario-fim", type=int, default=1, metavar="H",
                   help="Hora (0-23) em que o monitor para de checar")
    g.add_argument("--intervalo", type=int, default=30, metavar="MIN",
                   help="Intervalo em minutos entre checagens (padrão 30)")
    g.add_argument("--cooldown", type=float, default=6.0, metavar="HORAS",
                   help="Horas mínimas entre alertas semelhantes (padrão 6)")
    g.add_argument("--dry-run", action="store_true",
                   help="Monitor: simula envio sem chamar o CallMeBot")
    g.add_argument("--checar-uma-vez", action="store_true",
                   help="Monitor: faz UMA checagem e sai "
                        "(útil pra testar / agendar com Task Scheduler)")
    g.add_argument("--testar-whatsapp", action="store_true",
                   help="Envia apenas uma mensagem de teste pelo WhatsApp "
                        "(não busca voos). Use com --whatsapp e "
                        "--whatsapp-apikey (ou --dry-run).")
    g.add_argument("--ignorar-horario", action="store_true",
                   help="Checa preços mesmo fora da janela "
                        "(útil para teste manual no GitHub Actions)")
    g.add_argument("--sem-html-link", action="store_true",
                   help="Não anexa link da página HTML detalhada no alerta")

    args = p.parse_args()

    if args.testar_whatsapp:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        apikey = args.whatsapp_apikey or os.getenv("CALLMEBOT_APIKEY")
        if not args.whatsapp:
            print("[erro] use --whatsapp NUMERO (ex: 11986185400)",
                  file=sys.stderr)
            sys.exit(2)
        if not apikey and not args.dry_run:
            print(
                "[erro] sem API key — passe --whatsapp-apikey KEY "
                "ou defina env CALLMEBOT_APIKEY. Ou use --dry-run pra "
                "simular sem enviar.",
                file=sys.stderr,
            )
            sys.exit(2)
        agora = datetime.now().strftime("%d/%m/%Y %H:%M")
        msg = (
            f"✅ Teste do buscar_passagens.py\n"
            f"Se você está lendo isso pelo WhatsApp, o alerta de preço "
            f"está funcionando!\n"
            f"Enviado em {agora}."
        )
        r = enviar_whatsapp_callmebot(
            args.whatsapp, msg, apikey or "", dry_run=args.dry_run,
        )
        print(r)
        return

    if args.trecho:
        legs = list(args.trecho)
        v = Viagem(
            legs=legs, pax=args.pax, classe=args.classe,
            max_escalas=args.max_escalas,
        )
    elif all([args.origem, args.destino, args.ida, args.volta]):
        v = Viagem(
            legs=[
                Leg(args.origem, args.destino, args.ida),
                Leg(args.destino, args.origem, args.volta),
            ],
            pax=args.pax, classe=args.classe,
            max_escalas=args.max_escalas,
        )
    elif all([args.origem, args.destino, args.ida]):
        v = Viagem(
            legs=[Leg(args.origem, args.destino, args.ida)],
            pax=args.pax, classe=args.classe,
            max_escalas=args.max_escalas,
        )
    else:
        v = coletar_interativo()
        if args.max_escalas is not None:
            v.max_escalas = args.max_escalas

    if args.servir:
        servir_local(v, porta=args.servir, abrir=True)
        return

    if args.monitor and not args.checar_uma_vez:
        monitorar(v, args)
        return

    if args.checar_uma_vez:
        agora = agora_br()
        marca = agora.strftime("%d/%m %H:%M")
        if not args.ignorar_horario and not dentro_da_janela(
            agora, args.horario_inicio, args.horario_fim
        ):
            print(
                f"[{marca}] fora da janela "
                f"({args.horario_inicio}h-{args.horario_fim}h BRT). "
                f"Saindo sem checar.",
                file=sys.stderr,
            )
            return
        dados = consultar_google_flights(v)
        total, moeda, _ = calcular_total_e_moeda(dados, v.max_escalas)
        if total is None:
            print(f"[{marca}] sem total estimado disponível.",
                  file=sys.stderr)
            return
        print(
            f"[{marca}] total estimado: {formatar_moeda(total, moeda)} "
            f"(moeda={moeda})",
            file=sys.stderr,
        )

        nivel, emoji = avaliar_alerta(
            total, args.preco_min, args.preco_max,
            moeda=moeda,
            preco_min_usd=args.preco_min_usd,
            preco_max_usd=args.preco_max_usd,
        )
        if not nivel:
            if moeda == "BRL" and args.preco_max is not None:
                alvo = formatar_brl(args.preco_max)
            elif moeda == "USD" and args.preco_max_usd is not None:
                alvo = formatar_moeda(args.preco_max_usd, "USD")
            else:
                alvo = f"(sem faixa-alvo definida pra {moeda})"
            print(
                f"[{marca}] fora da faixa-alvo (> {alvo}). Sem alerta.",
                file=sys.stderr,
            )
            return

        apikey = args.whatsapp_apikey or os.getenv("CALLMEBOT_APIKEY")
        cache = _carregar_cache_alerta()
        chave = v.rota_resumo
        cooldown_h = max(0.5, args.cooldown)
        if not pode_enviar_alerta(
            cache, chave, total, cooldown_h, agora, moeda=moeda,
        ):
            print(
                f"[{marca}] alerta semelhante recente "
                f"(cooldown {cooldown_h}h). Pulando.",
                file=sys.stderr,
            )
            return

        url_html = None
        if not args.sem_html_link:
            links_dl = gerar_deeplinks(v)
            html_str = render_html(v, links_dl, dados)
            print(f"[{marca}] subindo relatório HTML...", file=sys.stderr)
            url_html = upload_html_publico(html_str)
            if url_html:
                print(f"[{marca}] relatório: {url_html}", file=sys.stderr)

        msg = montar_mensagem_alerta(
            v, total, nivel, emoji, url_html, moeda=moeda,
        )
        if args.whatsapp and (apikey or args.dry_run):
            r = enviar_whatsapp_callmebot(
                args.whatsapp, msg, apikey or "", dry_run=args.dry_run,
            )
            print(f"[{marca}] WhatsApp → {r}", file=sys.stderr)
            if not args.dry_run:
                cache[chave] = {
                    "ultimo_preco": total,
                    "moeda": moeda,
                    "timestamp": agora.isoformat(),
                    "nivel": nivel,
                    "url": url_html,
                }
                _salvar_cache_alerta(cache)
        else:
            print(
                f"[{marca}] alerta disparado, mas sem WhatsApp "
                f"configurado. Mensagem:\n{msg}",
                file=sys.stderr,
            )
        return

    links = gerar_deeplinks(v)
    dados = consultar_google_flights(v)
    md = render_markdown(v, links, dados)
    print("\n" + md)

    if args.salvar:
        Path(args.salvar).write_text(md, encoding="utf-8")
        print(f"\n[ok] markdown salvo em {args.salvar}", file=sys.stderr)

    if args.html:
        html_str = render_html(v, links, dados)
        path = Path(args.html).resolve()
        path.write_text(html_str, encoding="utf-8")
        print(f"\n[ok] HTML salvo em {path}", file=sys.stderr)
        webbrowser.open_new_tab(path.as_uri())

    if args.abrir:
        for link in links:
            webbrowser.open_new_tab(link["url"])


if __name__ == "__main__":
    main()
