# OCPP EV-laddare – Home Assistant-integration

En Home Assistant custom component som fungerar som OCPP 1.6 Central System. Laddboxen ansluter till Home Assistant via WebSocket – inte tvärtom.

## Vad gör den?

- Tar emot data från din laddbox i realtid (effekt, ström, energi, status)
- Styr laddningen baserat på spotpriser – laddar när elen är billigast
- Beräknar optimal laddtid och kostnad utifrån ditt batteri och prisdata
- Skickar push-notiser när kabeln kopplas in, laddningen startar och avslutas

## Förutsättningar

- Home Assistant (Core 2024.x eller senare)
- En OCPP 1.6-kompatibel laddbox (testad med Garo)
- Laddboxen måste kunna peka sin WebSocket-anslutning mot HA:s IP och port 9000
- Spotpris-integration med prisintervaller (t.ex. Tibber eller GE Spot)
- Valfritt: SOC-sensor från din bil (t.ex. via Smartcar, Kia Connect)

## Installation

1. Kopiera mappen `custom_components/ocpp_charger/` till din HA:s `/config/custom_components/`
2. Starta om Home Assistant
3. Gå till **Inställningar → Enheter och tjänster → Lägg till integration**
4. Sök efter **OCPP EV Charger** och följ installationsguiden

## Installationsguiden (4 steg)

1. **Anslutning** – Port (standard 9000), laddbox-ID, max ström och antal faser
2. **Fordon** – Batteristorlek, SOC-sensor och enhet (% eller kWh)
3. **Priser** – Välj din prisintervall-entitet
4. **Notiser** – Välj notificationstjänst och vilka händelser du vill ha notiser för

## Laddlägen

| Läge | Beskrivning |
|------|-------------|
| **Immediate** | Laddar alltid när kabeln är inkopplad |
| **Smart (price-optimised)** | Laddar under de billigaste timmarna, klart till deadline (standard 06:00) |
| **Scheduled** | Laddar inom ett konfigurerat tidsintervall |

I Smart-läget kan du även sätta ett **Price Cap** (öre/kWh): är taket > 0 laddas i stället
*varje* 15-minutersintervall vars spotpris är under taket (fortfarande med deadline,
dag/natt-schema och SOC-mål som gränser). Sätt till 0 för vanlig billigaste-timmar-planering.

## Manuell deadline (valfritt)

Vill du sätta en egen sluttid för laddningen, skapa en **Tid-helper** i HA
(Inställningar → Enheter & tjänster → Hjälpare → Lägg till → Tid) med entitets-id
`input_datetime.charger_target_time`. Sätt klockslaget (t.ex. `07:30`) så planeras laddningen
klar till dess; har tiden redan passerat idag används samma tid imorgon. Lämna den på `00:00`
(eller låt bli att skapa den) för automatisk deadline (vardag 06:00, helg/dag = slutet av prisdata).
Helpern nollställs automatiskt till `00:00` när kabeln kopplas ur.

## Planeringsalgoritmer

| Algoritm | Beskrivning |
|----------|-------------|
| **Greedy (cheapest slots)** | Väljer de globalt billigaste intervallen (kan pausa/återuppta) |
| **Contiguous (cheapest block)** | Hittar det billigaste sammanhängande blocket |

## Sensorer

