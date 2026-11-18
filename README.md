# Radar Tech

Dashboard per seguir cada dia les grans tecnològiques (NVDA, GOOGL, MSFT, AAPL, AMZN, META, AVGO, TSM, ASML, AMD, TSLA) i veure quan cotitzen barates respecte al seu propi historial.

- Gràfica interactiva amb selector d'acció (6 mesos, 1 any, 5 anys)
- Mitjana de 200 setmanes, mitjanes de 50 i 200 dies, RSI de 14 dies (cada línia es pot amagar o mostrar tocant-la a la llegenda)
- Fonamentals: PER forward, PER dels últims 12 mesos i la seva gràfica comparada amb la mediana de 5 anys, preu objectiu dels analistes i data dels propers resultats
- Puntuació d'oferta de 0 a 100 i semàfor (Oferta / Vigilar / Preu alt)
- Notificació al mòbil quan una acció entra en zona d'oferta

## Com funciona

Cada dia feiner, després del tancament de Wall Street, GitHub executa `scripts/update.py`: descarrega les cotitzacions, calcula els indicadors, desa `data/data.json` i torna a publicar la pàgina. No cal cap clau d'API ni tenir l'ordinador encès.

## Posar-ho en marxa (una sola vegada)

1. **Repositori**: crea a GitHub un repositori públic anomenat `Radar-Tech` i puja-hi tots aquests fitxers (inclosa la carpeta `.github`).
2. **Pages**: al repositori, ves a *Settings → Pages* i a *Source* tria **GitHub Actions**.
3. **Notificacions al mòbil**:
   - Instal·la l'app gratuïta **ntfy** (Android o iPhone).
   - A l'app, toca *+* i subscriu-te a un tema amb un nom difícil d'endevinar, per exemple `radar-tech-david-7k2q9`. Qualsevol persona que sàpiga el nom podria llegir els avisos, per això ha de ser poc evident.
   - A GitHub: *Settings → Secrets and variables → Actions → New repository secret*. Nom: `NTFY_TOPIC`. Valor: el nom del tema que has triat.
4. **Primera execució**: pestanya *Actions* → *Actualitzar dades i publicar* → **Run workflow**. En un parell de minuts tindràs la pàgina a `https://dddaviddd777.github.io/Radar-Tech/`.

La primera execució t'avisarà de totes les accions que ja estiguin en zona d'oferta. A partir d'aquí, només rebràs avisos quan una acció hi entri de nou.

## Canviar coses

Tot es configura a dalt de `scripts/update.py`:

- `TICKERS`: afegir o treure accions (símbol de Yahoo Finance i nom).
- `SCORE_OFFER`, `SCORE_WATCH`: a partir de quina puntuació és "Oferta" o "Vigilar".
- `WMA_NEAR_PCT`, `RSI_ALERT`: els altres senyals que generen avís.
- Pesos de la puntuació: `WEIGHT_WMA`, `WEIGHT_DD`, `WEIGHT_RSI`, `WEIGHT_PE` (han de sumar 100).
- PER respecte al seu habitual: `PE_CHEAP`, `PE_DEAR`, `PE_YEARS`.

Els fonamentals surten de Yahoo Finance. Si un dia no es poden descarregar, el dashboard conserva els de l'última vegada i la resta s'actualitza igual. El PER dels últims 12 mesos es calcula amb el benefici per acció que publica cada trimestre l'empresa. Si una empresa té pèrdues, el PER no té sentit i aquell component de la puntuació compta com si estigués a la seva mediana.

Per provar-ho a l'ordinador sense internet: `python scripts/update.py --demo` (genera dades inventades).

## Avís

Eina de seguiment personal. Els indicadors mostren si una acció està barata respecte al seu propi historial; no prediuen el futur ni són un consell d'inversió.
