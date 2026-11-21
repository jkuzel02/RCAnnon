# RCAnnon – finální protokol testů promptů

20. 11. 2026 · model `deepseek-v4.1-flash` · e-INFRA CZ · výchozí archiv `02bb484`

**Podle posledního celého běhu každého scénáře prošlo 92 z 96 kontrol (95,83 %).** Plných 6/6 získalo 14 ze 16 scénářů. Ve scénářích 05 a 16 skončily Optimal i koordinátor chybou kontroly očekávaných výrazů. Všechny kroky mají uloženou odpověď. Součet kombinuje různé běhy a verze promptu koordinátora.

| Scénář a zdroj | Testovaná závada | Base | Optimal | Konfigurace a události | Logy a konektivita | Metriky | Koordinátor |
|---|---|---|---|---|---|---|---|
| [01](prompt-results-01-retest.json) | Limit paměti | OK | OK | OK | OK | OK | OK |
| [02](prompt-results-02-retest.json) | Limit CPU | OK | OK | OK | OK | OK | OK |
| [03](prompt-results-03.json) | Neplatný APP_MODE | OK | OK | OK | OK | OK | OK |
| [04](prompt-results-04.json) | Neexistující tag obrazu | OK | OK | OK | OK | OK | OK |
| [05](prompt-results-05-retest-v2.json) | Chybějící klíč v Secret | OK | Selhal | OK | OK | OK | Selhal |
| [06](prompt-results-06.json) | Předčasný zásah liveness probe | OK | OK | OK | OK | OK | OK |
| [07](prompt-results-07.json) | Chybná cesta readiness probe | OK | OK | OK | OK | OK | OK |
| [08](prompt-results-08.json) | Selektor služby | OK | OK | OK | OK | OK | OK |
| [09](prompt-results-09.json) | Chybný targetPort | OK | OK | OK | OK | OK | OK |
| [10](prompt-results-10.json) | DNS jméno závislosti | OK | OK | OK | OK | OK | OK |
| [11](prompt-results-11.json) | Odchozí provoz v NetworkPolicy | OK | OK | OK | OK | OK | OK |
| [12](prompt-results-12.json) | Příliš vysoký požadavek na CPU | OK | OK | OK | OK | OK | OK |
| [13](prompt-results-13.json) | Neexistující StorageClass | OK | OK | OK | OK | OK | OK |
| [14](prompt-results-14.json) | Oprávnění ServiceAccount | OK | OK | OK | OK | OK | OK |
| [15](prompt-results-15-retest.json) | Výpadek frontendu při OOM backendu | OK | OK | OK | OK | OK | OK |
| [16](prompt-results-16-retest-v2.json) | Chybný plugin CNI | OK | Selhal | OK | OK | OK | Selhal |

## Druhý živý retest 05 a 16

V obou scénářích klesl výsledek z 5/6 na 4/6. Koordinátor znovu nesplnil kontrolu výrazů v poli `cause`. Nově ji nesplnil ani Optimal, který dostává vlastní prompt a běží nezávisle na koordinátorovi. Base a všichni tři specialisté prošli. Před druhým retestem bylo souhrnné skóre 94/96.

- **05.** Optimal i koordinátor v poli `cause` jmenují povinný klíč `api-token` a uvádějí, že Secret obsahuje pouze `other-key`. Obě odpovědi tak vystihují příčinu. Jejich formulace nesplnila slovníkové pravidlo pro chybějící klíč.
- **16.** Obě odpovědi jmenují chybný plugin `nonexistent-cni-plugin` přímo v poli `cause`. Dopad na síťový sandbox podu popisují v poli `causal_chain`, které kontrola výrazů nečte.

## Předchozí živé retesty (mezivýsledek 94/96)