| Sensor | Beskrivning |
|--------|-------------|
| Status | Laddboxens status (Available, Charging, etc.) |
| Charging Power | Aktuell effekt (W) |
| Charging Current | Genomsnittlig ström L1–L3 (A) |
| Session Energy | Laddad energi sedan start (kWh) |
| Session Cost | Upplupen faktisk kostnad (SEK) |
| Battery Level | Bilens batterinivå (%) |
| Charging Time | Aktiv laddtid i minuter |
| Estimated Completion | Beräknad klar-tid |
| Estimated Charge Time Remaining | Återstående tid (t.ex. "2 h 15 min") |
| Current Electricity Price | Aktuellt spotpris (öre/kWh) |
| Session ID | Unik identifierare per session |
| Session Start | Tidpunkt för sessionens start |
| Charging Period | Day / Night / Override |
| Planned Charge Start | Planerad starttid (HH:MM) |
| Planned Charge End | Planerad sluttid (HH:MM) |
| Estimated Charge Cost | Beräknad kostnad från laddplan (SEK) |
| Charge Goal Achievable | Om laddmålet kan nås till deadline |
| Chargeable Amount | Andel av laddmålet som kan uppnås (%) |
| Planner Savings | Skillnad i kostnad mellan Greedy och Contiguous (SEK) |
| Total Charging Cost | Kumulativ totalkostnad alla sessioner (SEK) |
| Price Cap Status | Antal 15-minutersslotar under pristaket (cappat vid återstående SoC-behov) + förväntad energi/kostnad samt `slots`-lista (Price Cap-läget) |

## Binära sensorer

| Sensor | Beskrivning |
|--------|-------------|
| Cable Connected | Kabel inkopplad |
| Charging | Aktivt laddande |
| Charger Connected | OCPP WebSocket ansluten |
| Price Cap Active | Pristaksläget konfigurerat (`price_cap_ore_kwh > 0`) – på även när inga slots möter taket just nu |

## Knappar och kontroller

- **Start / Stop** – Starta eller stoppa laddning manuellt
- **Charging Mode** – Välj mellan Immediate, Smart och Scheduled
- **Active Vehicle** – Välj vilket fordon som laddas (om flera är konfigurerade)
- **Planning Algorithm** – Välj Greedy eller Contiguous planering
- **Allow Day Charging** – Aktivera/inaktivera laddning dagtid i Smart-läge
- **Override Charging Schedule** – Manuell override av dag/natt-schema
- **Auto Vehicle Detection** – Auto-identifiera fordon vid inkoppling
- **Max Charging Current** – Sätt övre strömgräns
- **Target Battery Level** – Hur mycket du vill ladda (% SOC)
- **Target Energy** – Hur mycket du vill ladda (kWh, 0 = obegränsat)
- **Battery Capacity** – Batterikapacitet för beräkningar
- **Override Current** – Manuell strömgräns vid schema-override
- **Price Cap** – Pristak i öre/kWh för Smart-läget (0 = av, ladda alla intervall under taket)

## Notiser

Notiser skickas max en gång per session för varje händelse:

- Kabel inkopplad
- Laddning startad (med SOC, ström och beräknad klar-tid)
- Laddning avslutad (med laddad energi, kostnad och tid)

Notiserna är åtgärdbara – du kan välja dag- eller nattladdning direkt från notisen.

Du kan också ange en valfri **Dashboard URL** under notis-inställningarna. Är den ifylld öppnas dashboarden i HA Companion-appen när du klickar på notisen (lämnas tomt → notisen öppnar ingenting). Fungerar både på iOS (`data.url`) och Android (`data.clickAction`).

## Schema dag/natt

Standard:
- **Dag** (06:00–22:00): 6 A
- **Natt** (22:00–06:00): 16 A

Tider och strömgränser konfigureras under integrationsalternativen.

## Smart charging-logik

1. **Immediate** – ladda alltid
2. **Smart + genomförbar plan** – ladda ENDAST inom planerade tidsfönster
   - Auto-start när klockan passerar planens starttid
   - Auto-stopp vid planens sluttid
3. **Smart + ingen plan** – fallback till priströskel (40:e percentilen)
4. **Scheduled** – ladda inom konfigurerad tidsperiod

## Skyddsmekanismer

- **Grace period (90s)** – Nyss startade sessioner stoppas inte direkt
- **Plan-frysning under laddning** – Planen räknas inte om medan laddning pågår
- **Manuell override** – Manuellt startad laddning respekteras

## HACS-installation

Manuell installation krävs för närvarande. HACS-stöd planeras.
