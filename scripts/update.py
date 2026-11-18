"""
Radar Tech — actualització diària de dades i alertes.

Què fa:
  1. Descarrega ~10 anys de cotitzacions diàries de cada acció (Yahoo Finance;
     si falla, Stooq com a alternativa).
  2. Descarrega els fonamentals (Yahoo Finance): PER forward, preu objectiu
     dels analistes, data dels propers resultats i el benefici per acció de
     cada trimestre, amb el qual es reconstrueix l'historial del PER.
  3. Calcula els indicadors: mitjana de 200 setmanes (200 WMA), mitjanes de
     50 i 200 dies, RSI de 14 dies, caiguda des del màxim de 52 setmanes, PER
     respecte a la seva mediana de 5 anys i una puntuació d'oferta de 0 a 100.
  4. Desa-ho tot a data/data.json, que és el que llegeix el dashboard.
  5. Si una acció ENTRA en zona d'oferta (no si ja hi era ahir), envia una
     notificació al mòbil a través de ntfy.sh.

Execució:
  python scripts/update.py           -> dades reals
  python scripts/update.py --demo    -> dades inventades, només per provar
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- configuració

TICKERS = {
    "NVDA": "Nvidia",
    "GOOGL": "Alphabet (Google)",
    "MSFT": "Microsoft",
    "AAPL": "Apple",
    "AMZN": "Amazon",
    "META": "Meta",
    "AVGO": "Broadcom",
    "TSM": "TSMC",
    "ASML": "ASML",
    "AMD": "AMD",
    "TSLA": "Tesla",
}

# Puntuació d'oferta (0-100). Pesos de cada component (han de sumar 100):
WEIGHT_WMA = 30   # com de prop està de la seva mitjana de 200 setmanes
WEIGHT_DD = 30    # quant ha caigut des del màxim de 52 setmanes
WEIGHT_RSI = 20   # si està en sobrevenda a curt termini
WEIGHT_PE = 20    # si el PER està per sota del seu habitual (mediana de 5 anys)

WMA_FAR = 60.0    # a +60% o més sobre la 200 WMA, aquest component val 0
DD_FULL = 35.0    # una caiguda del 35% o més dona el màxim d'aquest component
RSI_LOW, RSI_HIGH = 30.0, 70.0
PE_CHEAP = 30.0   # PER un 30% o més per sota de la seva mediana: màxim del component
PE_DEAR = 20.0    # PER un 20% o més per sobre: zero. Just a la mediana: 40% del component.
                  # Sense dades de PER (p. ex. pèrdues), també compta com "a la mediana".
PE_YEARS = 5      # anys per calcular el PER habitual

SCORE_OFFER = 65  # a partir d'aquí: "Oferta"
SCORE_WATCH = 40  # a partir d'aquí: "Vigilar"; per sota: "Preu alt"

# Altres senyals que també generen alerta
WMA_NEAR_PCT = 5.0  # preu a menys d'un 5% per sobre de la 200 WMA (o per sota)
RSI_ALERT = 30.0    # RSI per sota de 30

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "data.json"
STATE_FILE = ROOT / "data" / "alert_state.json"

DAILY_DAYS = 260     # ~1 any de dades diàries per a la gràfica (6M i 1A)
WEEKLY_WEEKS = 262   # ~5 anys de dades setmanals per a la gràfica (5A)


# ---------------------------------------------------------------- descàrrega

def fetch_yahoo(ticker: str) -> pd.Series:
    import yfinance as yf

    df = yf.Ticker(ticker).history(period="10y", interval="1d", auto_adjust=True)
    if df is None or df.empty:
        raise ValueError("Yahoo no ha retornat dades")
    s = df["Close"].dropna()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    return s


def fetch_stooq(ticker: str) -> pd.Series:
    import requests

    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text))
    if "Close" not in df.columns or df.empty:
        raise ValueError("Stooq no ha retornat dades")
    s = df.set_index(pd.to_datetime(df["Date"]))["Close"].dropna()
    return s[s.index >= s.index.max() - pd.Timedelta(days=3700)]


def fetch(ticker: str) -> pd.Series:
    errors = []
    for source in (fetch_yahoo, fetch_stooq):
        for attempt in range(3):
            try:
                s = source(ticker)
                if len(s) < 300:
                    raise ValueError(f"massa poques dades ({len(s)})")
                return s.sort_index()
            except Exception as e:  # noqa: BLE001
                errors.append(f"{source.__name__}: {e}")
                time.sleep(2 + attempt * 3)
    raise RuntimeError(f"{ticker}: " + " | ".join(errors[-2:]))


REC_LABELS = {"strong_buy": "Compra forta", "buy": "Compra", "hold": "Mantenir",
              "underperform": "Infraponderar", "sell": "Venda"}


def num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(x) or np.isinf(x) else x


def fetch_fundamentals(ticker: str) -> dict:
    """PER forward, analistes, propers resultats i BPA trimestral (Yahoo)."""
    import yfinance as yf

    last_err = None
    for attempt in range(3):
        try:
            tk = yf.Ticker(ticker)
            info = tk.info or {}
            ed = tk.get_earnings_dates(limit=40)
            if ed is None or ed.empty:
                raise ValueError("sense dates de resultats")
            ed.index = pd.to_datetime(ed.index, utc=True).tz_convert(None)
            today = pd.Timestamp.today().normalize()
            upcoming = ed.index[ed.index >= today]
            eps = ed["Reported EPS"].dropna()
            eps = eps[~eps.index.normalize().duplicated(keep="first")].sort_index()
            # Les ADR (TSM, ASML) poden donar el BPA en una altra moneda que el preu.
            # Ajustem l'escala perquè els últims 12 mesos coincideixin amb el BPA
            # de Yahoo en dòlars. La comparació amb l'historial no en depèn.
            ttm_last = eps.iloc[-4:].sum() if len(eps) >= 4 else None
            tr_eps = num(info.get("trailingEps"))
            if (info.get("financialCurrency") not in (None, "USD") and ttm_last and tr_eps
                    and 0.5 < tr_eps / ttm_last < 2):
                eps = eps * (tr_eps / ttm_last)
            return {
                "fwdPE": num(info.get("forwardPE")),
                "target": num(info.get("targetMeanPrice")),
                "analysts": info.get("numberOfAnalystOpinions"),
                "rec": REC_LABELS.get(info.get("recommendationKey")),
                "nextEarnings": upcoming.min().strftime("%Y-%m-%d") if len(upcoming) else None,
                "eps": eps,
            }
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 + attempt * 3)
    raise RuntimeError(f"fonamentals {ticker}: {last_err}")


def fake_fundamentals(ticker: str, close: pd.Series) -> dict:
    """Fonamentals inventats per a la demo."""
    rng = np.random.default_rng(sum(map(ord, ticker)) * 104729)
    q = pd.date_range(end=close.index[-1] - pd.Timedelta(days=20), periods=40, freq="QS") + pd.Timedelta(days=25)
    base = close.reindex(q, method="ffill").bfill().values / rng.uniform(80, 140)
    eps = pd.Series(base * rng.uniform(0.85, 1.15, len(q)), index=q)
    price = float(close.iloc[-1])
    return {
        "fwdPE": round(float(rng.uniform(12, 45)), 2),
        "target": round(price * float(rng.uniform(0.9, 1.5)), 2),
        "analysts": int(rng.integers(15, 60)),
        "rec": "Compra",
        "nextEarnings": (close.index[-1] + pd.Timedelta(days=int(rng.integers(5, 90)))).strftime("%Y-%m-%d"),
        "eps": eps,
    }


def fake_series(ticker: str) -> pd.Series:
    """Sèrie inventada (passeig aleatori) per provar sense internet."""
    rng = np.random.default_rng(sum(map(ord, ticker)) * 7919)
    days = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=2520)
    drift = rng.uniform(0.0002, 0.0012)
    vol = rng.uniform(0.012, 0.03)
    rets = rng.normal(drift, vol, len(days))
    # una caiguda final en algunes, per veure ofertes a la demo
    if ticker in ("NVDA", "AMD", "TSLA"):
        rets[-60:] -= 0.004
    return pd.Series(rng.uniform(20, 200) * np.exp(np.cumsum(rets)), index=days)


# ---------------------------------------------------------------- indicadors

def rsi_wilder(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def clamp01(x: float) -> float:
    return float(min(1.0, max(0.0, x)))


def r2(x):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    return round(float(x), 2)


def series_list(s: pd.Series):
    return [None if pd.isna(v) else round(float(v), 2) for v in s.values]


def pe_history(close: pd.Series, eps: pd.Series) -> pd.Series:
    """PER diari: preu / suma del BPA dels últims 4 trimestres publicats."""
    ttm = eps.rolling(4).sum().dropna()
    # els resultats surten després del tancament: el BPA compta des de l'endemà
    ttm.index = ttm.index.normalize() + pd.Timedelta(days=1)
    ttm = ttm[~ttm.index.duplicated(keep="last")]
    daily = ttm.reindex(ttm.index.union(close.index)).ffill().reindex(close.index)
    return (close / daily.where(daily > 0)).astype(float)


def analyse(ticker: str, close: pd.Series, fund: dict | None) -> dict:
    # Tancament setmanal, amb la data real de l'últim dia cotitzat de cada setmana
    weeks = close.index.to_period("W-FRI")
    weekly = close.groupby(weeks).last()
    weekly.index = pd.DatetimeIndex(close.index.to_series().groupby(weeks).last().values)
    wma200 = weekly.rolling(200).mean()

    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    rsi = rsi_wilder(close)
    high52 = close.rolling(252, min_periods=20).max()

    # 200 WMA traslladada a cada dia (valor de l'última setmana disponible)
    wma_daily = wma200.reindex(close.index, method="ffill")

    price = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    wma_now = wma200.dropna().iloc[-1] if wma200.notna().any() else np.nan
    dist_wma = (price / wma_now - 1) * 100 if not np.isnan(wma_now) else np.nan
    dd = (1 - price / float(high52.iloc[-1])) * 100
    rsi_now = float(rsi.iloc[-1])

    c_wma = 0.0 if np.isnan(dist_wma) else clamp01(1 - dist_wma / WMA_FAR)
    c_dd = clamp01(dd / DD_FULL)
    c_rsi = clamp01((RSI_HIGH - rsi_now) / (RSI_HIGH - RSI_LOW))

    # PER respecte al seu habitual
    pe = pd.Series(np.nan, index=close.index)
    pe_now = pe_med = pe_vs = None
    if fund and fund.get("eps") is not None and len(fund["eps"]) >= 4:
        pe = pe_history(close, fund["eps"])
        recent = pe.iloc[-252 * PE_YEARS:]
        pe_now = num(pe.iloc[-1])
        if recent.notna().sum() >= 252 * 2:
            pe_med = float(recent.median())
        if pe_now and pe_med:
            pe_vs = (pe_now / pe_med - 1) * 100
    c_pe = clamp01((PE_DEAR - (pe_vs if pe_vs is not None else 0.0)) / (PE_DEAR + PE_CHEAP))

    score = round(WEIGHT_WMA * c_wma + WEIGHT_DD * c_dd + WEIGHT_RSI * c_rsi + WEIGHT_PE * c_pe)

    status = "oferta" if score >= SCORE_OFFER else "vigilar" if score >= SCORE_WATCH else "alt"

    pe_weekly = pe.groupby(weeks).last()
    pe_weekly.index = weekly.index

    fund = fund or {}
    target = fund.get("target")

    d = close.index[-DAILY_DAYS:]
    w = weekly.index[-WEEKLY_WEEKS:]
    return {
        "t": ticker,
        "name": TICKERS[ticker],
        "date": close.index[-1].strftime("%Y-%m-%d"),
        "price": r2(price),
        "chgPct": r2((price / prev - 1) * 100),
        "wma200": r2(wma_now),
        "distWma": r2(dist_wma),
        "sma50": r2(sma50.iloc[-1]),
        "sma200": r2(sma200.iloc[-1]),
        "rsi": r2(rsi_now),
        "high52": r2(high52.iloc[-1]),
        "drawdown": r2(dd),
        "pe": r2(pe_now),
        "peMed": r2(pe_med),
        "peVsHist": r2(pe_vs),
        "fwdPE": r2(fund.get("fwdPE")),
        "target": r2(target),
        "targetUp": r2((target / price - 1) * 100) if target else None,
        "analysts": fund.get("analysts"),
        "rec": fund.get("rec"),
        "nextEarnings": fund.get("nextEarnings"),
        "score": int(score),
        "parts": {"wma": round(WEIGHT_WMA * c_wma), "dd": round(WEIGHT_DD * c_dd),
                  "rsi": round(WEIGHT_RSI * c_rsi), "pe": round(WEIGHT_PE * c_pe)},
        "status": status,
        "daily": {
            "d": [x.strftime("%Y-%m-%d") for x in d],
            "c": series_list(close.loc[d]),
            "s50": series_list(sma50.loc[d]),
            "s200": series_list(sma200.loc[d]),
            "w200": series_list(wma_daily.loc[d]),
            "rsi": series_list(rsi.loc[d]),
            "pe": series_list(pe.loc[d]),
        },
        "weekly": {
            "d": [x.strftime("%Y-%m-%d") for x in w],
            "c": series_list(weekly.loc[w]),
            "w200": series_list(wma200.loc[w]),
            "pe": series_list(pe_weekly.loc[w]),
        },
    }


# ---------------------------------------------------------------- alertes

def signals(row: dict) -> dict:
    return {
        "oferta": row["status"] == "oferta",
        "wma": row["distWma"] is not None and row["distWma"] <= WMA_NEAR_PCT,
        "rsi": row["rsi"] is not None and row["rsi"] < RSI_ALERT,
    }


def describe(row: dict, new: list) -> str:
    parts = []
    if "oferta" in new:
        parts.append(f"puntuació d'oferta {row['score']}/100")
    if "wma" in new:
        where = "per sota" if row["distWma"] < 0 else "a prop"
        parts.append(f"{where} de la 200 WMA ({row['distWma']:+.1f}%)")
    if "rsi" in new:
        parts.append(f"RSI {row['rsi']:.0f} (sobrevenda)")
    return (f"{row['t']} · {row['price']:.2f} $ · " + ", ".join(parts)
            + f" · −{row['drawdown']:.1f}% des del màxim")


def notify(messages: list, dashboard_url: str):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC no configurat: no s'envien notificacions.")
        return
    import requests

    payload = {
        "topic": topic,
        "title": "Radar Tech: " + ("1 acció d'oferta" if len(messages) == 1
                                   else f"{len(messages)} accions d'oferta"),
        "message": "\n".join(messages),
        "tags": ["chart_with_downwards_trend"],
        "priority": 4,
    }
    if dashboard_url:
        payload["click"] = dashboard_url
    r = requests.post("https://ntfy.sh/", json=payload, timeout=20)
    print("Notificació enviada:", r.status_code)


def run_alerts(rows: list, demo: bool):
    old = {}
    if STATE_FILE.exists():
        old = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    new_state, messages = {}, []
    for row in rows:
        sig = signals(row)
        new_state[row["t"]] = sig
        before = old.get(row["t"], {})
        fresh = [k for k, v in sig.items() if v and not before.get(k, False)]
        if fresh:
            messages.append(describe(row, fresh))
    STATE_FILE.write_text(json.dumps(new_state, indent=1), encoding="utf-8")
    if messages:
        print("Alertes noves:\n  " + "\n  ".join(messages))
        if not demo:
            notify(messages, os.environ.get("DASHBOARD_URL", ""))
    else:
        print("Cap alerta nova avui.")


# ---------------------------------------------------------------- principal

def main():
    demo = "--demo" in sys.argv
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

    previous = {}
    if DATA_FILE.exists():
        try:
            for row in json.loads(DATA_FILE.read_text(encoding="utf-8"))["tickers"]:
                previous[row["t"]] = row
        except Exception:  # noqa: BLE001
            pass

    rows, failed, fund_failed = [], [], []
    for t in TICKERS:
        try:
            close = fake_series(t) if demo else fetch(t)
            try:
                fund = fake_fundamentals(t, close) if demo else fetch_fundamentals(t)
            except Exception as e:  # noqa: BLE001
                # sense fonamentals avui: la resta del càlcul continua igual
                print(f"AVÍS {e}")
                fund_failed.append(t)
                fund = None
            row = analyse(t, close, fund)
            if fund is None and t in previous:  # conservem els últims valors bons
                for k in ("fwdPE", "target", "analysts", "rec", "nextEarnings"):
                    row[k] = previous[t].get(k)
                if row["target"]:
                    row["targetUp"] = r2((row["target"] / row["price"] - 1) * 100)
            rows.append(row)
            print(f"OK  {t}")
        except Exception as e:  # noqa: BLE001
            print(f"ERR {e}")
            failed.append(t)
            if t in previous:  # conservem l'últim valor bo
                rows.append({**previous[t], "stale": True})

    if not rows:
        sys.exit("No s'ha pogut descarregar cap acció.")

    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "demo": demo,
        "failed": failed,
        "fundFailed": fund_failed,
        "thresholds": {"offer": SCORE_OFFER, "watch": SCORE_WATCH,
                       "wmaNear": WMA_NEAR_PCT, "rsi": RSI_ALERT},
        "weights": {"wma": WEIGHT_WMA, "dd": WEIGHT_DD, "rsi": WEIGHT_RSI, "pe": WEIGHT_PE},
        "params": {"wmaFar": WMA_FAR, "ddFull": DD_FULL, "rsiLow": RSI_LOW, "rsiHigh": RSI_HIGH,
                   "peCheap": PE_CHEAP, "peDear": PE_DEAR, "peYears": PE_YEARS},
        "tickers": rows,
    }
    DATA_FILE.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"Desat {DATA_FILE} ({DATA_FILE.stat().st_size // 1024} KB)")

    fresh_rows = [r for r in rows if not r.get("stale")]
    run_alerts(fresh_rows, demo)

    if len(failed) == len(TICKERS):
        sys.exit(1)


if __name__ == "__main__":
    main()