- **05 · 5/6 → 5/6.** Koordinátor v obou bězích popsal, že je vyžadován klíč `api-token`, ale Secret obsahuje pouze `other-key`. Při retestu použil formulaci „only contains“. Kontrola v poli `cause` hledá některý z výrazů `missing`, `lacks`, `absent` nebo `does not contain`. Věcná diagnóza odpovídá referenci. Dočasné rozšíření hodnoticího pravidla podle první odpovědi vedlo při offline přehrání k 6/6. Před živým retestem bylo odstraněno a do výsledného skóre se offline přehrání nezapočítává. [První běh](prompt-results-05.json) · [Retest](prompt-results-05-retest.json)
- **15 · 5/6 → 6/6.** V prvním běhu skončil požadavek koordinátora chybou spojení nebo timeoutem. V retestu koordinátor určil nesoulad mezi limitem paměti backendu 64 MiB a alokací 128 MiB. Prošlo všech šest kontrol. [První běh](prompt-results-15.json) · [Retest](prompt-results-15-retest.json)
- **16 · 2/6 → 5/6.** V prvním běhu selhaly tři požadavky na API a koordinátor byl blokován. V retestu odpovědělo všech šest rolí. Koordinátor v poli `cause` identifikoval chybný typ pluginu `nonexistent-cni-plugin`. V poli `causal_chain` vysvětlil, že kvůli tomu nelze nastavit síťový sandbox podu. Obě části dohromady odpovídají referenci. Validátor prohledává pouze `cause`, kde chyběl některý z očekávaných výrazů `sandbox`, `pod`, `network` nebo `networking`. [První běh](prompt-results-16.json) · [Retest](prompt-results-16-retest.json)

## Co test měří

Model dostal uložený kontext scénáře přes živé API. Base se hodnotí podle přítomnosti očekávaných výrazů v celé odpovědi. U Optimalu a koordinátora nástroj kontroluje strukturu JSON a hledá očekávané výrazy v poli `cause`. U specialistů kontroluje schéma zprávy a vybraná tvrzení podle dostupných pozorování. Celkové procento proto vyjadřuje průchodnost těchto kontrol. Diagnostika incidentů na běžícím clusteru vyžaduje samostatné ověření s naměřenými daty.

Dříve provedené offline testy hodnoticího nástroje prošly v 5 z 5 případů na Pythonu 3.10.6. Sada pytest pro rozhraní LangGraph zůstala mimo rozsah tohoto běhu.

## Původní živé běhy před opravou validátoru

| Scénář | Base | Optimal | MAS – konfigurace | MAS – logy | MAS – metriky | MAS – koordinátor |
|---|---|---|---|---|---|---|
| 01 – paměť | Prošel | Prošel | Prošel | Prošel | Selhal | Blokován |
| 02 – CPU | Prošel | Prošel | Prošel | Prošel | Selhal | Blokován |

- **01.** Obě samostatné varianty identifikovaly konflikt mezi cílovou alokací 128 MiB a limitem 64 MiB. Výsledek je uložen v `prompt-results-01.json`.
- **02.** Obě samostatné varianty identifikovaly limit 50m CPU u kontejneru, který pro každý požadavek provádí 500 000 iterací PBKDF2. Výsledek je uložen v `prompt-results-02.json`.

Diagnózy odpovídají referenčním příčinám scénářů. Běhy používaly uloženou konfiguraci bez živých měření z Kubernetes.

## Opakované selhání MAS

Prompt vyžaduje ve zprávě specialisty pole `agent` s hodnotou `metrics-telemetry`. Schéma v `evaluation/prompt_checks/schemas.py` ho před opravou nedefinovalo a zakazovalo dodatečná pole. U obou uložených odpovědí validátor ohlásil jedinou chybu:

```text
Additional properties are not allowed ('agent' was unexpected)
```

Runner proto v obou případech zastavil MAS před voláním koordinátora. Celkem vzniklo 10 odpovědí modelu. Osm prošlo a dvě odmítla validace. Dvě plánovaná volání koordinátora byla blokována.

## Živý retest po opravě validátoru

Opravené schéma přijímá pole `agent` s hodnotou `metrics-telemetry`. Výsledky původních běhů jsou v tabulce výše.

| Scénář | Výsledek po opravě | Soubor |
|---|---|---|
| 01 – paměť | 6/6 prošlo včetně specialisty na metriky a koordinátora | `prompt-results-01-retest.json` |
| 02 – CPU | 6/6 prošlo včetně specialisty na metriky a koordinátora | `prompt-results-02-retest.json` |
