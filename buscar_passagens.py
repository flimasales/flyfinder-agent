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

Exemplos:
  # ida e volta (atalho)
  python buscar_passagens.py --origem GRU --destino GIG \\
      --ida 15/06/2026 --volta 22/06/2026 --html

  # multi-trecho (open-jaw)
  python buscar_passagens.py \\
      --trecho GRU-IBZ:16/07/2026 \\
      --trecho CDG-GRU:01/08/2026 \\
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
        return self.origem.upper()

    @property
    def destino_iata(self) -> str:
        return self.destino.upper()

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


def parse_trecho(s: str) -> Leg:
    """Formato: ORIG-DEST:DD/MM/AAAA  ex: GRU-IBZ:16/07/2026"""
    s = s.strip()
    m = re.match(
        r"^\s*([A-Za-z]{3})\s*-\s*([A-Za-z]{3})\s*[:\s]\s*(.+?)\s*$", s
    )
    if not m:
        raise argparse.ArgumentTypeError(
            f"Trecho inválido: {s!r}. "
            f"Use o formato ORIG-DEST:DD/MM/AAAA (ex: GRU-IBZ:16/07/2026)."
        )
    orig, dest, data_str = m.groups()
    return Leg(orig.upper(), dest.upper(), parse_data_br(data_str))


def gerar_link_oferta(leg: Leg, oferta: dict, classe_label: str,
                      pax: int = 1) -> str:
    """Gera URL do Google Flights para uma oferta específica de um trecho.
    Inclui a companhia para refinar a busca."""
    cia = (oferta.get("cia") or "").strip()
    base = (
        f"Voo só ida de {leg.origem_iata} para {leg.destino_iata} "
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


def consultar_travelpayouts(leg: Leg, token: str,
                            classe: str = "economy",
                            limite: int = 30) -> list:
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

    ofertas = []
    for it in data.get("data", []) or []:
        preco_num = float(it.get("price", 0)) or None
        if not preco_num:
            continue
        cia = it.get("airline") or "—"
        gate = it.get("gate") or ""
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
            "fonte":     _fonte_normalizada(gate),
        })
    return ofertas


