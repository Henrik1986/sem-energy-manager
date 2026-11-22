# SEM 1.0.8 – Multi-system adapters

> **Status:** Releaseunderlag, utkast  
> **Aktuell betaversion:** 1.0.8-beta.2  
> **Branch:** `feature/multi-system-solax-test`

Det här dokumentet beskriver ändringarna som är tänkta att ingå när den kombinerade multi-system-versionen flyttas till `main`. Det är inte en bekräftelse på att en stabil release är publicerad. Slutlig versionsbeteckning fastställs inför releasen.

## Nyheter

### Gemensam adapterarkitektur

SEM har fått en tillverkaroberoende adapterarkitektur. Varje energisystemadapter översätter tillverkarens entiteter och funktioner till SEM:s gemensamma gränssnitt.

Den gemensamma strukturen omfattar bland annat:

- Ett gemensamt adapterkontrakt och en systemhanterare.
- Kapabilitetsdeklarationer för läsbara och skrivbara funktioner samt tillgängliga SEM-kommandon.
- Gemensam hantering av entitetsregister, proxyentiteter och aggregering.
- Tydligare installationsstatus, diagnostik och varningar för saknade eller otillgängliga funktioner.

Målet är att YAML-paketet ska arbeta mot SEM:s egna entiteter och tjänster, inte mot enskilda tillverkares entitetsnamn eller tjänster.

### Huawei

Huawei-adaptern är flyttad till den gemensamma arkitekturen.

- Befintliga publika SEM-entiteter och tjänster behålls för kompatibilitet med nuvarande YAML.
- Aggregering av flera växelriktare och batterier hanteras inom samma SEM-anläggning.
- Kapacitetsviktad SOC och gemensamma mätvärden hanteras via den gemensamma strukturen.
- Huawei har testats i en befintlig installation med den kombinerade betaversionen. Samtliga kontrollerade SEM-entiteter har fungerat.

### SolaX

En SolaX-adapter har lagts till för Home Assistant-integrationen **SolaX Modbus**.

Adaptern mappar tillgängliga SolaX-entiteter till SEM:s gemensamma gränssnitt, bland annat batteristatus, kapacitet, effektflöden, energivärden och de styrfunktioner som respektive växelriktarmodell exponerar.

För SolaX gäller följande:

- Stödet är modell- och integrationsberoende. En funktion exponeras bara när relevanta källenheter och kontroller finns tillgängliga.
- `input_number.sem_battery_total_capacity_kwh` kan användas som reservvärde när nominell batterikapacitet inte kan hämtas från en tillgänglig kapacitetssensor.
- Diagnostik och varningar används för att visa när kapacitetsinformation eller andra funktioner saknas.
- Stöd för schemalagd TOU-styrning är begränsat till verifierade Gen4- och Gen5-modeller.

**Viktigt:** SolaX-adaptern är ännu inte verifierad på en fysisk SolaX-anläggning. Den ska därför betraktas som beta tills praktiska tester har genomförts.

## Kända begränsningar

- SolaX-stödet beror på växelriktarmodell, firmware och vilka entiteter som tillhandahålls av SolaX Modbus-integrationen.
- TOU-periodtjänsten för SolaX stöder för närvarande ett begränsat schemaformat: alla veckodagar, högst två laddningsperioder och två urladdningsperioder, perioder inom samma dygn och inga överlappningar. Funktionen är avgränsad till verifierade Gen4- och Gen5-modeller.
- SEM:s virtuella reglage för maximal nätladdning och maximal urladdning på SolaX är till-/från-överstyrningar, inte kontinuerliga effektgränser i watt. Värdet 0 aktiverar spärren; ett värde skilt från 0 återställer det tidigare läget.
- SolaX-stöd för modeller eller funktioner som inte uttryckligen verifierats ska inte betraktas som garanterat.

## Kompatibilitet

- Ingen ändring av SEM:s befintliga YAML-automationer ingår i denna ändring.
- De befintliga publika Huawei-entiteterna och SEM-tjänsterna ska behålla sina nuvarande namn och betydelser.
- Användarens befintliga tillverkarintegration i Home Assistant krävs fortfarande. SEM ersätter inte Huawei Solar eller SolaX Modbus.

## Kvar före stabil release

- Slutför den planerade flerdagarskontrollen av Huawei i den kombinerade betaversionen.
- Gå igenom eventuella avvikelser innan merge.
- Kör om samtliga CI-kontroller på den slutliga merge-kandidaten: unit tests, Hassfest och HACS validation.
- Kontrollera att `main` fortfarande kan ta emot branchen utan oavsiktliga konflikter.
- Fastställ slutlig versionsbeteckning och publicera release notes tillsammans med releasen.

SolaX bör fortsatt anges som beta/ej fältverifierad tills en användare har testat adaptern mot en verklig anläggning.