def consultar_google_flights(v: Viagem) -> Optional[dict]:
    """Consulta o Google Flights via fast-flights, um trecho por vez
    (busca one-way para cada leg). Retorna dict com lista por trecho
    ou None se o pacote não estiver instalado."""
    try:
        from fast_flights import FlightData, Passengers, get_flights
    except ImportError:
        print(
            "[info] 'fast-flights' não instalado — usando apenas deep links. "
            "Para preços reais: pip install fast-flights",
            file=sys.stderr,
        )
        return None

    trechos = []
    for i, leg in enumerate(v.legs, 1):
        print(
            f"[buscando] trecho {i}/{len(v.legs)}: "
            f"{leg.origem_iata} → {leg.destino_iata} em {leg.data_br}...",
            file=sys.stderr,
        )
        try:
            result = get_flights(
                flight_data=[
                    FlightData(
                        date=leg.data_iso,
                        from_airport=leg.origem_iata,
                        to_airport=leg.destino_iata,
                    ),
                ],
                trip="one-way",
                seat=v.classe_codigo,
                passengers=Passengers(adults=v.pax),
                fetch_mode="fallback",
            )
        except Exception as e:
            print(f"[erro fast-flights trecho {i}] {e}", file=sys.stderr)
            trechos.append({
                "leg": leg, "ofertas": [], "current_price": None,
                "erro": str(e),
            })
            continue

        ofertas = []
        for f in getattr(result, "flights", []) or []:
            preco_raw = getattr(f, "price", None)
            preco_num = _preco_para_float(preco_raw)
            preco_brl, preco_str = normalizar_para_brl(
                preco_num, str(preco_raw) if preco_raw else "—"
            )
            ofertas.append({
                "cia":       getattr(f, "name", "—") or "—",
                "saida":     getattr(f, "departure", "—"),
                "chegada":   getattr(f, "arrival", "—"),
                "duracao":   getattr(f, "duration", "—"),
                "escalas":   getattr(f, "stops", 0),
                "preco_str": preco_str,
                "preco":     preco_brl,
                "melhor":    bool(getattr(f, "is_best", False)),
                "fonte":     "Google Flights",
            })

        tp_token = (os.getenv("TRAVELPAYOUTS_TOKEN") or "").strip()
        if tp_token:
            print(
                f"[travelpayouts] consultando token=***{tp_token[-4:]} "
                f"trecho={leg.origem_iata}->{leg.destino_iata} "
                f"data={leg.data_iso}...",
                file=sys.stderr,
            )
            tp_ofertas = consultar_travelpayouts(
                leg, tp_token, classe=v.classe_codigo,
            )
            if tp_ofertas:
                fontes = sorted({o['fonte'] for o in tp_ofertas})
                print(
                    f"[travelpayouts] +{len(tp_ofertas)} ofertas "
                    f"({', '.join(fontes)})",
                    file=sys.stderr,
                )
            else:
                print(
                    "[travelpayouts] 0 ofertas retornadas "
                    "(token inválido, rota sem dados, ou data muito próxima)",
                    file=sys.stderr,
                )
            ofertas.extend(tp_ofertas)
        else:
            print(
                "[travelpayouts] SEM token — defina TRAVELPAYOUTS_TOKEN "
                "pra ativar Skyscanner/Kiwi/Trip.com",
                file=sys.stderr,
            )

        trechos.append({
            "leg": leg,
            "ofertas": ofertas,
            "current_price": getattr(result, "current_price", None),
            "erro": None,
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
    if not dados:
        return None
    total = 0.0
    for td in dados.get("trechos", []):
        ofertas = _filtrar_escalas(td["ofertas"], max_escalas)
        com_preco = [o for o in ofertas if o["preco"] is not None]
        if not com_preco:
            return None
        total += min(o["preco"] for o in com_preco)
    return total if total > 0 else None


def formatar_brl(v: float) -> str:
    s = f"R$ {v:,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


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


def avaliar_alerta(total: float, preco_min: float,
                    preco_max: float) -> tuple[Optional[str], Optional[str]]:
    """Retorna (nível, emoji) se deve alertar, ou (None, None)."""
    if total < preco_min:
        return (
            f"promoção excepcional (abaixo de {formatar_brl(preco_min)})",
            "🔥",
        )
    if total <= preco_max:
        return (
            f"dentro da faixa-alvo ({formatar_brl(preco_min)}–"
            f"{formatar_brl(preco_max)})",
            "🎯",
        )
    return None, None


def pode_enviar_alerta(cache: dict, chave: str, total: float,
                       cooldown_h: float, agora: datetime) -> bool:
    info = cache.get(chave, {})
    ult_preco = info.get("ultimo_preco")
    ult_ts = info.get("timestamp")
    if ult_preco is None or not ult_ts:
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
                           url_relatorio: Optional[str] = None) -> str:
    trechos_txt = " + ".join(
        f"{L.origem_iata}→{L.destino_iata} {L.data_br}" for L in v.legs
    )
    linhas = [
        f"{emoji} ALERTA DE PREÇO",
        f"Rota: {v.rota_resumo}",
        f"Trechos: {trechos_txt}",
        f"Classe: {v.classe_label}",
        f"Total estimado: {formatar_brl(total)}",
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
    print(
        f"\n=== Monitor de preços ativado ===\n"
        f"Rota:        {v.rota_resumo}\n"
        f"Faixa-alvo:  {formatar_brl(args.preco_min)} a "
        f"{formatar_brl(args.preco_max)}\n"
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
        total = calcular_total_estimado(dados, v.max_escalas)
        if total is None:
            print(
                f"[{marca}] não foi possível calcular o total estimado "
                f"(algum trecho sem preço). Tentando de novo em "
                f"{args.intervalo} min.",
                file=sys.stderr,
            )
            time.sleep(intervalo_s)
            continue

        print(f"[{marca}] total estimado: {formatar_brl(total)}",
              file=sys.stderr)

        nivel, emoji = avaliar_alerta(total, args.preco_min, args.preco_max)

        if not nivel:
            print(
                f"[{marca}] acima do alvo "
                f"(> {formatar_brl(args.preco_max)}). Sem alerta.",
                file=sys.stderr,
            )
            time.sleep(intervalo_s)
            continue

        envia = pode_enviar_alerta(cache, chave, total, cooldown_h, agora)
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
            msg = montar_mensagem_alerta(v, total, nivel, emoji, url_html)
            r = enviar_whatsapp_callmebot(
                args.whatsapp or "", msg, apikey or "", dry_run=args.dry_run,
            )
            print(f"[{marca}] WhatsApp → {r}", file=sys.stderr)
            cache[chave] = {
                "ultimo_preco": total,
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
            f"| {i} | {L.origem_iata} | {L.destino_iata} | {L.data_br} |"
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
                out.append(
                    f"- **Trecho {i}** ({L.origem_iata} → {L.destino_iata}, "
                    f"{L.data_br}): melhor {m['preco_str']} — {m['cia']} "
                    f"({m['duracao']})"
                )
            else:
                out.append(
                    f"- **Trecho {i}** ({L.origem_iata} → {L.destino_iata}, "
                    f"{L.data_br}): _sem ofertas retornadas_"
                )
        if total_ok:
            out.append(
                f"- **Total estimado (passagens separadas):** "
                f"R$ {total:,.2f}".replace(",", "X")
                .replace(".", ",").replace("X", ".")
            )
        out.append("")

        for i, td in enumerate(trechos_dados, 1):
            L = td["leg"]
            out.append(
                f"## Trecho {i}: {L.origem_iata} → {L.destino_iata} "
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

            por_preco = sorted(
                td["ofertas"],
                key=lambda o: (o["preco"] is None, o["preco"] or 0),
            )
            out.append(
                "| # | Companhia | Saída | Chegada | Duração | Escalas | "
                "Preço | Fonte | Reservar |"
            )
            out.append(
                "|---|-----------|-------|---------|---------|---------|"
                "-------|-------|----------|"
            )
            for j, o in enumerate(por_preco[:15], 1):
                marca = " ⭐" if o["melhor"] else ""
                link = gerar_link_oferta(
                    td["leg"], o, v.classe_label, v.pax,
                )
                fonte = o.get("fonte") or "Google Flights"
                out.append(
                    f"| {j} | {o['cia']}{marca} | {o['saida']} | "
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
  .fonte-kiwi       {{ background: rgba(244,114,182,.15); color: #f472b6; }}
  .fonte-trip       {{ background: rgba(251,146,60,.15); color: #fb923c; }}
  .fonte-outras     {{ background: rgba(148,163,184,.15); color: #94a3b8; }}
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: 11px; font-weight: 600; margin-left: 6px;
  }}
  .badge.best {{ background: rgba(52,211,153,.15); color: var(--good); }}
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
        f'<span class="leg-route">{e(L.origem_iata)} → {e(L.destino_iata)}</span>'
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
        total = 0.0
        total_ok = True
        for i, td in enumerate(trechos_dados, 1):
            L = td["leg"]
            m = _melhor_oferta(td["ofertas"])
            if m:
                if m.get("preco") is not None:
                    total += m["preco"]
                else:
                    total_ok = False
                cards.append(
                    f'<div class="card">'
                    f'<div class="label">Trecho {i} — '
                    f'{e(L.origem_iata)} → {e(L.destino_iata)}</div>'
                    f'<div class="value">{e(str(m["preco_str"]))}</div>'
                    f'<div class="label" style="margin-top:4px">'
                    f'{e(m["cia"])} • {e(str(m["duracao"]))}</div>'
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
            total_str = f"R$ {total:,.2f}".replace(",", "X").replace(
                ".", ","
            ).replace("X", ".")
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
                f"Trecho {i}: {e(L.origem_iata)} → {e(L.destino_iata)} "
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

            por_preco = sorted(
                td["ofertas"],
                key=lambda o: (o["preco"] is None, o["preco"] or 0),
            )
            linhas = []
            for o in por_preco[:15]:
                badge = (
                    '<span class="badge best">Melhor</span>'
                    if o["melhor"] else ""
                )
                link_oferta = gerar_link_oferta(
                    td["leg"], o, v.classe_label, v.pax,
                )
                fonte = o.get("fonte") or "Google Flights"
                cls_fonte = (
                    "google" if "Google" in fonte
                    else "skyscanner" if "Skyscanner" in fonte
                    else "kiwi" if "Kiwi" in fonte
                    else "trip" if "Trip" in fonte
                    else "outras"
                )
                linhas.append(
                    f'<tr class="clickable" '
                    f'onclick="window.open({json.dumps(link_oferta)}, '
                    f"'_blank','noopener')\" "
                    f'title="Abrir esta oferta no Google Flights">'
                    f"<td>{e(o['cia'])}{badge}</td>"
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
            secoes.append(
                f"<h3>{titulo}</h3>"
                f"<div class='label' style='color:var(--muted);"
                f"font-size:12px;margin-bottom:6px'>"
                f"Nível de preço Google: <b style='color:var(--warn)'>"
                f"{e(str(nivel))}</b></div>"
                f"<table><thead><tr>"
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
        f'<a class="btn secondary" '
        f'href="{e(links[0]["url"])}" target="_blank" rel="noopener">'
        f'🔎 Abrir no Google Flights</a>'
        f'</div>'
        f'<p id="acoesHint" style="color:var(--muted);font-size:12px;'
        f'margin:4px 0 16px">Carregando…</p>'
        f'<script>'
        f'const WORKFLOW_URL = {json.dumps(run_workflow_url)};'
        f'const hint = document.getElementById("acoesHint");'
        f'const btnText = document.getElementById("btnText");'
        f'const btnIcon = document.getElementById("btnIcon");'
        f'const isInteractive = location.protocol === "http:" && '
        f'(location.hostname === "localhost" || '
        f'location.hostname === "127.0.0.1" || '
        f'location.hostname.endsWith(".vercel.app"));'
        f'if (isInteractive) {{'
        f'  const onde = location.hostname.endsWith(".vercel.app") '
        f'? "na Vercel" : "no servidor local";'
        f'  hint.innerHTML = "Clique no botão verde para refazer a busca '
        f'" + onde + ". A página recarrega com preços atualizados '
        f'(leva ~5–15s).";'
        f'}} else {{'
        f'  hint.innerHTML = "Abra pelo link da Vercel ou rode '
        f'<code>python buscar_passagens.py --servir</code> em '
        f'<code>http://localhost:8765</code> para atualizar aqui.";'
        f'}}'
        f'async function reexecutarBusca() {{'
        f'  if (!isInteractive) {{ '
        f'window.open(WORKFLOW_URL, "_blank"); return; }}'
        f'  btnIcon.textContent = "⏳"; btnText.textContent = "Buscando…";'
        f'  document.getElementById("btnReexecutar").disabled = true;'
        f'  try {{'
        f'    const r = await fetch("/atualizar", {{method:"POST"}});'
        f'    if (r.ok) {{ location.reload(); }}'
        f'    else {{ alert("Erro: " + r.status); }}'
        f'  }} catch (err) {{ alert("Falhou: " + err); }}'
        f'  finally {{'
        f'    btnIcon.textContent = "🔄";'
        f'    btnText.textContent = "Reexecutar busca agora";'
        f'    document.getElementById("btnReexecutar").disabled = false;'
        f'  }}'
        f'}}'
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
    """Monta a Viagem a partir de variáveis de ambiente (Vercel / Docker)."""
    raw = os.getenv(
        "VIAGEM_TRECHOS",
        "GRU-IBZ:16/07/2026,CDG-GRU:01/08/2026",
    )
    legs = [parse_trecho(p.strip()) for p in raw.split(",") if p.strip()]
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
    n = int(
        input("Quantos trechos? (1=só ida, 2=ida e volta, 3+ multi-cidade) "
              "[2]: ").strip() or "2"
    )
    legs = []
    for i in range(1, n + 1):
        print(f"\n-- Trecho {i} --")
        orig = input("  Origem (IATA, ex: GRU): ").strip()
        dest = input("  Destino (IATA, ex: IBZ): ").strip()
        data = parse_data_br(input("  Data (DD/MM/AAAA): ").strip())
        legs.append(Leg(orig, dest, data))
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
            "  # multi-cidade (open-jaw)\n"
            "  python buscar_passagens.py "
            "--trecho GRU-IBZ:16/07/2026 "
            "--trecho CDG-GRU:01/08/2026 --html\n"
        ),
    )
    p.add_argument(
        "--trecho", action="append", type=parse_trecho, metavar="ORIG-DEST:DATA",
        help="Trecho da viagem (pode repetir). Ex: GRU-IBZ:16/07/2026",
    )
    p.add_argument("--origem",  help="[atalho ida-volta] IATA origem")
    p.add_argument("--destino", help="[atalho ida-volta] IATA destino")
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
    g.add_argument("--preco-max", type=float, default=999999,
                   metavar="VALOR",
                   help="Limite superior da faixa-alvo (R$)")
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
        total = calcular_total_estimado(dados, v.max_escalas)
        if total is None:
            print(f"[{marca}] sem total estimado disponível.",
                  file=sys.stderr)
            return
        print(f"[{marca}] total estimado: {formatar_brl(total)}",
              file=sys.stderr)

        nivel, emoji = avaliar_alerta(total, args.preco_min, args.preco_max)
        if not nivel:
            print(
                f"[{marca}] fora da faixa-alvo "
                f"(> {formatar_brl(args.preco_max)}). Sem alerta.",
                file=sys.stderr,
            )
            return

        apikey = args.whatsapp_apikey or os.getenv("CALLMEBOT_APIKEY")
        cache = _carregar_cache_alerta()
        chave = v.rota_resumo
        cooldown_h = max(0.5, args.cooldown)
        if not pode_enviar_alerta(cache, chave, total, cooldown_h, agora):
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

        msg = montar_mensagem_alerta(v, total, nivel, emoji, url_html)
        if args.whatsapp and (apikey or args.dry_run):
            r = enviar_whatsapp_callmebot(
                args.whatsapp, msg, apikey or "", dry_run=args.dry_run,
            )
            print(f"[{marca}] WhatsApp → {r}", file=sys.stderr)
            if not args.dry_run:
                cache[chave] = {
                    "ultimo_preco": total,
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
